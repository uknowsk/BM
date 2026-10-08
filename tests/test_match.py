"""Plain-assert tests (pytest-compatible). Run: python tests/test_match.py
Competitor match engine (5 price tiers + proximity, recency, response, launch radar) and the discovery history."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import match
from catalog import Candidate
from store import Store

NOW = datetime(2026, 10, 7)


def row(model, price, sub="electric_oven", brand="GE", attrs=None, name=None, first_seen="2026-01-01T00:00:00",
        baseline=1, currency="USD"):
    return {"url": f"https://x.test/{brand}/{model}", "brand": brand, "country": "us", "sub": sub, "major": "cooking",
            "model_number": model, "name": name or f"30 in. Single Wall Oven {model}", "price_usd": price,
            "price_local": None, "currency": currency, "attrs": attrs or {}, "first_seen": first_seen,
            "last_seen": first_seen, "baseline": baseline}


# ------------------------------------------------------------------ price: 5 tiers + proximity
def test_five_tiers_over_quantiles_and_boundaries():
    prices = [float(p) for p in range(100, 2100, 100)]  # 20 prices
    edges = match.price_tiers(prices)
    assert len(edges) == 4 and edges == sorted(edges)
    tiers = [match.tier_of(p, edges) for p in prices]
    assert tiers[0] == 0 and tiers[-1] == 4 and sorted(tiers) == tiers
    assert all(tiers.count(t) == 4 for t in range(5))  # 20 prices -> 4 per tier
    assert match.tier_of(edges[0], edges) == 1  # a price on a cut belongs to the higher tier
    rng = match.tier_ranges(prices, edges)
    assert [r["label"] for r in rng] == match.TIER_LABELS and sum(r["count"] for r in rng) == 20


def test_fewer_prices_make_fewer_tiers_never_fake_five():
    edges = match.price_tiers([100.0, 200.0, 300.0])
    assert len(edges) == 2 and match.tier_label(0, 3) == "1/3단계"
    assert match.price_tiers([100.0]) == [] and match.tier_of(100.0, []) == 0


def test_proximity_is_continuous_and_reaches_zero_at_the_band():
    assert match.proximity(1000, 1000) == 1.0
    assert abs(match.proximity(875, 1000, 25) - 0.25) < 1e-9
    assert match.proximity(750, 1000, 25) == 0.0 and match.proximity(1300, 1000, 25) == 0.0
    assert match.proximity(900, 1000, 25) > match.proximity(850, 1000, 25) > match.proximity(800, 1000, 25)
    assert match.proximity(1000, 0) == 0.0


# ------------------------------------------------------------------ spec fit
def test_spec_fit_hard_width_unknown_is_not_penalised_and_features():
    ok = match.spec_fit({"width_in": 30}, {"width_in": 30.0})
    assert ok["score"] == 1.0 and not ok["hard_fail"]
    far = match.spec_fit({"width_in": 30}, {"width_in": 36.0})
    assert far["hard_fail"]
    unknown = match.spec_fit({"width_in": 30, "features": ["air_fry"]}, {})
    assert unknown["score"] is None and not unknown["hard_fail"] and all(d["score"] is None for d in unknown["details"])
    mixed = match.spec_fit({"features": ["air_fry", "steam"]}, {"air_fry": True, "steam": False})
    assert mixed["score"] == 0.5
    fuel = match.spec_fit({"fuel": "gas"}, {"fuel": "dual_fuel"})
    assert fuel["score"] == 0.5
    assert match.spec_fit({"burners": 5}, {"burners": 4})["score"] == 0.5


# ------------------------------------------------------------------ recency
def test_recency_basis_order_and_first_seen_baseline_rule():
    new = match.recency({"attrs": {"release_date": "2026-04", "release_src": "site"}}, NOW)
    assert new["basis"] == "release_date" and new["score"] == 1.0 and new["months"] < 12
    old = match.recency({"attrs": {"release_date": "2022"}}, NOW)
    assert old["score"] is not None and old["score"] < 0.5
    weak = match.recency({"attrs": {"release_date": "2026-04", "release_src": "sitemap"}}, NOW)
    assert abs(weak["score"] - 0.6) < 1e-9
    assert match.recency({"attrs": {"is_new": True}}, NOW)["basis"] == "site_new"
    seen = match.recency({"attrs": {}, "baseline": 0, "first_seen": "2026-08-01T00:00:00"}, NOW)
    assert seen["basis"] == "first_seen" and seen["score"] == 0.9
    base = match.recency({"attrs": {}, "baseline": 1, "first_seen": "2026-08-01T00:00:00"}, NOW)
    assert base["basis"] == "unknown" and base["score"] is None  # first look = baseline, never "new"


def test_site_order_is_the_weakest_basis_and_needs_a_real_list():
    def at(rank, of=20, **extra):
        return match.recency({"attrs": {"newest_rank": rank, "newest_of": of, **extra}}, NOW)
    top, mid, last = at(1), at(12), at(20)
    assert top["basis"] == "site_order" and top["months"] is None and "1/20" in top["evidence"]
    assert top["score"] > mid["score"] > last["score"] and top["score"] < 1.0  # never as strong as a real date / site NEW
    assert at(3, release_date="2020")["basis"] == "release_date" and at(3, is_new=True)["basis"] == "site_new"
    assert match.recency({"attrs": {"newest_rank": 1, "newest_of": 4}}, NOW)["basis"] == "unknown"  # list too short to rank
    assert match.recency({"attrs": {"newest_rank": 30, "newest_of": 20}}, NOW)["basis"] == "unknown"  # inconsistent values
    assert match.recency({"attrs": {"newest_rank": 1, "newest_of": 20}, "baseline": 0, "first_seen": "2026-08-01T00:00:00"},
                         NOW)["basis"] == "first_seen"
    assert not match.is_launch({"attrs": {"newest_rank": 1, "newest_of": 20}}, NOW)  # a rank alone never makes a "launch"


def test_stamp_newest_order_records_position_in_the_given_order():
    import catalog
    cands = [Candidate(brand="X", model_number=f"M{i}", name=f"n{i}", url=f"https://x.test/{i}", attrs={"wifi": True},
                       attrs_src={"wifi": "name"}) for i in range(3)]
    out = catalog.stamp_newest_order(cands)
    assert [(c.attrs["newest_rank"], c.attrs["newest_of"]) for c in out] == [(1, 3), (2, 3), (3, 3)]
    assert out[0].attrs["wifi"] is True and out[0].attrs_src["newest_rank"] == "listing" and out[0].attrs_src["wifi"] == "name"
    assert Candidate(brand="X", model_number="Z", name="z", url="https://x.test/z").attrs == {}  # no shared default dict


# ------------------------------------------------------------------ consumer response
def test_response_is_bayesian_few_reviews_cannot_beat_many():
    prior = 4.2
    few = match.response({"rating": 5.0, "review_count": 3}, prior)
    many = match.response({"rating": 4.6, "review_count": 500}, prior)
    assert many["score"] > few["score"] and few["adjusted"] < 4.6
    assert match.response({}, prior)["score"] is None and match.response({"rating": 4.5}, prior)["score"] is None
    assert match.prior_rating([row("A", 1, attrs={"rating": 4.0, "review_count": 10})]) == 4.0
    assert match.prior_rating([row("A", 1)]) == 4.2


# ------------------------------------------------------------------ ranking
def _pool():
    pool = [row(f"M{i:02d}", 1000 + i * 100, brand="GE") for i in range(10)]  # 1000..1900, no signals
    pool.append(row("BEST", 1500, brand="LG", attrs={"release_date": "2026-05", "release_src": "site", "rating": 4.6,
                                                     "review_count": 400, "width_in": 30.0}))
    pool.append(row("OLD", 1500, brand="Bosch", attrs={"release_date": "2021", "rating": 4.6, "review_count": 400,
                                                       "width_in": 30.0}))
    pool.append(row("WIDE", 1500, brand="Smeg", attrs={"width_in": 36.0}, name="36 in. Wall Oven WIDE"))
    pool.append(row("GAS", 1500, sub="gas_oven", brand="Viking"))
    pool.append(row("EUR", 1500, brand="Miele", currency="EUR"))
    return pool


def test_rank_orders_by_proximity_recency_and_response_with_evidence():
    out = match.rank(_pool(), {"sub": "electric_oven", "price": 1500, "country": "us", "specs": {"width_in": 30}}, now=NOW)
    names = [r["model_number"] for r in out["results"]]
    assert names[0] == "BEST" and names.index("BEST") < names.index("OLD")
    assert "WIDE" not in names and out["counts"]["excluded_by_spec"] == 1  # 36 in vs 30 in: hard exclusion
    assert "GAS" not in names and "EUR" not in names  # other sub group / other currency never mix
    best = out["results"][0]
    assert best["components"]["recency"]["basis"] == "release_date" and best["components"]["response"]["reviews"] == 400
    assert best["components"]["price"]["delta_pct"] == 0.0
    assert out["target"]["tier"] is not None and len(out["tiers"]) == 5 and out["tier_scope"].startswith("소분류")
    assert all(0 <= r["coverage"] <= 1 for r in out["results"])


def test_rank_unknown_data_is_not_scored_zero_and_coverage_shows_it():
    out = match.rank([row("PLAIN", 1500)], {"sub": "electric_oven", "price": 1500, "specs": {}}, now=NOW)
    r = out["results"][0]
    assert r["components"]["recency"]["score"] is None and r["components"]["response"]["score"] is None
    # only price is known (specs were not asked): unknown parts score NEUTRAL, coverage says half rests on data
    assert r["total"] == 68.6 and r["coverage"] == 0.5
    good = match.rank([row("PLAIN", 1500), row("GOOD", 1500, attrs={"release_date": "2026-05", "rating": 4.6,
                                                                    "review_count": 400})],
                      {"sub": "electric_oven", "price": 1500, "specs": {}}, now=NOW)
    assert [x["model_number"] for x in good["results"]] == ["GOOD", "PLAIN"]  # empty rows must not beat proven ones


def test_rank_leaves_unpriced_models_out_but_counts_them():
    pool = [row("P1", 1500), row("P2", 1550), row("NOPRICE", None)]
    out = match.rank(pool, {"sub": "electric_oven", "price": 1500, "specs": {}}, now=NOW)
    assert [r["model_number"] for r in out["results"]] == ["P1", "P2"] and out["counts"]["price_unknown"] == 1
    # without a target price there is nothing to compare, so unpriced models stay
    assert match.rank(pool, {"sub": "electric_oven", "specs": {}}, now=NOW)["counts"]["price_unknown"] == 0


def test_blanket_site_new_flags_are_ignored_but_selective_ones_count():
    blanket = [row(f"B{i}", 1500, brand="Bertazzoni", attrs={"is_new": True}) for i in range(6)]  # 6 of 6 "new"
    selective = [row(f"S{i}", 1500, brand="LG", attrs={"is_new": True} if i == 0 else {}) for i in range(8)]  # 1 of 8
    cleaned, bad = match.distrust_blanket_new_flags(blanket + selective)
    assert bad == ["Bertazzoni electric_oven"]
    assert not any((r["attrs"] or {}).get("is_new") for r in cleaned if r["brand"] == "Bertazzoni")
    assert sum(1 for r in cleaned if (r["attrs"] or {}).get("is_new")) == 1  # LG's single flag survives
    small = [row(f"X{i}", 1, brand="Smeg", attrs={"is_new": True}) for i in range(3)]  # too few to judge
    assert match.distrust_blanket_new_flags(small)[1] == []
    out = match.launches(blanket + selective, now=NOW)
    assert out["totals"]["new"] == 1 and out["distrusted_new_flags"] == ["Bertazzoni electric_oven"]
    assert match.rank(blanket + selective, {"sub": "electric_oven", "price": 1500, "specs": {}}, now=NOW)["distrusted_new_flags"]


def test_rank_tier_window_limits_to_neighbouring_tiers():
    pool = [row(f"T{i:02d}", 500 + i * 100) for i in range(20)]
    wide = match.rank(pool, {"sub": "electric_oven", "price": 1000, "specs": {}}, top=50, now=NOW)
    narrow = match.rank(pool, {"sub": "electric_oven", "price": 1000, "specs": {}}, tier_window=0, top=50, now=NOW)
    assert narrow["counts"]["outside_tier_window"] > 0 and len(narrow["results"]) < len(wide["results"])
    assert {r["tier"] for r in narrow["results"]} == {narrow["target"]["tier"]}


def test_rank_small_sub_group_borrows_tiers_from_the_major():
    pool = [row(f"G{i}", 1000 + i * 50, sub="gas_oven") for i in range(3)] + [row(f"E{i:02d}", 400 + i * 80) for i in range(15)]
    out = match.rank(pool, {"sub": "gas_oven", "price": 1050, "specs": {}}, now=NOW)
    assert out["tier_scope"].startswith("대분류") and len(out["results"]) == 3


# ------------------------------------------------------------------ launch radar
def test_launch_radar_groups_by_brand_with_evidence_and_trend():
    pool = []
    for i in range(4):
        pool.append(row(f"N{i}", 1500, brand="LG", attrs={"release_date": "2026-03", "release_src": "site", "air_fry": True}))
    pool.append(row("N-site", 1600, brand="GE", attrs={"is_new": True, "air_fry": True}))
    pool.append(row("N-seen", 1700, brand="Smeg", baseline=0, first_seen="2026-09-01T00:00:00", attrs={"air_fry": False}))
    for i in range(6):
        pool.append(row(f"O{i}", 1400, brand="Bosch", attrs={"release_date": "2020", "air_fry": i < 1}))
    pool.append(row("BASE", 1400, brand="Viking", baseline=1))  # first look: neither new nor old evidence
    out = match.launches(pool, now=NOW)
    assert out["totals"]["new"] == 6 and set(out["by_brand"]) == {"LG", "GE", "Smeg"}
    assert out["basis_counts"] == {"release_date": 4, "site_new": 1, "first_seen": 1}
    af = next(t for t in out["trend"] if t["feature"] == "air_fry")
    assert af["new"]["known"] == 6 and af["new"]["yes"] == 5 and af["existing"]["yes"] == 1
    assert af["delta_pts"] > 0 and not af["low_sample"] and out["trend"][0]["feature"] == "air_fry"
    assert out["by_brand"]["Smeg"][0]["basis"] == "first_seen" and "추정" in out["note"]


# ------------------------------------------------------------------ discovery history (store)
def _c(model, sub="electric_oven", brand="GE"):
    return Candidate(brand=brand, model_number=model, name=f"Oven {model}", url=f"https://x.test/{model}", price_usd=1000.0,
                     category="cooking", subcategory=sub, attrs={})


def test_history_truncated_listing_never_makes_old_models_look_new():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "c.db")
        assert s.record_seen([_c(f"A{i}") for i in range(5)], limit=5) == 0      # limit reached: not complete
        assert s.record_seen([_c(f"A{i}") for i in range(8)], limit=30) == 0     # bigger look: still baseline
        assert all(r["baseline"] == 1 for r in s.list_seen(sub="electric_oven"))
        assert s.record_seen([_c(f"A{i}") for i in range(8)] + [_c("NEW1")], limit=30) == 1  # genuinely new
        rows = {r["model_number"]: r for r in s.list_seen(sub="electric_oven")}
        assert rows["NEW1"]["baseline"] == 0 and rows["A0"]["baseline"] == 1 and len(rows) == 9
        assert s.record_seen([_c("NEW1")], limit=30) == 0  # seen again: not new twice


def test_history_merges_attrs_and_prices_and_filters():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "c.db")
        s.record_seen([_c("X1"), _c("Y1", sub="gas_oven", brand="LG")], limit=30)
        s.update_seen_attrs("https://x.test/X1", {"rating": 4.5, "review_count": 120})
        s.record_seen([_c("X1").model_copy(update={"attrs": {"width_in": 30.0}})], limit=30)  # relist keeps detail attrs
        x = s.list_seen(sub="electric_oven")[0]
        assert x["attrs"] == {"rating": 4.5, "review_count": 120, "width_in": 30.0}
        assert [r["model_number"] for r in s.list_seen(brands=["LG"])] == ["Y1"]
        assert len(s.list_seen(major="cooking")) == 2 and s.list_seen(country="kr") == []


def test_history_backfill_from_old_candidate_cache_is_all_baseline():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "c.db")
        s.put_candidates("GE", "cooking", [_c("OLD1"), _c("OLD2")], limit=30, subcategory="electric_oven")
        assert s.backfill_seen() == 2 and s.backfill_seen() == 0
        assert {r["baseline"] for r in s.list_seen()} == {1}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
