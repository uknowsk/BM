"""Competitor match: pick the best rival models for our product / a spec we are developing.

Works on rows of the durable discovery history (store.Store.list_seen): dicts with brand, country, sub, major,
model_number, name, price_usd, price_local, currency, attrs (listing/detail facts), first_seen, baseline.
Pure functions, no I/O. Every score comes with its evidence so the UI can explain a ranking.

Price: a 5-tier split (quintiles) plus a continuous proximity score. Recency: release date > site "new" flag >
first discovery by this app (approximate, always labelled with its basis). Response: Bayesian-adjusted site rating.
Unknown data is never scored as bad: it is marked unknown and the remaining weights are renormalised (coverage shown).
"""
from __future__ import annotations

import statistics
from bisect import bisect_right
from datetime import date, datetime
from typing import Any, Optional

import catalog
import filters

TIER_LABELS = ["보급", "중저가", "중가", "중고가", "프리미엄"]
DEFAULT_WEIGHTS = {"price": 35, "spec": 30, "recency": 20, "response": 15}
NEUTRAL = {"price": 0.2, "spec": 0.5, "recency": 0.35, "response": 0.4}  # score of an UNKNOWN component
DEFAULT_BAND_PCT = 25.0  # price proximity reaches 0 at +-25 % of the target price
MIN_TIER_POOL = 10  # fewer priced models than this in the sub group: tiers are computed over the whole major group
PRIOR_REVIEWS = 20  # Bayesian prior strength m
FEATURES = ("wifi", "convection", "air_fry", "steam", "energy_star")


# ------------------------------------------------------------------ site NEW flags that carry no information
def distrust_blanket_new_flags(pool: list[dict], share: float = 0.6, min_group: int = 5) -> tuple[list[dict], list[str]]:
    """A site that flags most of a listing as "new" (e.g. every range of a brand) is not telling us anything: drop
    `is_new` from such (brand, country, sub) groups. Returns (pool, ["Brand sub", ...] of the ignored groups)."""
    groups: dict[tuple, list[dict]] = {}
    for r in pool:
        groups.setdefault((r["brand"], r["country"], r["sub"]), []).append(r)
    bad = {k for k, rows in groups.items()
           if len(rows) >= min_group and sum(1 for r in rows if (r.get("attrs") or {}).get("is_new")) / len(rows) >= share}
    if not bad:
        return pool, []
    cleaned = [{**r, "attrs": {k: v for k, v in (r.get("attrs") or {}).items() if k != "is_new"}}
               if (r["brand"], r["country"], r["sub"]) in bad else r for r in pool]
    return cleaned, sorted(f"{b} {s}" for b, _, s in bad)


# ------------------------------------------------------------------ price
def row_price(row: dict) -> Optional[float]:
    return row["price_usd"] if row.get("price_usd") is not None else row.get("price_local")


def price_tiers(prices: list[float], n: int = 5) -> list[float]:
    """Upper cut prices between tiers (n-1 values, ascending; fewer when there are too few prices). A price equal to a
    cut belongs to the higher tier."""
    prices = sorted(prices)
    n_eff = min(n, len(prices))
    if n_eff < 2:
        return []
    return list(statistics.quantiles(prices, n=n_eff, method="inclusive"))


def tier_of(price: float, edges: list[float]) -> int:
    return bisect_right(edges, price)  # 0-based


def tier_label(tier: int, n_tiers: int) -> str:
    return TIER_LABELS[tier] if n_tiers == len(TIER_LABELS) else f"{tier + 1}/{n_tiers}단계"


def tier_ranges(prices: list[float], edges: list[float]) -> list[dict]:
    n = len(edges) + 1
    out = []
    for t in range(n):
        ps = [p for p in prices if tier_of(p, edges) == t]
        out.append({"tier": t + 1, "label": tier_label(t, n), "min": min(ps) if ps else None,
                    "max": max(ps) if ps else None, "count": len(ps)})
    return out


def proximity(price: float, target: float, band_pct: float = DEFAULT_BAND_PCT) -> float:
    """0..1: 1 at the target price, falling quadratically to 0 at +-band_pct."""
    if target <= 0 or band_pct <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(price - target) / target / (band_pct / 100.0)) ** 2


# ------------------------------------------------------------------ spec fit
def _facts(row: dict) -> dict[str, Any]:
    """Name-derived facts, overridden by listing/detail attrs."""
    return {**filters.name_facts(row.get("name") or ""), **(row.get("attrs") or {})}


def spec_fit(specs: dict[str, Any], facts: dict[str, Any], width_tol_in: float = 1.5) -> dict:
    """Compare the wanted specs with a model's facts. Returns {score|None, hard_fail, details}; details rows carry
    score None when the model's value is unknown (not penalised)."""
    details, weighted, total_w, hard_fail = [], 0.0, 0.0, False

    def add(key, want, actual, score, weight=1.0):
        nonlocal weighted, total_w
        details.append({"key": key, "want": want, "actual": actual, "score": score})
        if score is not None:
            weighted += score * weight
            total_w += weight

    if specs.get("width_in") is not None:
        want, got = float(specs["width_in"]), facts.get("width_in")
        if got is None:
            add("width_in", want, None, None)
        else:
            diff = abs(float(got) - want)
            if diff > width_tol_in:
                hard_fail = True
            add("width_in", want, got, max(0.0, 1.0 - 0.5 * diff / width_tol_in) if diff <= width_tol_in else 0.0, 2.0)
    for key in ("capacity_total_cuft", "oven_capacity_cuft"):
        if specs.get(key) is not None:
            want, got = float(specs[key]), facts.get(key)
            if got is None:
                add(key, want, None, None)
            else:
                add(key, want, got, max(0.0, 1.0 - abs(float(got) - want) / want / 0.25))
    if specs.get("fuel"):
        got = facts.get("fuel")
        if got is None:
            add("fuel", specs["fuel"], None, None)
        else:
            add("fuel", specs["fuel"], got, 1.0 if got == specs["fuel"] else (0.5 if "dual_fuel" in (got, specs["fuel"]) else 0.0))
    if specs.get("burners") is not None:
        want, got = int(specs["burners"]), facts.get("burners")
        add("burners", want, got, None if got is None else {0: 1.0, 1: 0.5}.get(abs(int(got) - want), 0.0))
    for feat in specs.get("features") or []:
        got = facts.get(feat)
        add(feat, True, got, None if got is None else (1.0 if got else 0.0))
    return {"score": (weighted / total_w) if total_w else None, "hard_fail": hard_fail, "details": details}


# ------------------------------------------------------------------ recency
def _parse_date(text: str) -> Optional[date]:
    try:
        parts = [int(p) for p in str(text).split("-")]
        if len(parts) == 1:
            return date(parts[0], 7, 1)  # a bare year: middle of the year
        if len(parts) == 2:
            return date(parts[0], parts[1], 15)
        return date(*parts[:3])
    except (ValueError, TypeError):
        return None


def _age_score(months: float) -> float:
    if months <= 12:
        return 1.0
    if months <= 24:
        return 1.0 - 0.5 * (months - 12) / 12
    if months <= 48:
        return 0.5 * (1.0 - (months - 24) / 24)
    return 0.0


def recency(row: dict, now: Optional[datetime] = None) -> dict:
    """{score|None, basis, months|None, evidence}. basis: release_date | site_new | first_seen | unknown."""
    now = now or datetime.now()
    attrs = row.get("attrs") or {}
    released = _parse_date(attrs["release_date"]) if attrs.get("release_date") else None
    if released:
        months = (now.date() - released).days / 30.44
        score = _age_score(months) * (0.6 if attrs.get("release_src") == "sitemap" else 1.0)
        return {"score": score, "basis": "release_date", "months": round(months, 1),
                "evidence": f"{attrs['release_date']} ({attrs.get('release_src') or '?'})"}
    if attrs.get("is_new"):
        return {"score": 1.0, "basis": "site_new", "months": None, "evidence": "사이트 신제품 표시"}
    if row.get("baseline") == 0 and row.get("first_seen"):
        try:
            days = (now - datetime.fromisoformat(row["first_seen"])).days
        except ValueError:
            days = None
        if days is not None:
            return {"score": 0.9 if days <= 180 else 0.6 if days <= 365 else 0.4, "basis": "first_seen",
                    "months": round(days / 30.44, 1), "evidence": f"앱이 {row['first_seen'][:10]}에 처음 발견"}
    return {"score": None, "basis": "unknown", "months": None, "evidence": "출시 시점 미확인"}


def is_launch(row: dict, now: Optional[datetime] = None, window_months: float = 12) -> bool:
    r = recency(row, now)
    if r["basis"] in ("release_date", "first_seen"):
        return r["months"] is not None and r["months"] <= window_months
    return r["basis"] == "site_new"


# ------------------------------------------------------------------ consumer response
def prior_rating(rows: list[dict]) -> float:
    """Review-weighted mean rating of the pool (4.2 when nothing is known)."""
    num = den = 0.0
    for r in rows:
        a = r.get("attrs") or {}
        if a.get("rating") is not None and a.get("review_count"):
            num += float(a["rating"]) * a["review_count"]
            den += a["review_count"]
    return num / den if den else 4.2


def response(attrs: dict, prior: float, m: int = PRIOR_REVIEWS) -> dict:
    """Bayesian-adjusted rating mapped to 0..1 (3.0 -> 0, 5.0 -> 1); unknown without rating and review count."""
    rating, count = attrs.get("rating"), attrs.get("review_count")
    if rating is None or not count:
        return {"score": None, "rating": rating, "reviews": count, "adjusted": None}
    adjusted = (count * float(rating) + m * prior) / (count + m)
    return {"score": min(1.0, max(0.0, (adjusted - 3.0) / 2.0)), "rating": float(rating), "reviews": int(count),
            "adjusted": round(adjusted, 2)}


# ------------------------------------------------------------------ ranking
def _currency_of(target: dict) -> str:
    return target.get("currency") or catalog.currency_of(target.get("country") or catalog.DEFAULT_COUNTRY)


def rank(pool: list[dict], target: dict, weights: Optional[dict] = None, band_pct: float = DEFAULT_BAND_PCT,
         tier_window: Optional[int] = None, top: int = 20, now: Optional[datetime] = None) -> dict:
    """Rank rival models for `target` = {sub, price, country|currency, specs{width_in, capacity_total_cuft, fuel,
    burners, features[]}}. `tier_window` keeps only models within +-N price tiers of the target price."""
    now = now or datetime.now()
    pool, distrusted = distrust_blanket_new_flags(pool)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    currency, sub = _currency_of(target), target.get("sub")
    major = catalog.major_of(sub) if sub else None
    same_cur = [r for r in pool if (r.get("currency") or "USD") == currency]
    scope = [r for r in same_cur if not sub or r["sub"] == sub]
    tier_rows, tier_scope = scope, f"소분류({sub or '전체'})"
    if len([r for r in scope if row_price(r) is not None]) < MIN_TIER_POOL and major:
        tier_rows, tier_scope = [r for r in same_cur if r.get("major") == major], f"대분류({catalog.label_ko(major)})"
    prices = [p for p in map(row_price, tier_rows) if p is not None]
    edges = price_tiers(prices)
    n_tiers = len(edges) + 1
    t_price = target.get("price")
    t_tier = tier_of(t_price, edges) if (t_price is not None and prices) else None
    prior = prior_rating(scope)
    specs_wanted = any(v for v in (target.get("specs") or {}).values())

    results, excluded_hard, outside_tier, unpriced = [], 0, 0, 0
    for r in scope:
        facts = _facts(r)
        spec = spec_fit(target.get("specs") or {}, facts)
        if spec["hard_fail"]:
            excluded_hard += 1
            continue
        p = row_price(r)
        if t_price and p is None:  # "similar price" cannot be judged without a price: counted, not ranked
            unpriced += 1
            continue
        tier = tier_of(p, edges) if (p is not None and prices) else None
        tier_diff = (tier - t_tier) if (tier is not None and t_tier is not None) else None
        if tier_window is not None and tier_diff is not None and abs(tier_diff) > tier_window:
            outside_tier += 1
            continue
        price_score = proximity(p, t_price, band_pct) if (p is not None and t_price) else None
        rec = recency({**r, "attrs": facts}, now)
        resp = response(facts, prior)
        comps = {"price": price_score, "spec": spec["score"], "recency": rec["score"], "response": resp["score"]}
        # unknown is neither rewarded nor punished like a bad value: it scores NEUTRAL, and coverage tells how much of
        # the verdict rests on real data (renormalising over known parts would let empty rows beat good ones)
        used = {k: w[k] for k in comps if k not in ("price", "spec") or (k == "price" and t_price) or (k == "spec" and specs_wanted)}
        known_w = sum(w[k] for k in used if comps[k] is not None)
        total = (sum(w[k] * (comps[k] if comps[k] is not None else NEUTRAL[k]) for k in used) / sum(used.values()) * 100
                 if used else None)
        results.append({
            "brand": r["brand"], "model_number": r["model_number"], "name": r["name"], "url": r["url"], "sub": r["sub"],
            "country": r["country"], "price": p, "currency": r.get("currency"), "tier": None if tier is None else tier + 1,
            "tier_label": None if tier is None else tier_label(tier, n_tiers), "tier_diff": tier_diff,
            "total": None if total is None else round(total, 1), "coverage": round(known_w / sum(used.values()), 2) if used else 0.0,
            "components": {"price": {"score": price_score, "delta_pct": None if (p is None or not t_price) else round((p - t_price) / t_price * 100, 1)},
                           "spec": spec, "recency": rec, "response": resp},
            "is_launch": is_launch({**r, "attrs": facts}, now)})
    results.sort(key=lambda x: (x["total"] is not None, x["total"] or 0, x["components"]["price"]["score"] or 0,
                                x["components"]["response"].get("reviews") or 0), reverse=True)
    return {"target": {**target, "currency": currency, "tier": None if t_tier is None else t_tier + 1,
                       "tier_label": None if t_tier is None else tier_label(t_tier, n_tiers)},
            "distrusted_new_flags": distrusted,
            "tiers": tier_ranges(prices, edges), "tier_scope": tier_scope, "weights": w, "band_pct": band_pct,
            "counts": {"pool": len(scope), "ranked": len(results), "excluded_by_spec": excluded_hard,
                       "outside_tier_window": outside_tier, "price_unknown": unpriced},
            "results": results[:top]}


# ------------------------------------------------------------------ launch radar
def launches(pool: list[dict], now: Optional[datetime] = None, window_months: float = 12,
             sub: Optional[str] = None, currency: Optional[str] = None) -> dict:
    """New models per brand (with the evidence) and which features are more common among them than among the rest."""
    now = now or datetime.now()
    pool, distrusted = distrust_blanket_new_flags(pool)
    rows = [r for r in pool if (not sub or r["sub"] == sub) and (not currency or (r.get("currency") or "USD") == currency)]
    new_rows, old_rows = [], []
    by_brand: dict[str, list[dict]] = {}
    basis_counts: dict[str, int] = {}
    for r in rows:
        facts = _facts(r)
        rr = {**r, "attrs": facts}
        if is_launch(rr, now, window_months):
            rec = recency(rr, now)
            new_rows.append(rr)
            basis_counts[rec["basis"]] = basis_counts.get(rec["basis"], 0) + 1
            by_brand.setdefault(r["brand"], []).append({
                "model_number": r["model_number"], "name": r["name"], "url": r["url"], "sub": r["sub"],
                "price": row_price(r), "currency": r.get("currency"), "basis": rec["basis"], "evidence": rec["evidence"],
                "rating": facts.get("rating"), "review_count": facts.get("review_count")})
        else:
            old_rows.append(rr)

    def share(group: list[dict], feat: str) -> dict:
        known = [g["attrs"][feat] for g in group if feat in g["attrs"]]
        yes = sum(1 for v in known if v)
        return {"known": len(known), "yes": yes, "pct": round(100 * yes / len(known), 1) if known else None}

    def median(group: list[dict]) -> Optional[float]:
        prices = [p for p in map(row_price, group) if p is not None]
        return statistics.median(prices) if prices else None

    trend = []
    for feat in FEATURES:
        n, o = share(new_rows, feat), share(old_rows, feat)
        trend.append({"feature": feat, "new": n, "existing": o,
                      "delta_pts": None if (n["pct"] is None or o["pct"] is None) else round(n["pct"] - o["pct"], 1),
                      "low_sample": n["known"] < 3 or o["known"] < 3})
    return {"window_months": window_months, "totals": {"models": len(rows), "new": len(new_rows)},
            "basis_counts": basis_counts, "distrusted_new_flags": distrusted,
            "by_brand": {b: sorted(v, key=lambda x: x["model_number"]) for b, v in sorted(by_brand.items())},
            "trend": sorted(trend, key=lambda t: -(t["delta_pts"] if t["delta_pts"] is not None else -999)),
            "median_price": {"new": median(new_rows), "existing": median(old_rows)},
            "note": "출시 시점은 사이트 표시, 사이트가 밝힌 날짜, 앱의 최초 발견 시점을 근거로 한 추정입니다. "
                    "앱이 처음 조회한 모델은 기준선으로 보아 신제품에서 제외됩니다."}
