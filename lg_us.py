"""LG US adapter: refrigerators, washers/dryers and cooking (https://www.lg.com/us).

discover(): LG's listing is driven by a Coveo search backend. A short-lived token comes from
  GET /us/plp/api/coveo/v1/getAccessToken and the catalog from POST platform.cloud.coveo.com (plain HTTP, no browser).
  Every product carries a multi-valued `ec_category_code` (e.g. refrigerators, french_door, 3_door); SUB_RULES maps
  each catalog sub key to category codes (+ optional title regexes) and drives both the Coveo query and the
  classification of a PDP (`product.category` holds the same codes).
scrape(): PDP (Next.js) embeds everything in <script id="__NEXT_DATA__"> -> props.pageProps.productData
  (product, options, allInfo spec table, documents). Manuals come from the support page's __NEXT_DATA__.
  The spec table differs per product family; fridge-only fields are filled for refrigerators only and
  category-specific specs go to ProductRecord.extra_specs.
Pages are loaded with Playwright; FRIDGE_BROWSER_MODE = auto (headless, fall back to visible when blocked)
| headless | visible.
"""
import json
import os
import re
import sys
import time
from typing import NamedTuple, Optional
from urllib.parse import urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from catalog import Candidate
from common import ROOT, UA, download_pdf, launch_browser, looks_blocked, num, pdf_text
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "LG"
BASE = "https://www.lg.com"
TOKEN_URL = BASE + "/us/plp/api/coveo/v1/getAccessToken"
COVEO_URL = "https://platform.cloud.coveo.com/rest/search/v2?organizationId=lgelectronicsusaproductionh9cyypz4"
MANUAL_DL = "https://gscs-b2c.lge.com/downloadFile?fileId="
MAX_DOCS = 5
DELAY_S = 1.0
COVEO_PAGE = 200      # Coveo's per-request cap
COVEO_MAX_PAGES = 10  # safety bound on paging through a category (2000 results)

PAGE_HOSTS = ("lg.com",)            # pages we navigate to / candidate product urls
PDF_HOSTS = ("lg.com", "lge.com")   # hosts seen serving LG PDFs (www.lg.com, lg.com, gscs-b2c.lge.com)

_working_headless: bool | None = None  # mode that worked last; only a PREFERRED ORDER for auto mode


class LGPageError(RuntimeError):
    """LG page/API did not have the expected structure (site changed or blocked)."""


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _lg_host(url: str) -> bool:
    return _host_in(url, PAGE_HOSTS)


def _pdf_url_ok(url: str) -> bool:
    return urlparse(url).scheme == "https" and _host_in(url, PDF_HOSTS)


def _check_final_url(url: str) -> None:
    if not _lg_host(url):
        raise LGPageError(f"unexpected host after navigation/request: {url[:120]}")


def _safe_name(m: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", m).strip(".")


# ---------------------------------------------------------------- sub-category rules
class _Rule(NamedTuple):
    major: str
    any_codes: tuple          # product must carry at least one of these ec_category_code values
    none_codes: tuple = ()    # ... and none of these
    title: Optional[str] = None      # regex the product title must match
    not_title: Optional[str] = None  # regex it must not match


_BUILT_IN = r"built[- ]?in"
_LAUNDRY_CENTER_CODES = ("washtower", "washer_dryer_combos")
_SCO_TITLE = r"combination|combo|speed oven"
_GAS_TITLE = r"(?<![a-z])gas(?![a-z])|dual fuel"

# sub key -> rule. Order is the classification priority (first match wins; specific before generic, so
# laundry_center precedes top_load/front_load). Edit the mapping here only.
# LG US sells only electric wall ovens, so gas_oven is its gas / dual-fuel ranges.
SUB_RULES: dict[str, _Rule] = {
    "built_in": _Rule("refrigerator", ("refrigerators",), title=_BUILT_IN),
    "french_door": _Rule("refrigerator", ("french_door",), not_title=_BUILT_IN),
    "side_by_side": _Rule("refrigerator", ("side_by_side",), not_title=_BUILT_IN),
    "top_freezer": _Rule("refrigerator", ("top_freezer",), not_title=_BUILT_IN),
    "bottom_freezer": _Rule("refrigerator", ("bottom_freezer",), not_title=_BUILT_IN),
    # ASSUMPTION: compact = LG's small single-door refrigerators (not columns / freezers).
    "compact": _Rule("refrigerator", ("single_door",), title="refrigerator", not_title=r"column|counter.depth|freezer"),
    # ASSUMPTION: laundry_center = WashTower (stacked single unit) + all-in-one WashCombo units.
    "laundry_center": _Rule("washer", ("washtower", "washer_dryer_combos")),
    "top_load": _Rule("washer", ("top_load",), none_codes=_LAUNDRY_CENTER_CODES),
    "front_load": _Rule("washer", ("front_load",), none_codes=_LAUNDRY_CENTER_CODES),
    "dryer": _Rule("washer", ("dryers",)),
    # ASSUMPTION: sco = Speed Cook Oven = microwave-combination wall ovens (combination_wall_ovens) and built-in
    # microwave speed ovens; before microwave / electric_oven so each product has one sub key. OTR units never match.
    "sco": _Rule("cooking", ("combination_wall_ovens", "wall_ovens", "microwaves"), none_codes=("over_the_range",),
                 title=_SCO_TITLE),
    "microwave": _Rule("cooking", ("microwaves",), none_codes=("over_the_range",)),
    "otr": _Rule("cooking", ("over_the_range",)),
    "induction": _Rule("cooking", ("ranges_induction", "cooktop_induction")),
    # ASSUMPTION: gas_oven = gas and dual-fuel ranges (LG sells no gas wall oven); gas_cooktop = oven-less gas
    # cooktops (category code cooktops_gas).
    "gas_oven": _Rule("cooking", ("ranges",), none_codes=("ranges_induction",), title=_GAS_TITLE),
    "gas_cooktop": _Rule("cooking", ("cooktops_gas",)),
    # ASSUMPTION: radiant = electric ranges (every non-gas, non-induction range) + electric cooktops.
    "radiant": _Rule("cooking", ("ranges", "cooktops_electric"), none_codes=("ranges_induction", "cooktop_induction"),
                     not_title=_GAS_TITLE),
    # ASSUMPTION: electric_oven = built-in single/double wall ovens without a microwave / speed-cook function.
    "electric_oven": _Rule("cooking", ("wall_ovens",), title=r"wall oven", not_title=_SCO_TITLE),
}
SUPPORTED_SUBCATEGORIES = set(SUB_RULES)

# family category code -> major key, used when a PDP matches no sub rule
_MAJOR_CODES = {"refrigerators": "refrigerator", "washers_and_dryers": "washer", "cooking_appliances": "cooking"}


def _rule_matches(rule: _Rule, codes: set[str], title: str) -> bool:
    return (any(c in codes for c in rule.any_codes) and not any(c in codes for c in rule.none_codes)
            and (rule.title is None or re.search(rule.title, title, re.I) is not None)
            and (rule.not_title is None or re.search(rule.not_title, title, re.I) is None))


def infer_subcategory(codes, title: str) -> Optional[str]:
    """Sub key for a product from its own category codes + title; None when no sub rule matches."""
    cs = {str(c) for c in codes or []}
    return next((sub for sub, rule in SUB_RULES.items() if _rule_matches(rule, cs, title or "")), None)


def _major_from_codes(codes) -> Optional[str]:
    cs = {str(c) for c in codes or []}
    return next((m for code, m in _MAJOR_CODES.items() if code in cs), None)


def _cq(rule: _Rule) -> str:
    any_ = " OR ".join(f'@ec_category_code=="{c}"' for c in rule.any_codes)
    parts = ['@ec_store_code="OBS"', "@ec_model_status_code===ACTIVE", "@ec_model_type==Product",
             any_ if len(rule.any_codes) == 1 else f"({any_})"]
    parts += [f'NOT @ec_category_code=="{c}"' for c in rule.none_codes]
    return " ".join(parts)


def _signals(rating, count, tags) -> dict:
    """Consumer-response / newness signals the site itself publishes (rating on a 5-point scale; the Coveo listing
    carries no review count); absent or empty values are left out."""
    out: dict = {}
    try:
        r = float(rating)
    except (TypeError, ValueError):
        r = None
    try:
        n = int(count) if count is not None else None
    except (TypeError, ValueError):
        n = None
    if r is not None and 0 < r <= 5 and (n is None or n > 0):
        out["rating"] = round(r, 2)
        if n:
            out["review_count"] = n
    if any(isinstance(t, str) and t.strip().lower() == "new" for t in tags or []):
        out["is_new"] = True
    return out


# ---------------------------------------------------------------- discover
def parse_listing(data: dict, limit: int, sub: Optional[str] = None, stats: Optional[dict] = None) -> list[Candidate]:
    """Coveo search response -> deduped Candidates (by model number); with `sub`, only products that
    infer_subcategory() (the rule scrape() uses) files under it. `stats["dropped"]` counts the rest."""
    results = data.get("results")
    if not isinstance(results, list):
        raise LGPageError("Coveo response has no 'results' list")
    rule = SUB_RULES[sub] if sub else None
    out, seen = [], set()
    for r in results:
        raw = r.get("raw", {})
        model = raw.get("ec_model_display_name") or (raw.get("ec_group_id") or [None])[0]
        path = raw.get("clickableuri") or ""
        if not model or not path or model in seen:
            continue
        name = raw.get("ec_user_friendly_name") or r.get("title") or model
        if rule and infer_subcategory(raw.get("ec_category_code"), name) != sub:
            if stats is not None:
                stats["dropped"] = stats.get("dropped", 0) + 1
            continue
        url = BASE + path if path.startswith("/") else path
        if not _lg_host(url):
            print(f"dropped off-domain candidate {model}: {url[:100]}", file=sys.stderr)
            continue
        seen.add(model)
        price = raw.get("ec_final_price")
        extra = {"category": rule.major, "subcategory": sub} if rule else {}
        attrs = _signals(raw.get("ec_s_rating"), None, str(raw.get("ec_default_product_tag") or "").split(";"))
        out.append(Candidate(brand=BRAND, model_number=model, name=name, url=url,
                             price_usd=float(price) if price else None,
                             attrs=attrs, attrs_src={k: "listing" for k in attrs}, **extra))
        if len(out) >= limit:
            break
    return out


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"LG US adapter does not support sub category {subcategory!r}")
    headers = {"User-Agent": UA}
    tok = requests.get(TOKEN_URL, headers=headers, timeout=30)
    tok.raise_for_status()
    _check_final_url(tok.url)
    token = tok.json().get("token")
    if not token:
        raise LGPageError("getAccessToken returned no token")
    body = {
        "locale": "en-US", "context": {"organization": "OBS"},
        "searchHub": "LG.com - Commerce - PDS - Listing", "sortCriteria": "relevancy",
        "cq": _cq(SUB_RULES[subcategory]),
        "fieldsToInclude": ["ec_model_display_name", "ec_user_friendly_name", "ec_final_price",
                            "clickableuri", "ec_group_id", "ec_category_code", "ec_s_rating", "ec_default_product_tag"],
        "numberOfResults": COVEO_PAGE,
    }
    found: dict[str, Candidate] = {}
    stats: dict = {}
    first, total, pages = 0, None, 0
    while len(found) < limit and (total is None or first < total) and pages < COVEO_MAX_PAGES:
        time.sleep(DELAY_S)
        r = requests.post(COVEO_URL, json={**body, "firstResult": first},
                          headers={**headers, "Authorization": "Bearer " + token}, timeout=30)
        r.raise_for_status()
        data = r.json()
        # the title / classification filters run after the query, so keep paging until limit or exhaustion
        for c in parse_listing(data, 10 ** 6, subcategory, stats):
            found.setdefault(c.model_number, c)
        n = len(data["results"])
        total = data.get("totalCount", first + n) if n else first
        first += n
        pages += 1
    exhausted = total is not None and first >= total
    print(f"lg_us: discover({subcategory}): scanned {first} of {total} Coveo results in {pages} request(s), "
          f"kept {min(len(found), limit)}, dropped {stats.get('dropped', 0)} by category/title filters"
          + ("" if exhausted or len(found) >= limit else " (STOPPED at the page cap; results incomplete)"),
          file=sys.stderr)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- page loading
_REJECT = ("Reject all", "Reject All", "Decline", "Decline all", "Reject non-essential")
_ACCEPT = ("Accept all", "Accept All", "Accept", "Allow all", "I agree")


def _consent(pg, accept: bool = False) -> bool:
    """Best-effort cookie banner click: decline non-essential by default, accept only when the page is blocked."""
    for label in (_ACCEPT if accept else _REJECT):
        try:
            pg.get_by_role("button", name=label, exact=True).first.click(timeout=1500)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _modes() -> list[bool]:
    """Headless flags to try, in order. Env is read on every call; the cached mode only reorders auto."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    first = True if _working_headless is None else _working_headless
    return [first, not first]


def _load_pages(urls: list[str]) -> list[str]:
    """HTML of each url in one browser. First url is required; the others are best-effort ('' on failure)."""
    global _working_headless
    last: Exception | None = None
    for headless in _modes():
        try:
            with sync_playwright() as p:
                b = launch_browser(p, headless=headless)
                try:
                    pg = b.new_context(user_agent=UA).new_page()
                    htmls = []
                    for i, url in enumerate(urls):
                        try:
                            if not _lg_host(url):
                                raise LGPageError(f"refusing non-LG url {url[:120]}")
                            resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                            _check_final_url(pg.url)
                            _consent(pg)
                            try:
                                pg.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=20000)
                            except PlaywrightTimeoutError:
                                _consent(pg, accept=True)  # banner may be blocking content; retry once
                                pg.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=20000)
                            _check_final_url(pg.url)
                            html, text = pg.content(), pg.inner_text("body")
                            if looks_blocked(resp.status if resp else None, text):
                                raise LGPageError(f"blocked ({resp.status if resp else '?'}) at {url}")
                            htmls.append(html)
                        except Exception as e:
                            if i == 0:
                                raise
                            print(f"skipped page {url}: {e}", file=sys.stderr)
                            htmls.append("")
                        time.sleep(DELAY_S)
                finally:
                    b.close()
            _working_headless = headless
            return htmls
        except Exception as e:  # blocked / nav failure -> try the next mode
            last = e
            print(f"LG load failed (headless={headless}): {e}", file=sys.stderr)
    raise LGPageError(f"could not load {urls[0]}: {last}")


# ---------------------------------------------------------------- parsing
def _next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise LGPageError("no __NEXT_DATA__ on page")
    return json.loads(m.group(1))


def _first_num(s: str | None) -> float | None:
    return num(r"(\d[\d,]*\.?\d*)", s or "")


def _inches(v: str | None) -> float | None:
    """'29 3/4"' | '26.843"' | '11/16"' | '27 in' -> float inches."""
    v = v or ""
    m = re.search(r"(\d+)\s+(\d+)/(\d+)", v)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.search(r"(?<![\d.])(\d+)/(\d+)", v)
    if m:
        return int(m.group(1)) / int(m.group(2))
    return _first_num(v)


def _sane_in(v: float | None) -> float | None:
    """LG occasionally publishes corrupt dimensions (e.g. '2914743.2"' depth); no appliance is over 200 in."""
    return v if v is not None and 0 < v <= 200 else None


def _wxhxd(v: str | None) -> tuple[float | None, float | None, float | None]:
    """'27" x 39" x 29 3/4"' -> (width, height, depth) inches; (None,)*3 when not three parts."""
    parts = re.split(r"\s*[xX×]\s*", v or "")
    if len(parts) < 3:
        return None, None, None
    return _inches(parts[0]), _inches(parts[1]), _inches(parts[2])


def _door_style(*texts: str) -> str | None:
    t = " ".join(texts).lower().replace("-", " ").replace("_", " ")
    for pat, name in ((r"french( \d)? door", "French Door"), (r"side by side", "Side-by-Side"),
                      (r"top freezer", "Top Freezer"), (r"bottom freezer", "Bottom Freezer"),
                      (r"single door", "Single Door")):
        if re.search(pat, t):
            return name
    return None


def _yes_no(v: str | None) -> bool | None:
    v = (v or "").strip().lower()
    return True if v.startswith("yes") else False if v in ("no", "none") else None


_WIFI_KEYS = ("Wi-Fi Enabled", "Wi-Fi", "Wi-Fi Connectivity", "ThinQ® Smart Technology", "ThinQ Care™")
_CAPACITY_KEYS = ("Total Capacity (cu.ft.)", "Total Capacity", "Capacity", "Washer Capacity", "Oven Capacity (cu. ft.)")
_WEIGHT_KEYS = ("Weight (Unit/Carton)", "Weight (Product)", "Weight (Product/Carton)", "Product Weight (lbs)")
_ELEC_KEYS = ("Washer Electrical Requirements", "Dryer Electrical Requirements", "Energy Requirements",
              "Electric Supply", "Ratings/Requirements/Type", "Required Power Supply (amp)", "Amps / Watts", "Volts")

# Curated category-specific specs: (label, LG spec keys (first present wins), "num" keeps only the number).
_NUM = "num"
_EXTRA: dict[str, list[tuple]] = {
    "refrigerator": [("Compressor type", ("Compressor Type",)), ("Refrigerant", ("Refrigerant",)),
                     ("Sabbath mode", ("Sabbath Mode",)), ("Dispenser type", ("Dispenser Type",)),
                     ("Ice storage capacity (lb)", ("Ice Storage Capacity (lbs)",), _NUM),
                     ("Water filter", ("Water Filter",)), ("Counter depth", ("Counter Depth",))],
    "washer": [("Product type", ("Type",)), ("Dryer capacity (cu ft)", ("Dryer Capacity",), _NUM),
               ("Max spin speed (rpm)", ("Max RPM",), _NUM), ("Motor type", ("Motor Type",)),
               ("Drum axis", ("Axis",)), ("Agitator", ("Agitator",)), ("Impeller", ("Impeller",)),
               ("Washer programs", ("No. of Washer Programs",), _NUM), ("Dryer programs", ("No. of Dryer Programs",), _NUM),
               ("Spin speeds", ("No. of Spin Speeds",), _NUM), ("Wash/rinse temperatures", ("No. of Wash/Rinse Temps",), _NUM),
               ("Soil levels", ("No. of Soil Levels",), _NUM), ("Steam", ("Steam Technology", "Steam")),
               ("IMEF", ("IMEF",), _NUM), ("IWF", ("IWF",), _NUM), ("CEF", ("CEF",), _NUM),
               ("Washer power source", ("Washer Power Source Type",)), ("Dryer power source", ("Dryer Power Source Type",)),
               ("Dryer BTU rating", ("Dryer BTU Rating",), _NUM), ("Vent type", ("Vent Type",)),
               ("Dispenser", ("Dispenser",)), ("Internal water heater", ("Internal Water Heater",))],
    "cooking": [("Product type", ("Type",)), ("Fuel type", ("Fuel Type", "Configuration")), ("Oven type", ("Oven Type",)),
                ("Upper oven capacity (cu ft)", ("Upper Oven Capacity (cu. ft.)",), _NUM),
                ("Lower oven capacity (cu ft)", ("Lower Oven Capacity (cu. ft.)",), _NUM),
                ("Convection type", ("Convection Type",)), ("Self clean", ("Self Clean",)),
                ("Oven cleaning", ("Oven Cleaning Type",)), ("Cooktop type", ("Cooktop Type", "Cooktop Burner Type")),
                ("Burners/elements", ("No. of Burners",), _NUM),
                ("Microwave power (W)", ("Output (Watts) Microwave",), _NUM),
                ("Power levels", ("Microwave Power Levels",), _NUM), ("Sensor cook options", ("Sensor Cook Options",), _NUM),
                ("Turntable diameter (in)", ("Turntable Diameter (in.)",), _NUM),
                ("Vent CFM", ("Vent Air Flow (CFM)",), _NUM), ("Filtration", ("Filtration",)),
                ("Control type", ("Oven Control Type", "Control Type", "Cooktop Control Type")),
                ("Sabbath mode", ("Sabbath Mode",)), ("Cutout dimensions", ("Cut-Out (WxHxD)",)),
                ("Cavity dimensions", ("Cavity (WxHxD)", "Interior (WxHxD)")), ("Amps/watts", ("Amps / Watts",)),
                ("Amps at 208V/240V", ("Amp at 208V / 240V",)), ("kW at 208V/240V", ("kW at 208V / 240V",)),
                ("Total connected load", ("Total Connected Load",))],
}


def _extra_specs(major: str, spec: dict[str, str], rows) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, keys, *kind in _EXTRA.get(major, []):
        v = next((spec[k] for k in keys if spec.get(k, "").strip()), None)
        if v is None:
            continue
        if kind and (n := _first_num(v)) is not None:
            v = str(int(n)) if float(n).is_integer() else str(n)
        out[label] = v.strip()
    if major == "cooking":
        detail = [(k, v) for _, k, v in rows if re.match(r"(Burner/BTU|Element Size/Wattage) - ", k)]
        if detail:
            out["Burner/element detail"] = "; ".join(f"{k.split(' - ', 1)[1]}: {v}" for k, v in detail)
            out.setdefault("Burners/elements", str(len(detail)))
    return out


def _spec_electrical(spec: dict[str, str]) -> dict:
    """voltage/amps/frequency from the spec table (non-fridge families list them there)."""
    text = " ; ".join(spec[k] for k in _ELEC_KEYS if spec.get(k))
    volts = re.search(r"(\d{3}(?:\s*/\s*\d{3})?)\s*(?:V\b|VAC)", text, re.I)
    if not volts and spec.get("Volts"):
        volts = re.match(r"\s*(\d{3})", spec["Volts"])
    amps = re.search(r"(\d+(?:\.\d+)?)\s*A(?:mps?)?\b", text)
    hz = re.search(r"(\d{2})\s*Hz", text, re.I) or re.search(r"/\s*(\d{2})\s*$", spec.get("Volts", ""))
    return {"voltage_v": re.sub(r"\s+", "", volts.group(1)) if volts else None,
            "amps": float(amps.group(1)) if amps else None,
            "frequency_hz": float(hz.group(1)) if hz else None}


_SUPERSCRIPT = re.compile(r"[ᶛ-ᶿ¶]+")


def _noise(t) -> str:
    """Strip registered/trademark/footnote marks; line breaks inside a value become ' | '."""
    lines = [" ".join(_SUPERSCRIPT.sub("", l.replace("®", "").replace("™", "")).split()) for l in str(t).splitlines()]
    return " | ".join(l for l in lines if l)


def full_spec_table(rows) -> dict[str, str]:
    """EVERY Specs row: 'Section > Label' -> full value (a repeated key keeps all values, ' | '-joined)."""
    out: dict[str, str] = {}
    for sec, k, v in rows:
        val = _noise(v)
        if not val:
            continue
        key = f"{_noise(sec)} > {_noise(k)}" if _noise(sec) else _noise(k)
        out[key] = f"{out[key]} | {val}" if key in out else val
    return out


IMAGE_HOSTS = ("lg.com", "lge.com")


def _lg_image_ok(url) -> bool:
    return isinstance(url, str) and urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS)


def main_image_url(option: dict | None) -> str | None:
    """Hero image of the selected option: first entry of productGroupAttributes.images (a 350px thumbnail url),
    upgraded to the 1600px zoom/1100px large rendition of the SAME asset (matching uuid) from the gallery."""
    pga = (option or {}).get("productGroupAttributes") or {}
    first = next((v for g in pga.get("images") or [] if isinstance(g, dict)
                  for k, v in g.items() if k.startswith("images_") and isinstance(v, dict)), None)
    thumb = (first or {}).get("image_url_asset")
    if not _lg_image_ok(thumb):
        return None
    m = re.search(r"/([0-9a-f]{8}-[0-9a-f-]{27})/", thumb)
    if m:
        for fam in pga.get("common_gallery_asset") or []:
            for k, v in (fam.items() if isinstance(fam, dict) else []):
                if k.startswith("gallery_") and isinstance(v, dict):
                    for field in ("zoom_image_addr_asset", "large_image_addr_asset"):
                        if m.group(1) in str(v.get(field)) and _lg_image_ok(v.get(field)):
                            return v[field]
    return thumb


def parse_pdp(html: str, url: str):
    """-> (ProductRecord, [RawSpec], {doc_type: url}, support_page_url|None). Raises LGPageError if unrecognised,
    ValueError for a product family this adapter does not cover (e.g. dishwashers, TVs)."""
    pd = _next_data(html).get("props", {}).get("pageProps", {}).get("productData")
    prod = (pd or {}).get("product")
    if not prod or not prod.get("sku") or not pd.get("allInfo"):
        raise LGPageError("PDP __NEXT_DATA__ lacks productData.product/allInfo (structure changed?)")
    model = prod["sku"]
    title = prod.get("title", model).strip()
    codes = prod.get("category") or []
    sub = infer_subcategory(codes, title)
    major = SUB_RULES[sub].major if sub else _major_from_codes(codes)
    if not major:
        raise ValueError(f"not a supported LG product (categories: {list(codes)[:6]})")
    fridge = major == "refrigerator"
    rows = [(s.get("subtitle", ""), t["term"], str(t["description"]))
            for s in pd["allInfo"] for t in s.get("tableData", []) if t.get("term")]
    spec = {}
    for _, k, v in sorted(rows, key=lambda r: r[0] != "Summary"):  # Summary first: bare keys like "Type" repeat per section
        spec.setdefault(k, v)
    get = lambda *keys: next((spec[k] for k in keys if k in spec), None)

    option = next((o for o in pd.get("options", []) if o.get("sku") == model and o.get("variantDescription")), None)
    feats = [re.sub(r"[ᶛ-ᶿ¶]+", "", f["feature"]).strip() for f in prod.get("keyFeatures", [])
             if f.get("feature") and not re.search(r"purchased through|Terms apply", f["feature"])]
    ice_keys = ("Dual Ice", "Ice System", "Craft Ice™ Daily Ice Production")
    ice_rows = [v for k, v in spec.items() if "ice maker" in k.lower() or k in ice_keys]
    disp = get("Dispenser Type")
    wifi_key = next((k for k in _WIFI_KEYS if _yes_no(spec.get(k)) is not None), None)
    wifi = _yes_no(spec[wifi_key]) if wifi_key else None
    es_key = next((k for k in spec if re.search(r"energy star.*(qualified|certified)", k, re.I)), None)

    if fridge:
        width, height, depth = (_first_num(get("Width")), _first_num(get("Height to Top of Door Hinge", "Height to Top of Case")),
                                _first_num(get("Depth without Handles", "Depth with Handles")))
    else:
        width, height, depth = _wxhxd(get("Product (WxHxD)", "Exterior (WxHxD)"))
        width = width or _inches(get("Width", "Overall Width (in)", "Cabinet Width (in)"))
        height = height or _inches(get("Height", "Height to Cooking Surface (in)"))
        depth = depth or _inches(get("Depth", "Overall Depth (in)", "Overall Depth (in) - including handle"))
    width, height, depth = (_sane_in(v) for v in (width, height, depth))
    elec = _spec_electrical(spec) if not fridge else {}

    review = prod.get("review") if isinstance(prod.get("review"), dict) else {}
    product = ProductRecord(
        brand=BRAND, model_number=model, product_name=title, product_url=url,
        category=major, subcategory=sub,
        **_signals(review.get("points"), review.get("reviewers"), prod.get("promotionTags")),
        door_style=_door_style(title, prod.get("pdpUrl", ""), " ".join(codes)) if fridge else None,
        finish_color=option["variantDescription"] if option else get("All Available Colors", "Product Color"),
        price_usd=(prod.get("price", {}).get("finalPrice", {}).get("value") or None),
        capacity_total_cuft=_first_num(get(*_CAPACITY_KEYS)),
        capacity_fridge_cuft=_first_num(get("Refrigerator (cu.ft.)")) if fridge else None,
        capacity_freezer_cuft=_first_num(get("Freezer (cu.ft.)")) if fridge else None,
        width_in=width, height_in=height, depth_in=depth,
        weight_lb=_first_num(get(*_WEIGHT_KEYS)),  # unit weight; source text can be malformed ("249bs / 271 lbs")
        voltage_v=elec.get("voltage_v"), amps=elec.get("amps"), frequency_hz=elec.get("frequency_hz"),
        energy_kwh_year=_first_num(get("Energy Consumption (kWh/Year)")) if fridge else None,
        energy_star=_yes_no(spec[es_key]) if es_key else None,
        ice_maker=(True if (any(v.strip() and _yes_no(v) is not False for v in ice_rows)
                            or any(re.search(r"\bice makers?\b", f, re.I) and not re.search(r"\bno ice maker", f, re.I)
                                   for f in feats)) else None) if fridge else None,
        water_dispenser=(None if disp is None else (False if disp.strip().lower() in ("none", "no") else True)) if fridge else None,
        wifi_supported=wifi,
        wifi_evidence=f"LG spec table: {wifi_key} = {spec[wifi_key]}" if wifi_key else None,
        pod_features=feats,
        extra_specs={**full_spec_table(rows), **_extra_specs(major, spec, rows)},
        image_url=main_image_url(next((o for o in pd.get("options", []) if o.get("sku") == model), None)),
    )
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=k, value=v) for s, k, v in rows]

    links: dict[str, str] = {}
    docs = pd.get("documents") or {}
    if ".pdf" in (docs.get("energyGuideLink") or "").lower():
        links["EnergyGuide"] = docs["energyGuideLink"]
    if docs.get("specSheetLink"):
        links["SpecSheet"] = docs["specSheetLink"]
    support = None
    for sec in pd.get("ownerSection", []):
        if sec.get("title") == "Manuals and Downloads":
            support = sec.get("url")
        wty = re.search(r'href="(https://[^"]+\.pdf)"', (sec.get("info") or {}).get("wtyPlcyDesc", ""))
        if wty:
            links["Warranty"] = wty.group(1)
    return product, raw, links, support


def parse_support_manuals(html: str) -> dict[str, str]:
    """English PDF manuals from the support page's manualData -> {doc_type: url}."""
    if not html:
        return {}
    md = _next_data(html).get("props", {}).get("pageProps", {}).get("manualData") or {}
    out: dict[str, str] = {}
    for e in md.get("manualList", []):
        name = e.get("originalFileName") or ""
        if e.get("fileType") != "PDF" or not e.get("fileName") or "English" not in (e.get("fileNamePrint") or ""):
            continue
        dt = "Installation" if re.search(r"install", name, re.I) else "Manual"
        out.setdefault(dt, MANUAL_DL + e["fileName"])
    return out


def _electrical(manual: str) -> dict:
    volts = re.search(r"(\d{3}) ?V(?:olts)?\b[^\n]{0,12}\b60 ?Hz", manual)
    return {"voltage_v": volts.group(1) if volts else None,
            "frequency_hz": 60.0 if volts else None,
            "amps": num(r"(\d+) ?amps? minimum", manual, re.I)}


def _apply_manual_electrical(product, manual: str) -> None:
    """Fill voltage/amps/frequency from the manual only where the record has none: spec-table values
    (non-fridge families) are authoritative and never overridden."""
    for k, v in _electrical(manual).items():
        if v is not None and getattr(product, k) is None:
            setattr(product, k, v)


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_final_url(url)
    pdp_html = _load_pages([url])[0]
    product, raw, links, support = parse_pdp(pdp_html, url)
    if support:
        try:
            links = {**parse_support_manuals(_load_pages([support])[0]), **links}
        except Exception as e:  # manuals are optional
            print(f"skipped support page ({support}): {e}", file=sys.stderr)
    docs, texts = [], {}
    for dt, link in list(links.items())[:MAX_DOCS]:
        if not _pdf_url_ok(link):
            print(f"skipped {dt}: PDF url not https on an allowed host ({link[:100]})", file=sys.stderr)
            continue
        d = download_pdf(BRAND, _safe_name(product.model_number), dt, link)
        if d:
            docs.append(d)
            texts[dt] = pdf_text(ROOT / d.local_path)
    if "Manual" in texts:
        _apply_manual_electrical(product, texts["Manual"])
    return product, docs, raw
