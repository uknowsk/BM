"""Samsung US adapter: refrigerators, laundry and cooking (contract: catalog.py).

Data sources (no HTML scraping of rendered DOM needed):
  discover: POST sribsrch.ecom.samsung.com/estoresearch-api/v1/scom/us/pf_search (the listing page's own XHR, JSON).
            category_code 08030000 fridges, 08010000 laundry (washers, dryers, sets), 08080000 cooking (ranges,
            cooktops, wall ovens, hoods), 08110000 microwaves. One code spans several sub keys, so every result is
            classified by its pdpURL path / ial4_code / title (classify()).
  scrape:   PDP <script id="__NEXT_DATA__"> (product, price, groupId)
            + GET www.samsung.com/us/gapi/v1/bridge/cacheable/bridge-data?data_type=Specs,Support (spec table, PDF links)
PDP HTML is fetched with plain requests; Playwright (headless, then visible) is only a fallback if blocked.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

import requests

import catalog
from catalog import Candidate
from common import ROOT, UA, download_pdf, launch_browser, looks_blocked, num, pdf_text
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Samsung"
BASE = "https://www.samsung.com"
SEARCH_URL = "https://sribsrch.ecom.samsung.com/estoresearch-api/v1/scom/us/pf_search"
BRIDGE_URL = BASE + "/us/gapi/v1/bridge/cacheable/bridge-data"
PAGE_SIZE = 14
PAGE_DELAY_S = 0.6
PAGE_CACHE_TTL_S = 120.0  # pf_search pages reused across discover() calls in one process (3 fridge sub keys share one code)
MAX_PDFS = 5
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
DOOR_STYLES = {"french-door": "French Door", "side-by-side": "Side-by-Side",
               "top-freezer": "Top Freezer", "bottom-freezer": "Bottom Freezer"}


class SamsungPageError(RuntimeError):
    """Raised when a Samsung page/API does not have the expected structure."""


def _model(code: str) -> str:
    return code.replace("/", "").strip()


def _safe_model(model: str) -> str:
    """Model number safe to use in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model).strip(".")


def _is_samsung_url(url: str) -> bool:
    """https URL whose host is samsung.com or a subdomain (exact, dot-anchored suffix match)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and (host == "samsung.com" or host.endswith(".samsung.com"))


# PDF hosts actually observed in Samsung bridge data: images.samsung.com, org.downloadcenter.samsung.com.
_pdf_url_ok = _is_samsung_url


def _require_samsung(url: str) -> None:
    if not _is_samsung_url(url):
        raise SamsungPageError(f"unexpected non-Samsung final URL: {url!r}")


# ---------------------------------------------------------------- sub-category map
_FRIDGE, _LAUNDRY, _COOK, _MICRO = "08030000", "08010000", "08080000", "08110000"

# sub key -> (major, [pf_search category codes]). Edit the mapping here only.
# Not offered (Samsung US lists none): bottom_freezer, built_in, compact fridges. Range hoods,
# upright freezers, bundles/F- sets are skipped.
# ASSUMPTIONS: sco = combi wall ovens (microwave + oven, leaf 08080103 / 'combi' in the name); electric_oven = single/
# double wall ovens (all electric); gas_oven = gas ranges (leaf 08080201 / 'gas' in the name); radiant = electric
# ranges (leaf 08080202) + electric cooktops; induction = induction ranges + induction cooktops (Samsung US sells
# no gas wall oven, no dual-fuel range and no standalone speed oven); gas_cooktop = gas cooktops (leaf 08080301,
# no oven; Samsung US sells no rangetop);
# laundry_center = stacked Laundry Hub + all-in-one washer-dryer combo (single model numbers only).
SUB_SOURCES: dict[str, tuple[str, list[str]]] = {
    "french_door": ("refrigerator", [_FRIDGE]),
    "side_by_side": ("refrigerator", [_FRIDGE]),
    "top_freezer": ("refrigerator", [_FRIDGE]),
    "top_load": ("washer", [_LAUNDRY]),
    "front_load": ("washer", [_LAUNDRY]),
    "dryer": ("washer", [_LAUNDRY]),
    "laundry_center": ("washer", [_LAUNDRY]),
    "microwave": ("cooking", [_MICRO]),
    "otr": ("cooking", [_MICRO]),
    "sco": ("cooking", [_COOK]),
    "gas_oven": ("cooking", [_COOK]),
    "gas_cooktop": ("cooking", [_COOK]),
    "electric_oven": ("cooking", [_COOK]),
    "induction": ("cooking", [_COOK]),
    "radiant": ("cooking", [_COOK]),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)

_FRIDGE_PATHS = {"refrigerators/french-door": "french_door", "refrigerators/side-by-side": "side_by_side",
                 "refrigerators/top-freezer": "top_freezer"}
_INDUCTION_RANGE, _INDUCTION_TOP, _RADIANT_TOP, _GAS_TOP = "08080204", "08080303", "08080304", "08080301"
_GAS_RANGE, _ELECTRIC_RANGE, _COMBI_WALL_OVEN = "08080201", "08080202", "08080103"
_SCO_NAME = re.compile(r"combi(?![a-z])|combination|speed oven|with microwave")


def classify(path_key: str, name: str = "", leaf: str = "", model: str = "") -> str | None:
    """Sub key for a product, or None when it is outside the supported groups.
    path_key = '<section>/<type>' from the pdpURL (/us/<section>/<type>/...) or PDP taxonomy_path; leaf = the
    ial4 / last taxonomyIdPath code; name = display title."""
    n = (name or "").lower()
    if path_key in _FRIDGE_PATHS:
        return _FRIDGE_PATHS[path_key]
    if path_key == "laundry/washers":
        if leaf == "08010103" or (leaf not in ("08010102", "08010104") and "top load" in n):
            return "top_load"
        if leaf in ("08010102", "08010104") or "front load" in n:
            return "front_load"
        return None
    if path_key == "laundry/dryers":
        return "dryer"
    if path_key == "laundry/washer-and-dryer-sets":
        return None if model.upper().startswith("F-") else "laundry_center"  # F-* are two-product packages
    if path_key == "microwaves/solo":
        return "microwave"
    if path_key == "microwaves/over-the-range":
        return "otr"
    if path_key == "cooking-appliances/wall-ovens":
        return "sco" if leaf == _COMBI_WALL_OVEN or _SCO_NAME.search(n) else "electric_oven"
    if path_key == "cooking-appliances/ranges":
        if leaf == _INDUCTION_RANGE or "induction" in n:
            return "induction"
        if leaf == _GAS_RANGE or re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", n):
            return "gas_oven"
        return "radiant" if leaf == _ELECTRIC_RANGE or "electric" in n else None
    if path_key == "cooking-appliances/cooktops":
        if leaf == _INDUCTION_TOP or n.startswith("induction"):
            return "induction"
        if leaf == _RADIANT_TOP or n.startswith("electric cooktop"):
            return "radiant"
        if leaf == _GAS_TOP or n.startswith("gas cooktop"):
            return "gas_cooktop"
    return None


def _url_path_key(path: str) -> str:
    segs = [s for s in urlsplit(path).path.split("/") if s]  # us / <section> / <type> / <slug>
    return "/".join(segs[1:3])


def _taxonomy_path_key(taxonomy: str) -> str:
    segs = [s for s in taxonomy.split("/") if s]  # home-appliances / <section> / <type> / <variant>
    return "/".join(segs[1:3])


# ---------------------------------------------------------------- discover
def parse_search(payload: dict, sub: str | None = None, stats: dict | None = None) -> list[Candidate]:
    """pf_search payload -> Candidates (off-domain urls dropped). With `sub`, only items classified as that sub key;
    without it every item is returned, classified when possible. `stats` (optional) accumulates counts of the
    skipped items: invalid, off_domain, unclassified (outside the supported groups), other (another sub key)."""
    st = stats if stats is not None else {}
    bump = lambda k: st.__setitem__(k, st.get(k, 0) + 1)
    out = []
    for r in payload.get("searchResults", []):
        code, path = r.get("modelCode"), r.get("pdpURL") or r.get("consumerUrl")
        if not code or not path:
            bump("invalid")
            continue
        if not _is_samsung_url(urljoin(BASE, path)):
            bump("off_domain")
            continue
        name = r.get("productDisplayName") or r.get("name") or code
        found = classify(_url_path_key(path), name, r.get("ial4_code") or "", code)
        if sub is not None and found != sub:
            bump("unclassified" if found is None else "other")
            continue
        price = r.get("sale_price") or r.get("msrp_price")
        out.append(Candidate(brand=BRAND, model_number=_model(code), name=name,
                             url=urljoin(BASE, path.rstrip("/") + "/"),
                             price_usd=float(price) if price else None,
                             category=catalog.major_of(found) or "refrigerator",
                             subcategory=found))
    return out


_PAGE_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}


def _clear_page_cache() -> None:
    _PAGE_CACHE.clear()


def _search_page(code: str, start: int) -> dict:
    """One pf_search page; cached per (category code, start) for PAGE_CACHE_TTL_S so several sub keys of the same
    category do not re-page it."""
    hit = _PAGE_CACHE.get((code, start))
    if hit and time.monotonic() - hit[0] < PAGE_CACHE_TTL_S:
        return hit[1]
    data = {"clientCode": "b2c", "clientName": "scom_pf", "firstSearchYN": str(start == 0).lower(),
            "countryCode": "us", "storeID": "us", "startIndex": start, "requestCount": PAGE_SIZE,
            "category_code": code, "filters": "[]", "sort": "recommended"}
    r = requests.post(SEARCH_URL, data=data, timeout=30,
                      headers={**HEADERS, "Origin": BASE, "Referer": BASE + "/"})
    r.raise_for_status()
    payload = r.json()
    if "searchResults" not in payload:
        raise SamsungPageError(f"pf_search: unexpected payload keys {list(payload)}")
    _PAGE_CACHE[(code, start)] = (time.monotonic(), payload)
    return payload


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r}")
    seen: dict[str, Candidate] = {}
    stats: dict = {}
    for code in SUB_SOURCES[subcategory][1]:
        start = 0
        while len(seen) < limit:
            payload = _search_page(code, start)
            for c in parse_search(payload, subcategory, stats):
                seen.setdefault(c.model_number, c)
            start += PAGE_SIZE
            if not payload.get("hasMoreResults") or not payload["searchResults"]:
                break
            time.sleep(PAGE_DELAY_S)
    print(f"samsung_us: discover({subcategory}): kept {min(len(seen), limit)}, unclassified {stats.get('unclassified', 0)}, "
          f"other sub keys {stats.get('other', 0)}, off-domain {stats.get('off_domain', 0)}, "
          f"invalid {stats.get('invalid', 0)}", file=sys.stderr)
    return list(seen.values())[:limit]


# ---------------------------------------------------------------- page loading
def _playwright_html(url: str, headless: bool) -> tuple[int | None, str]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = launch_browser(p, headless=headless)
        try:
            pg = b.new_context(user_agent=UA).new_page()
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(4000)
            _require_samsung(pg.url)
            return (resp.status if resp else None), pg.content()
        finally:
            b.close()


def _page_ok(status: int | None, html: str) -> bool:
    """A real PDP is a 2xx carrying __NEXT_DATA__; a real page may mention 'captcha' in its scripts,
    so block-text detection (common.looks_blocked) is only a diagnostic when this fails."""
    return status is not None and 200 <= status < 300 and "__NEXT_DATA__" in html


def _load_html(url: str) -> str:
    """Cheap requests first (except FRIDGE_BROWSER_MODE=visible), then Playwright:
    auto = headless then visible, headless = never visible, visible = visible only.
    Re-evaluated on every call; nothing is remembered between calls."""
    _require_samsung(url)
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").lower()
    if mode != "visible":
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            r = None
        if r is not None:
            _require_samsung(r.url)
            if _page_ok(r.status_code, r.text):
                return r.text
    order = {"headless": [True], "visible": [False]}.get(mode, [True, False])
    for headless in order:
        try:
            status, html = _playwright_html(url, headless)
        except SamsungPageError:
            raise
        except Exception as e:  # playwright raises its own error types
            print(f"samsung_us: playwright headless={headless} failed: {e}", file=sys.stderr)
            continue
        if _page_ok(status, html):
            return html
        if looks_blocked(status, html):
            print(f"samsung_us: playwright headless={headless} blocked", file=sys.stderr)
    raise SamsungPageError(f"could not load {url} (blocked; mode={mode})")


# ---------------------------------------------------------------- parsing
def parse_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise SamsungPageError("no __NEXT_DATA__ on page")
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
        pd = pp["productData"]
        code = pd["modelCode"]
        product = next(p for p in pd["products"] if p["modelCode"] == code)
    except (KeyError, StopIteration, ValueError) as e:
        raise SamsungPageError(f"unrecognised __NEXT_DATA__ structure ({e!r})") from e
    return {"product": product, "group_id": pp.get("groupId"), "taxonomy": pd.get("taxonomy_path", ""),
            "leaf": str(product.get("taxonomyIdPath") or "").split("/")[-1]}


def fetch_bridge(group_id: str, model_code: str) -> tuple[list[tuple[str, str, str]], str | None, list[dict]]:
    """-> (spec rows (group, name, value), spec-sheet url, support document entries) for model_code."""
    url = (f"{BRIDGE_URL}?data_type=Specs,Support&store_type=B2C&group_id={group_id}&version=v2")  # comma must stay literal
    r = requests.get(url, timeout=30, headers={**HEADERS, "Referer": BASE + "/us/"})
    r.raise_for_status()
    return parse_bridge(r.json(), model_code)


def parse_bridge(j: dict, model_code: str) -> tuple[list[tuple[str, str, str]], str | None, list[dict]]:
    specs = next((s for s in j.get("Specs", []) if s.get("modelCode") == model_code), None)
    if not specs or not specs.get("fullSpecs"):
        raise SamsungPageError(f"no Specs block for {model_code}")
    rows = [(g["groupName"], s["name"], str(s["value"]).strip())
            for g in specs["fullSpecs"] for s in g.get("specList", []) if s.get("name")]
    support = next((s for s in j.get("Support", []) if s.get("modelCode") == model_code), {})
    return rows, (specs.get("download") or {}).get("url"), support.get("supports", [])


def _spec(rows, pat: str, group: str | None = None) -> str | None:
    """First spec value whose name matches pat (optionally within a group)."""
    for g, n, v in rows:
        if (group is None or g == group) and re.fullmatch(pat, n, re.I) and v not in ("", "?"):
            return v
    return None


def _yes_no(rows, *names: str) -> bool | None:
    """True if any named spec says Yes, False if all present ones say No/None, None if silent."""
    vals = [v.lower() for p in names if (v := _spec(rows, p))]
    if any(v != "no" and not v.startswith("no ") and v != "none" for v in vals):
        return True
    return False if vals else None


def _first_float(text: str | None) -> float | None:
    return num(r"(\d[\d,]*\.?\d*)", text) if text else None


def _dims_from_row(rows) -> tuple:
    """(w, h, d) inches: '(decimal)' rows first, else 'W x H x D' string."""
    w, h, d = (_first_float(_spec(rows, rf"{k} \(decimal\)")) for k in ("Width", "Height", "Depth"))
    if None in (w, h, d):
        s = _spec(rows, r"Product Dimensions with Hinges, Handles and Door") or ""
        m = re.search(r"([\d.]+)\"? \(W\) x ([\d.]+)\"? \(H\) x ([\d.]+)\"? \(D\)", s)
        if m:
            w, h, d = (w or float(m.group(1))), (h or float(m.group(2))), (d or float(m.group(3)))
    return w, h, d


def _wifi(rows) -> tuple[bool | None, str | None]:
    """(supported, evidence naming the group/key actually matched)."""
    for g, n, v in rows:
        if re.fullmatch(r"Wi-?Fi(?: Connectivity| Embedded)?", n, re.I) and v not in ("", "?"):
            return v.lower().startswith("yes"), f"Specs > {g}: {n} = {v}"
    for g, n, v in rows:
        if re.fullmatch(r"SmartThings(?: App Support)?", n, re.I) and v.lower().startswith("yes"):
            return True, f"Specs > {g}: {n} = {v}"
    return None, None


def _energy_kwh(rows) -> float | None:
    """Annual kWh: 'Energy Consumption...' row, else an 'Energy Guide Label' row stating kWh per year."""
    got = num(r"(\d[\d,]*\.?\d*)\s*kWh", _spec(rows, r"Energy Consumption.*") or "")
    if got is None:
        got = num(r"(\d[\d,]*\.?\d*)\s*kWh\s*/\s*y", _spec(rows, r"Energy Guide Label") or "", re.I)
    return got


def _pod_features(rows, product: dict, yes_only: bool = False) -> list[str]:
    """Key Features names + marketing title parts. yes_only (non-fridge): laundry/cooking Key Features also hold
    valued specs (capacity, control type), so keep only the Yes ones."""
    feats = [n for g, n, v in rows if g == "Key Features"
             and (v.lower().startswith("yes") if yes_only else v.lower() not in ("no", "none"))]
    title = re.sub(r"&quot;", '"', re.sub(r"&amp;", "&", product.get("productFeatureTitle") or ""))
    feats += [t.strip() for t in title.split("|") if t.strip()]
    return list(dict.fromkeys(feats))


def _doc_type(name: str, in_spec: bool = False) -> str:
    n = name.lower()
    if in_spec:
        return "SpecSheet"
    for pat, dt in ((r"energy", "EnergyGuide"), (r"warranty", "Warranty"), (r"install", "Installation"),
                    (r"user manual|manual", "Manual")):
        if re.search(pat, n):
            return dt
    return "Other"


# Curated category-specific specs: (label, [spec-name patterns, first hit wins], kind). kind: "" raw string,
# "num" number only, "yn" Yes/No from _yes_no over all patterns. Missing specs are skipped, never invented.
_EXTRA: dict[str, list[tuple]] = {
    "washer": [
        ("Washer capacity (cu ft)", [r"Washing Capacity.*", r"Washer Total Capacity.*", r"Washer Capacity.*"], "num"),
        ("Dryer capacity (cu ft)", [r"Dryer Total Capacity.*", r"Dryer Capacity.*"], "num"),
        ("Max spin speed (rpm)", [r"Maximum Spin Speed", r"Spin Speed"], "num"),
        ("Spin speed settings", [r"Spin Speeds"], "num"),
        ("Preset wash cycles", [r"Number of Preset Wash Cycles", r"Number of Cycle", r"Wash Cycles", r"Cycles"], "num"),
        ("Wash options", [r"Number of Wash Options", r"Wash Options", r"Options"], "num"),
        ("Preset drying cycles", [r"Number of Preset Drying Cycles"], "num"),
        ("Drying options", [r"Number of Dryer Options"], "num"),
        ("Steam", [r"Steam", r"Steam Sanitize\+?"], "yn"),
        ("Super Speed", [r"Super Speed"], "yn"),
        ("Sensor dry", [r"Sensor Drying"], "yn"),
        ("Auto dispense", [r"Auto Dispense System", r"Auto Dispense"], ""),
        ("Motor", [r"Motor Power", r"Motor"], ""),
        ("Stackable", [r"Stackable"], "yn"),
        ("Power source / dryer type", [r"Dryer Type", r"Dryer Power Source", r"Power Source"], ""),
        ("Dryer venting", [r"Venting System", r"Dryer Venting System"], ""),
        ("Dryer heating element", [r"Heating Element"], ""),
        ("Cycle time AHAM (min)", [r"Cycle Time \(AHAM 8lbs\)", r"Cycle time \(min\)"], "num"),
        ("IMEF", [r"IMEF"], "num"),
        ("IWF", [r"IWF"], "num"),
    ],
    "cooking": [
        ("Configuration", [r"Configuration", r"Installation Type", r"Type"], ""),
        ("Fuel type", [r"Fuel Type"], ""),
        ("Cooktop type", [r"Cooktop Type", r"General Feature"], ""),
        ("Oven capacity (cu ft)", [r"Oven Capacity \(cu\.ft\)", r"Oven Capacity", r"Large Oven Capacity"], "num"),
        ("Cavity capacity (cu ft)", [r"Capacity \(cu\. ?ft\.\)", r"Capacity"], "num"),
        ("Oven cavities", [r"Cavity Type", r"Oven Type"], ""),
        ("Oven racks", [r"Total Number of Racks", r"Total Racks"], "num"),
        ("Rack positions", [r"Number of Rack Positions"], "num"),
        ("Oven cleaning", [r"Oven Cleaning Type"], ""),
        ("Control type", [r"Control Type", r"Controls for Oven", r"Control Material"], ""),
        ("Bake element (W)", [r"Bake Element"], "num"),
        ("Broil element (W)", [r"Broil Element"], "num"),
        ("Cooktop total power (kW)", [r"Total Power"], "num"),
        ("Microwave output (W)", [r"Output Power \(Microwave\)", r"Cooking Power"], "num"),
        ("Microwave input (W)", [r"Power Consumption \(Microwave\)"], "num"),
        ("Power levels", [r"Number of Power Levels"], "num"),
        ("Turntable size", [r"Turntable Size.*"], ""),
        ("Vent power (CFM)", [r"Vent Power"], "num"),
        ("Vent fan speeds", [r"Vent\s+Fan Speed"], "num"),
        ("Sabbath mode", [r"Sabbath Mode"], "yn"),
        ("Cutout dimensions", [r"Cut-?Out Dimensions"], ""),
    ],
}
_YES = re.compile(r"^(yes|y)\b", re.I)


def _num_str(v: str) -> str | None:
    n = _first_float(v)
    return None if n is None else (str(int(n)) if n == int(n) else str(n))


def _burner_rows(rows) -> list[tuple[str, str]]:
    return [(re.sub(r"^(?:Burner|Element) - ", "", n), v) for g, n, v in rows
            if g == "Cooktop" and re.match(r"(?:Burner|Element) - ", n)]


def _noise(t) -> str:
    """Strip registered/trademark marks; line breaks inside a value become ' | '."""
    lines = [" ".join(l.replace("®", "").replace("™", "").split()) for l in str(t).splitlines()]
    return " | ".join(l for l in lines if l)


def full_spec_table(rows) -> dict[str, str]:
    """EVERY Specs row: 'Group > Name' -> full value (a repeated key keeps all values, ' | '-joined)."""
    out: dict[str, str] = {}
    for g, n, v in rows:
        val = _noise(v)
        if not val or val == "?":
            continue
        key = f"{_noise(g)} > {_noise(n)}" if _noise(g) else _noise(n)
        out[key] = f"{out[key]} | {val}" if key in out else val
    return out


def main_image_url(product: dict) -> str | None:
    """Hero image: gallery[0].url (else defaultImage); protocol-relative/relative urls resolved; Samsung hosts only."""
    first = next(iter(product.get("gallery") or []), None)
    cands = [first.get("url") if isinstance(first, dict) else None, product.get("defaultImage")]
    for raw in cands:
        if isinstance(raw, str) and raw.strip():
            url = urljoin(BASE + "/us/", raw.strip())
            if _is_samsung_url(url):
                return url
    return None


def extra_specs(major: str, sub: str | None, rows, product: dict) -> dict[str, str]:
    """Curated category-specific specs (English label -> value string); empty for refrigerators."""
    out: dict[str, str] = {}
    for label, pats, kind in _EXTRA.get(major, []):
        if kind == "yn":
            yn = _yes_no(rows, *pats)
            val = None if yn is None else ("Yes" if yn else "No")
        else:
            val = next((v for p in pats if (v := _spec(rows, p))), None)
            if val and kind == "num":
                val = _num_str(val)
        if val:
            out[label] = val
    if major == "washer":
        ai = [n for g, n, v in rows if re.search(r"\bAI\b", n) and _YES.match(v)]
        title = [t.strip() for t in (product.get("productFeatureTitle") or "").split("|") if re.search(r"\bAI\b", t)]
        ai = list(dict.fromkeys(ai + title))
        if ai:
            out["AI features"] = "; ".join(ai)
        if sub == "dryer":  # a dryer has one capacity, filed under 'Dryer capacity'
            cap = out.pop("Washer capacity (cu ft)", None)
            if cap and "Dryer capacity (cu ft)" not in out:
                out["Dryer capacity (cu ft)"] = cap
    elif major == "cooking":
        if sub in ("microwave", "otr"):
            out.pop("Oven capacity (cu ft)", None)
        else:
            out.pop("Cavity capacity (cu ft)", None)
            conv = _yes_no(rows, r"Convection\+?", r"Convection Bake", r"True Convection", r"Dual Convection")
            if conv is None and (cs := _spec(rows, r"Cooking System")):
                conv = "convection" in cs.lower()
            if conv is not None:
                out["Convection"] = "Yes" if conv else "No"
            air = _yes_no(rows, r"Air Fry", r"Air Fry Max")
            if air is not True and ("air fry" in (product.get("productTitle") or "").lower()
                                    or any(re.search(r"air fry", n, re.I) and _YES.match(v) for _, n, v in rows)):
                air = True
            if air is not None:
                out["Air fry"] = "Yes" if air else "No"
        if sub in ("gas_oven", "gas_cooktop", "induction", "radiant"):
            b = _burner_rows(rows)
            count = _spec(rows, r"Total Number of (?:Burners|Elements)")
            if count and (n := _num_str(count)):
                out["Burners/elements"] = n
            elif b:
                out["Burners/elements"] = str(len(b))
            if b:
                out["Burner/element detail"] = "; ".join(f"{k}: {v}" for k, v in b)
    return out


def _electrical(rows) -> tuple[str | None, float | None, float | None]:
    """(voltage, amps, hertz) from the electrical rows. When a row lists several (dryer 120V/240V, washer+dryer
    amps) the largest is the service requirement."""
    vals = [(n, v) for _, n, v in rows if re.fullmatch(
        r"Volt/Amps|Voltage\s*/\s*Frequency.*|Power/Ratings|Electrical|Ampere|Amp Circuit", n, re.I)]
    volts = [int(x) for _, v in vals for x in re.findall(r"(\d+)\s*V\b", v)]
    amps = [float(x) for n, v in vals for x in
            (re.findall(r"([\d.]+)\s*A(?:mps?)?\b", v) or (re.findall(r"^\s*([\d.]+)\s*$", v) if n.lower() == "ampere" else []))]
    hz = [float(x) for _, v in vals for x in re.findall(r"(\d+)\s*Hz", v, re.I)]
    return (str(max(volts)) if volts else None, max(amps) if amps else None, max(hz) if hz else None)


def _capacity_total(rows, major: str) -> float | None:
    if major == "refrigerator":
        return _first_float(_spec(rows, r"(?:Total Capacity|Net Total).*"))
    pats = {"washer": [r"Total Capacity.*", r"Washing Capacity.*", r"Washer Total Capacity.*", r"Washer Capacity.*",
                       r"Dryer Total Capacity.*", r"Dryer Capacity.*"],
            "cooking": [r"Oven Capacity \(cu\.ft\)", r"Oven Capacity", r"Large Oven Capacity", r"Capacity.*"]}[major]
    return _first_float(next((v for p in pats if (v := _spec(rows, p))), None))


def _classify_product(nd: dict) -> tuple[str | None, str | None]:
    """(major, sub). Major falls back to the taxonomy section when the sub key is unsupported (e.g. upright freezer)."""
    p = nd["product"]
    path = _taxonomy_path_key(nd["taxonomy"])
    sub = classify(path, p.get("productTitle") or "", nd.get("leaf", ""), p.get("modelCode") or "")
    if sub:
        return catalog.major_of(sub), sub
    section = path.split("/")[0]
    return ({"refrigerators": "refrigerator", "laundry": "washer", "microwaves": "cooking",
             "cooking-appliances": "cooking"}.get(section), None)


def build_record(url: str, nd: dict, rows, spec_url, supports) -> tuple[ProductRecord, list[tuple[str, str]]]:
    """Pure function: parsed page+bridge data -> ProductRecord (+ [(doc_type, https url)] to download)."""
    p = nd["product"]
    model = _model(p["modelCode"])
    major, sub = _classify_product(nd)
    if not major:
        raise SamsungPageError(f"unsupported Samsung product group (taxonomy {nd['taxonomy']!r})")
    fridge = major == "refrigerator"
    volt, amps, hz = _electrical(rows)
    wifi_ok, wifi_ev = _wifi(rows)
    segs = [s for s in nd["taxonomy"].split("/") if s]
    style = DOOR_STYLES.get(segs[2]) if fridge and len(segs) > 2 else None
    price = p.get("currentPrice") or p.get("msrpPrice")
    star = {"Y": True, "N": False}.get(p.get("energyStarFlag"))
    w, h, d = _dims_from_row(rows)
    docs = [(_doc_type(s["name"]), s["url"]) for s in supports if s.get("type") == "PDF" and s.get("url")]
    if spec_url:
        docs.append(("SpecSheet", spec_url))
    docs = [(t, re.sub(r"^http://", "https://", u)) for t, u in docs]
    docs = [(t, u) for t, u in docs if _pdf_url_ok(u)][:MAX_PDFS]
    # A fridge's 'Net Weight' is lb; on laundry it is kg labelled lb, so non-fridges use 'Product Weight' only.
    weight = _first_float(_spec(rows, r"(?:Product|Net) Weight.*" if fridge else r"Product Weight.*"))
    fridge_fields = dict(
        door_style=style,
        capacity_fridge_cuft=_first_float(_spec(rows, r"(?:Refrigerator Capacity|Net for Fridge).*")),
        capacity_freezer_cuft=_first_float(_spec(rows, r"(?:Freezer Capacity|Net for Freezer).*")),
        ice_maker=_yes_no(rows, r"Ice Maker", r"Automatic Ice Maker", r"Built-In Automatic Icemaker", r"Icemaker"),
        water_dispenser=_yes_no(rows, r"Ice/Water Dispenser", r"Water and Ice Dispenser",
                                r"Dispenser with Water Filter", r"Dispenser Type"),
    ) if fridge else {}
    return ProductRecord(
        brand=BRAND, model_number=model, product_name=p.get("productTitle") or model, product_url=url,
        category=major, subcategory=sub,
        finish_color=(p.get("attributes") or {}).get("Color") or _spec(rows, r"Color"),
        price_usd=float(price) if price else None,
        capacity_total_cuft=_capacity_total(rows, major),
        width_in=w, height_in=h, depth_in=d, weight_lb=weight,
        voltage_v=volt, amps=amps, frequency_hz=hz,
        energy_kwh_year=_energy_kwh(rows),
        energy_star=star if star is not None else (
            True if _YES.match(_spec(rows, r"ENERGY STAR(?:(?!Most Efficient).)*") or "") else None),
        wifi_supported=wifi_ok, wifi_evidence=wifi_ev,
        pod_features=_pod_features(rows, p, yes_only=not fridge),
        extra_specs={**full_spec_table(rows), **extra_specs(major, sub, rows, p)},
        image_url=main_image_url(p),
        **fridge_fields,
    ), docs


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    nd = parse_next_data(_load_html(url))
    if not nd["group_id"]:
        raise SamsungPageError("no groupId in __NEXT_DATA__")
    rows, spec_url, supports = fetch_bridge(str(nd["group_id"]), nd["product"]["modelCode"])
    product, doc_urls = build_record(url, nd, rows, spec_url, supports)
    fname_model = _safe_model(product.model_number)

    docs, texts = [], {}
    for dt, u in doc_urls:
        d = download_pdf(BRAND, fname_model, dt, u)
        if d:
            docs.append(d)
            texts[dt] = pdf_text(ROOT / d.local_path)
    if product.energy_kwh_year is None:  # PDF fallback: Energy Guide
        product.energy_kwh_year = num(r"(\d[\d,]*)\s*kWh\s*/?\s*(?:year|yr)", texts.get("EnergyGuide", ""), re.I)

    ss = texts.get("SpecSheet", "")  # PDF fallbacks (spec sheet): only fill what the web spec table lacks
    if product.weight_lb is None:
        product.weight_lb = num(r"^Weight:\s*([\d.]+)\s*lbs", ss, re.I | re.M)  # first = net; shipping comes later
    if product.capacity_total_cuft is None and product.category == "refrigerator":
        # EnergyGuide states net capacity; SpecSheet only a marketing size
        product.capacity_total_cuft = (num(r"Capacity:\s*([\d.]+)\s*Cubic Feet", texts.get("EnergyGuide", ""), re.I)
                                       or num(r"Capacity of ([\d.]+) cu", ss, re.I))

    raw = [RawSpec(brand=BRAND, model_number=product.model_number, source="web", section=g, key=n, value=v)
           for g, n, v in rows]
    return product, docs, raw
