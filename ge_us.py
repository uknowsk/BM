"""GE Appliances US adapter (https://www.geappliances.com; brand 'GE', lines GE and GE Profile).

Data sources (found by inspecting the site; no browser needed in the normal case):
  discover(): category pages are rendered client-side by Searchspring (site id q7rntw). The same JSON the
    page widget calls is fetched directly: GET q7rntw.a.searchspring.io/api/search/search.json with
    bgfilter.categories_hierarchy=<category path> (+ is_part/is_obsolete/availability filters). Each result
    carries sku, sale `price`, `msrp` and the PDP path in `custom_url`.
  scrape(): the BigCommerce PDP embeds the whole product as JSON in `window.stencilBootstrap("product", "...")`
    -> productObj.custom_fields (Spec_<SECTION>_<Key>[_n] spec table, Documents_<n>_<Label> PDF links,
    Product_Claims_n, ...), price and category list. PDFs live on images.salsify.com.
  Café and Monogram are separate sites (cafeappliances.com / monogram.com): not covered, so 'built_in'
  refrigerators (Monogram only) is unsupported.
Browser mode (FRIDGE_BROWSER_MODE, read on every call): auto = plain requests, then headless, then visible
browser | headless | visible (explicit modes skip requests).
"""
import html as htmllib
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import catalog
import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "GE"
BASE = "https://www.geappliances.com"
SS_SITE = "q7rntw"
SS_HOST = "a.searchspring.io"
SS_URL = f"https://{SS_SITE}.{SS_HOST}/api/search/search.json"
PAGE_HOSTS = ("geappliances.com",)
PDF_HOSTS = ("geappliances.com", "salsify.com")  # images.salsify.com, products.geappliances.com
PAGE_SIZE = 100
DELAY_S = 1.0
MAX_DOCS = 5
BOOTSTRAP = 'window.stencilBootstrap("product", '
CONSENT_REJECT = ("Reject All", "Reject all", "Decline", "Decline All", "Reject non-essential", "Only necessary")
CONSENT_ACCEPT = ("Accept All", "Accept all", "Accept", "Allow all", "I Agree")


class GEPageError(RuntimeError):
    """GE page/API did not have the expected structure (site changed or blocked)."""


class GENotFound(GEPageError):
    """HTTP 404/410: retrying with a browser cannot help."""


# ---------------------------------------------------------------- subcategory map
def _text(item: dict, *keys: str) -> str:
    return " ".join(str(item.get(k) or "") for k in keys).lower()


def _is_french(item: dict) -> bool:
    return "french" in _text(item, "name", "spec_features_configuration")


def _not_french(item: dict) -> bool:
    return not _is_french(item)


def _is_induction(item: dict) -> bool:
    return "induction" in _text(item, "name", "spec_features_cooktop_burner_type")


def _not_induction(item: dict) -> bool:
    return not _is_induction(item)


def _is_gas(item: dict) -> bool:
    return bool(re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", _text(item, "name", "spec_features_fuel_type")))


def _not_gas(item: dict) -> bool:
    return not _is_gas(item)


def _not_wine(item: dict) -> bool:
    return not _text(item, "item_commercial_category3").startswith("wine")


_REF = "GE Appliances>Kitchen>Refrigerators>"
_RNG = "GE Appliances>Kitchen>Ranges>"
_MW = "GE Appliances>Kitchen>Microwaves>"
_CT = "GE Appliances>Kitchen>Cooktops>"
_WO = "GE Appliances>Kitchen>Wall Ovens>"
_LND = "GE Appliances>Laundry>"

# sub key -> (major, [(category path, predicate on the item or None)]). Edit the mapping here only.
# Dict order is the classification priority (first match wins), used by discover() AND scrape().
SUB_SOURCES: dict[str, tuple[str, list]] = {
    "french_door": ("refrigerator", [(_REF + "French Door Refrigerators", None),
                                     (_REF + "Bottom Freezer Refrigerators", _is_french)]),
    "side_by_side": ("refrigerator", [(_REF + "Side-by-Side Refrigerators", None)]),
    "top_freezer": ("refrigerator", [(_REF + "Top Freezer Refrigerators", None)]),
    "bottom_freezer": ("refrigerator", [(_REF + "Bottom Freezer Refrigerators", _not_french)]),
    "compact": ("refrigerator", [(_REF + "Small & Undercounter Refrigerators", _not_wine)]),
    "laundry_center": ("washer", [(_LND + "Stacked Washer Dryer Units", None)]),  # before top/front load: stacked units
    "top_load": ("washer", [(_LND + "Washers>Top Loading Washers", None)]),         # may also sit under a washer category
    "front_load": ("washer", [(_LND + "Washers>Front Loading Washers", None)]),
    "dryer": ("washer", [(_LND + "Dryers", None)]),
    # sco = Speed Cook Oven: microwave-combination wall ovens and every Advantium (speed) oven. Before otr/microwave
    # so an Over-the-Range Advantium (listed only under Advantium Ovens) is a speed oven, not an OTR microwave.
    "sco": ("cooking", [(_WO + "Microwave Oven Combination", None), ("GE Appliances>Kitchen>Advantium Ovens", None)]),
    "microwave": ("cooking", [(_MW + "Countertop Microwave Ovens", None), (_MW + "Built In Microwave Ovens", None)]),
    "otr": ("cooking", [(_MW + "Over-the-Range Microwave Ovens", None)]),
    "induction": ("cooking", [(_RNG + "Induction Ranges", None), (_CT + "Induction Cooktops", None)]),
    # gas_oven = gas and dual-fuel RANGES (GE sells no gas wall oven); a Double Oven Range is gas or electric by its fuel.
    "gas_oven": ("cooking", [(_RNG + "Gas Ranges", None), (_RNG + "Dual Fuel Ranges", None),
                             (_RNG + "Double Oven Ranges", _is_gas)]),
    # gas_cooktop = oven-less gas cooktops and rangetops (the site files rangetops under Gas Cooktops).
    "gas_cooktop": ("cooking", [(_CT + "Gas Cooktops", None)]),
    # radiant = electric (non-induction) ranges and electric cooktops.
    "radiant": ("cooking", [(_RNG + "Electric Ranges", _not_induction), (_RNG + "Double Oven Ranges", _not_gas),
                            (_CT + "Electric Cooktops", _not_induction)]),
    # electric_oven = single/double wall ovens without a microwave/speed-cook function; GE sells no gas wall oven
    # and no Cafe/Monogram on this site, so built_in is not offered.
    "electric_oven": ("cooking", [(_WO + n, None) for n in ("Single Wall Ovens", "Double Wall Ovens")]),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
_ACCESSORY = re.compile(r"accessor", re.I)


def _ge_host(url: str) -> bool:
    return _host_in(url, PAGE_HOSTS)


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _pdf_url_ok(url: str) -> bool:
    return urlparse(url).scheme == "https" and _host_in(url, PDF_HOSTS)


def _check_final_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not _ge_host(url):
        raise GEPageError(f"unexpected url after navigation/request: {url[:120]}")


def _safe_name(m: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", m).strip(".") or "unknown"


# ---------------------------------------------------------------- discover
def signals(rating, count, first_dist) -> dict:
    """Consumer-response / launch signals the site itself publishes (Bazaarvoice summary, Product_First_Distribution_Date).
    Absent or zero-review values are omitted (= unknown); is_new is never set (the site has no NEW flag)."""
    out = {}
    try:
        n, r = int(float(count)), float(rating)
    except (TypeError, ValueError):
        n = r = 0
    if n > 0 and 0 < r <= 5:
        out["rating"], out["review_count"] = round(r, 2), n
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", str(first_dist or ""))
    if m:
        out["release_date"], out["release_src"] = "-".join(m.groups()), "distribution"
    return out


def parse_search(data: dict, sub: str) -> list[tuple[Candidate, dict]]:
    """Searchspring response -> [(Candidate, raw result item)] for one sub key; off-domain urls dropped."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or "pagination" not in data:
        raise GEPageError("Searchspring response has no results/pagination (structure changed?)")
    major = SUB_SOURCES[sub][0]
    out = []
    for r in results:
        sku, path = (r.get("sku") or "").strip().upper(), r.get("custom_url") or ""
        if not sku or not path or _ACCESSORY.search(str(r.get("item_commercial_category2") or "")):
            continue
        url = urljoin(BASE + "/", path)
        if not _ge_host(url) or urlparse(url).scheme != "https":
            print(f"dropped off-domain candidate {sku}: {url[:100]}", file=sys.stderr)
            continue
        try:
            price = float(r.get("price")) if r.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        sig = signals(r.get("rating"), r.get("ratingCount"), r.get("product_first_distribution_date"))
        out.append((Candidate(brand=BRAND, model_number=sku, name=htmllib.unescape(str(r.get("name") or sku)),
                              url=url, price_usd=price or None, category=major, subcategory=sub,
                              attrs=sig, attrs_src={k: "listing" for k in sig}), r))
    return out


def _search_page(path: str, page_no: int) -> dict:
    params = {"siteId": SS_SITE, "resultsFormat": "native", "resultsPerPage": PAGE_SIZE, "page": page_no,
              "bgfilter.categories_hierarchy": path, "bgfilter.is_part": "false",
              "bgfilter.is_obsolete": "false", "bgfilter.availability": "available",
              "sort.product_first_distribution_date": "desc"}  # the site's own "Newest" option (verified 2026-10-09)
    r = requests.get(SS_URL, params=params, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise GEPageError(f"unexpected Searchspring redirect (HTTP {r.status_code})")
    if not _host_in(r.url, (SS_HOST,)) or urlparse(r.url).scheme != "https":
        raise GEPageError(f"unexpected Searchspring url {r.url[:120]}")
    r.raise_for_status()
    return r.json()


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"GE US adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    first, other = True, 0
    for path, _pred in SUB_SOURCES[subcategory][1]:
        page_no, total_pages = 1, 1
        while len(found) < limit and page_no <= total_pages:
            if not first:
                time.sleep(DELAY_S)
            first = False
            data = _search_page(path, page_no)
            total_pages = int((data.get("pagination") or {}).get("totalPages") or 1)
            for cand, item in parse_search(data, subcategory):
                cats = _item_categories(item)
                if cats is not None and _classify(cats, item) != subcategory:
                    other += 1  # belongs to another sub key (it is listed there by the same rule)
                    continue
                found.setdefault(cand.model_number, cand)
            page_no += 1
        if len(found) >= limit:
            break
    if other:
        print(f"ge_us: discover({subcategory}): skipped {other} listed product(s) classified under another sub key",
              file=sys.stderr)
    out = list(found.values())[:limit]
    # a sub spread over several site categories has no single order, so no rank there
    return catalog.stamp_newest_order(out) if len(SUB_SOURCES[subcategory][1]) == 1 else out


# ---------------------------------------------------------------- page loading
def _consent(page, accept: bool = False) -> bool:
    """Best-effort cookie banner click: decline non-essential first; accept only if the page is stuck."""
    for label in (CONSENT_ACCEPT if accept else CONSENT_REJECT):
        try:
            page.get_by_role("button", name=label, exact=True).first.click(timeout=1500)
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return False


def _strategies() -> list[str]:
    """Fetch strategies in order. FRIDGE_BROWSER_MODE is read on every call; nothing is cached across calls."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return ["headless"]
    if mode == "visible":
        return ["visible"]
    return ["requests", "headless", "visible"]


MAX_REDIRECTS = 5


def _via_requests(url: str) -> str:
    """GET url following redirects by hand: every hop (https + GE host) is checked BEFORE it is requested."""
    cur = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_final_url(cur)
        r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise GEPageError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise GENotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if common.looks_blocked(r.status_code, body) or r.status_code != 200:
        raise GEPageError(f"blocked or bad status ({r.status_code})")
    if BOOTSTRAP not in body:
        raise GEPageError("no stencilBootstrap product data in page")
    return body


def _via_browser(url: str, headless: bool) -> str:
    probe = "() => document.documentElement.outerHTML.includes('window.stencilBootstrap(\"product\"')"
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_final_url(page.url)
            _consent(page)
            try:
                page.wait_for_function(probe, timeout=15000)
            except PlaywrightTimeoutError:
                _consent(page, accept=True)  # banner may be blocking rendering; retry once
                page.wait_for_function(probe, timeout=15000)
            _check_final_url(page.url)
            status = resp.status if resp else None
            if status in (404, 410):
                raise GENotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise GEPageError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_pdp_html(url: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url) if strategy == "requests" else _via_browser(url, strategy == "headless")
        except GENotFound:
            raise
        except (GEPageError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"GE load via {strategy} failed: {e}", file=sys.stderr)
    raise GEPageError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- parsing helpers
def extract_product(html: str) -> dict:
    """productObj dict from the PDP's stencilBootstrap JSON. Raises GEPageError if absent/unrecognised."""
    i = html.find(BOOTSTRAP)
    if i < 0:
        raise GEPageError("no stencilBootstrap product data in page")
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[i + len(BOOTSTRAP):])
        po = json.loads(payload)["productObj"]
    except (ValueError, KeyError, TypeError) as e:
        raise GEPageError(f"stencilBootstrap product JSON unreadable: {e}") from e
    if not po.get("sku") or not isinstance(po.get("custom_fields"), list):
        raise GEPageError("productObj lacks sku/custom_fields (structure changed?)")
    return po


_SPEC_NAME = re.compile(r"^Spec_([^_]+)_(.+?)(?:_(\d+))?$")


def spec_rows(po: dict, sep: str = "; ") -> list[tuple[str, str, str]]:
    """(section, key, value) in site order; numbered duplicates (Key_1, Key_2...) merged with `sep`."""
    rows: dict[tuple[str, str], list[str]] = {}
    for f in po["custom_fields"]:
        m = _SPEC_NAME.match(f.get("name") or "")
        val = _clean(f.get("value"))
        if m and val:
            rows.setdefault((m.group(1).title(), m.group(2).strip()), []).append(val)
    return [(s, k, sep.join(v)) for (s, k), v in rows.items()]


def _noise(t: str) -> str:
    return t.replace("®", "").replace("™", "").strip()


def full_spec_table(po: dict) -> dict[str, str]:
    """EVERY Specs & Details row: 'Section > Label' -> value (multi-value rows joined with ' | ')."""
    out: dict[str, str] = {}
    for sec, key, val in spec_rows(po, " | "):
        out.setdefault(f"{_noise(sec)} > {_noise(key)}", _noise(val))
    return out


IMAGE_HOSTS = ("bigcommerce.com", "geappliances.com", "salsify.com")
IMAGE_SIZE = "1280x1280"


def main_image_url(po: dict) -> str | None:
    """Hero image: productObj.main_image (else first of images); BigCommerce '{:size}' placeholder -> 1280x1280."""
    cands = [po.get("main_image")] + list(po.get("images") or [])
    for c in cands:
        raw = (c.get("data") if isinstance(c, dict) else None) or ""
        if not raw:
            continue
        url = urljoin(BASE + "/", raw.replace("{:size}", IMAGE_SIZE))
        if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS):
            return url
    return None


def _clean(v) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", str(v or ""))).split())


def _num_str(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def _first_num(v: str | None) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", v or "")
    return float(m.group(0).replace(",", "")) if m else None


def _inches(v: str | None) -> float | None:
    """'35.75 in' | '26 3/4 in' | '1/8 in' -> float inches."""
    v = v or ""
    m = re.search(r"(\d+)\s+(\d+)/(\d+)", v)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.search(r"(?<![\d.])(\d+)/(\d+)", v)
    if m:
        return int(m.group(1)) / int(m.group(2))
    return _first_num(v)


def _yes_no(v: str | None) -> bool | None:
    v = (v or "").strip().lower()
    return True if re.match(r"(yes|true|built)", v) else False if re.match(r"(no|none|n/a)\b", v) else None


def _electrical(spec: dict[str, str]) -> dict:
    text = " ; ".join(spec.get(k, "") for k in ("Electrical Requirements", "Volts/Hertz/Amps", "Volts/Hertz",
                                                "Volts/Watts/Amps", "Input Volts/Hertz"))
    volts = re.search(r"(\d{3}(?:/\d{3})?)\s*V", text, re.I)
    amps = re.search(r"(\d+(?:\.\d+)?)\s*A\b", text)
    hz = re.search(r"(\d{2})\s*Hz", text, re.I)
    return {"voltage_v": volts.group(1) if volts else None,
            "amps": float(amps.group(1)) if amps else _first_num(spec.get("Electrical Input - 120V Amperage")),
            "frequency_hz": float(hz.group(1)) if hz else _first_num(spec.get("Hertz"))}


def _door_style(*texts: str) -> str | None:
    t = " ".join(texts).lower().replace("-", " ")
    for pat, name in ((r"french", "French Door"), (r"side by side", "Side-by-Side"), (r"top freezer", "Top Freezer"),
                      (r"bottom freezer", "Bottom Freezer"), (r"compact|undercounter", "Compact")):
        if re.search(pat, t):
            return name
    return None


_PRIORITY = [("energy guide", "EnergyGuide"), ("use and care|owner|manual", "Manual"),
             ("quick spec", "QuickSpecs"), ("install", "Installation"), ("spec sheet|dimension", "SpecSheet")]


def doc_links(po: dict) -> list[tuple[str, str]]:
    """[(doc_type, url)] English-site PDFs from Documents_<n>_<Label> fields, priority order, one per type."""
    picked: dict[str, tuple[int, str]] = {}
    for f in po["custom_fields"]:
        m = re.match(r"^Documents_\d+_(.+)$", f.get("name") or "")
        url = (f.get("value") or "").strip()
        if not m or ".pdf" not in url.lower():
            continue
        label = m.group(1).lower()
        for prio, (pat, dtype) in enumerate(_PRIORITY):
            if re.search(pat, label):
                picked.setdefault(dtype, (prio, url))
                break
    return [(t, u) for t, (_, u) in sorted(picked.items(), key=lambda kv: kv[1][0])][:MAX_DOCS]


def _categories(po: dict) -> set[str]:
    return {htmllib.unescape(c).replace("/", ">") for c in po.get("category") or []}


def _classify(cats: set[str], item: dict) -> str | None:
    """First SUB_SOURCES entry (dict order = priority) whose category path + predicate accept the product.
    The only classification rule: discover() and scrape() both use it, so a product has exactly one sub key."""
    for sub, (_, sources) in SUB_SOURCES.items():
        if any(any(c == path or c.startswith(path + ">") for c in cats) and (pred is None or pred(item))
               for path, pred in sources):
            return sub
    return None


def infer_subcategory(po: dict, item: dict) -> str | None:
    return _classify(_categories(po), item)


def _item_categories(item: dict) -> set[str] | None:
    """Category paths of a Searchspring result (same form as _categories), None when it carries none."""
    cats = {htmllib.unescape(str(c)).replace("/", ">") for c in item.get("categories_hierarchy") or []}
    return cats or None


def _major_from_paths(po: dict) -> str | None:
    t = " ".join(_categories(po)).lower()
    for major, pat in (("refrigerator", r"refrigerators"), ("washer", r"laundry|\bwashers\b|\bdryers\b"),
                       ("cooking", r"ranges|wall ovens|microwaves|cooktops")):
        if re.search(pat, t):
            return major
    return None


# Curated category-specific specs: (label, GE spec key, "num" to keep only the number). Missing keys are skipped.
_EXTRA = {
    "refrigerator": [("Defrost type", "Defrost Type"), ("Refrigerant", "Refrigerant Type"),
                     ("Sabbath mode", "Sabbath Mode"), ("Number of doors", "Number of Doors"),
                     ("Control type", "Control Type"), ("Temperature display", "Temperature Display"),
                     ("Dispenser", "Dispenser"), ("Ice maker", "Icemaker"), ("Water filtration", "Water Filtration"),
                     ("Garage ready", "Garage Ready"), ("Exterior style", "Exterior Style"),
                     ("Configuration", "Configuration")],
    "washer": [("Product type", "Product Type"), ("Style", "Style"), ("Drum capacity (cu ft)", "Total Capacity (cubic feet)", "num"),
               ("Fuel type", "Fuel Type"), ("Cycles", "Number of Cycles", "num"), ("Specialty cycles", "Specialty Cycles"),
               ("Additional cycles", "Additional Cycles"), ("Steam", "Steam"),
               ("Max spin speed (rpm)", "Maximum Spin Speed", "num"), ("Spin speeds", "Spin Speeds", "num"),
               ("Wash/rinse temperatures", "Wash/Rinse Temperatures"), ("Water temperature system", "Water Temp System"),
               ("Wash basket", "Wash Basket Type"), ("Motor speeds", "Motor Speeds"), ("Wattage (W)", "Watts", "num"),
               ("Drum type", "Drum Type"), ("Dryness levels", "Dryness Levels", "num"),
               ("Heat selections", "Heat Selections", "num"), ("Moisture sensor", "Moisture Sensor"),
               ("Exhaust options", "Exhaust Options"), ("Long vent capability", "Long Vent Capability"),
               ("Energy saving option", "Energy Saving Option"), ("Companion link", "Communication")],
    "cooking": [("Product type", "Product Type"), ("Fuel type", "Fuel Type"), ("Configuration", "Configuration"),
                ("Cooking technology", "Cooking Technology"), ("Cooking system", "Cooking System"),
                ("Oven capacity (cu ft)", "Total Capacity (cubic feet)", "num"),
                ("Oven cleaning", "Oven Cleaning Type"), ("Oven rack positions", "Oven Rack Positions (Single or Upper/Lower)"),
                ("Cooktop type", "Cooktop Type"), ("Cooktop surface", "Cooktop Surface"),
                ("Cooktop burner type", "Cooktop Burner Type"), ("Bake wattage", "Bake Wattage"),
                ("Broiler wattage", "Broiler Wattage"), ("Convection wattage", "Convection Wattage"),
                ("Top burner BTU (nat. gas)", "Top Burner BTU Rating - Nat. Gas (000's BTU's)"),
                ("Bake/broil BTU (nat. gas)", "Bake/Broiler BTU Rating - Nat. Gas (000's BTU's)"),
                ("Microwave wattage (W)", "Microwave Watts (IEC-705)", "num"), ("Power levels", "Power Levels", "num"),
                ("Turntable size (in)", "Turntable Size", "num"), ("Vent CFM", "Vent CFM", "num"),
                ("Venting type", "Venting Type"), ("Input wattage (W)", "Watts", "num"),
                ("Control type", "Control Type"), ("Sabbath mode", "Sabbath Mode"), ("Timer", "Timer"),
                ("Cutout dimensions (in)", "Cutout Dimensions (w x h x d) (in.)")],
}


def extra_specs(major: str, sub: str | None, spec: dict[str, str], rows) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, key, *kind in _EXTRA.get(major, []):
        v = spec.get(key)
        if not v or (major == "cooking" and key == "Total Capacity (cubic feet)" and sub in ("microwave", "otr")):
            continue
        if kind and (n := _first_num(v)) is not None:
            v = _num_str(n)
        out[label] = v
    if major == "cooking" and sub not in ("microwave", "otr"):
        tech = spec.get("Cooking Technology") or spec.get("Cooking System")
        if tech:
            out["Convection"] = "Yes" if "convection" in tech.lower() else "No"
        burners = [(k, v) for s, k, v in rows if s == "Features" and re.match(r"(Burner|Element) - ", k)]
        if burners:
            out["Burners/elements"] = str(len(burners))
            out["Burner/element detail"] = "; ".join(f"{k.split(' - ', 1)[1]}: {v}" for k, v in burners)
    elif major == "cooking" and (n := _first_num(spec.get("Total Capacity (cubic feet)"))) is not None:
        out["Cavity capacity (cu ft)"] = _num_str(n)
    return out


def parse_product(url: str, po: dict) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    """Pure parse of productObj -> (record, RawSpec rows, doc links). energy_kwh_year comes from the spec table."""
    model = po["sku"].strip().upper()
    rows = spec_rows(po)
    spec: dict[str, str] = {}
    for _, k, v in rows:
        spec.setdefault(k, v)
    fields = {f["name"]: _clean(f.get("value")) for f in po["custom_fields"] if f.get("name")}
    title = _clean((po.get("title") or "").split("|^|")[0])
    item = {"name": title, "item_commercial_category3": fields.get("item_commercial_category3", ""),
            "spec_features_configuration": spec.get("Configuration", ""),
            "spec_features_cooktop_burner_type": spec.get("Cooktop Burner Type", ""),
            "spec_features_fuel_type": spec.get("Fuel Type", "")}
    sub = infer_subcategory(po, item)
    major = catalog.major_of(sub) if sub else _major_from_paths(po)
    if not major:
        raise ValueError(f"not a supported GE product (categories: {sorted(_categories(po))[:3]})")
    fridge = major == "refrigerator"

    claims = " ".join(v for k, v in fields.items() if k.startswith("Product_Claims"))
    wifi_v = spec.get("WiFi Connect")
    wifi = _yes_no(wifi_v)
    hw = re.search(r"(\d+(?:\.\d+)?)\s*H\s*x\s*(\d+(?:\.\d+)?)\s*W\s*x\s*(\d+(?:\.\d+)?)\s*D", fields.get("ProductDimensions", ""))
    ice, disp = spec.get("Icemaker"), spec.get("Dispenser")
    price = (po.get("price") or {}).get("without_tax", {}).get("value")
    benefit = []
    try:
        benefit = json.loads(po.get("warranty") or "{}").get("BenefitCopy") or []
    except ValueError:
        pass
    feats = list(dict.fromkeys(_clean(b["Feature"]) for b in benefit if b.get("Feature")))
    if re.search(r"energy star", claims + " " + title, re.I):
        es = True
    else:
        es = False if (major in ("refrigerator", "washer") and claims) else None
    elec = _electrical(spec)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=title or model, product_url=url,
        category=major, subcategory=sub,
        door_style=_door_style(spec.get("Configuration", ""), title, spec.get("Product Type", "")) if fridge else None,
        finish_color=spec.get("Color Appearance") or fields.get("Color") or None,
        price_usd=float(price) if price else None,
        capacity_total_cuft=_first_num(spec.get("Total Capacity (cubic feet)")),
        capacity_fridge_cuft=_first_num(spec.get("Fresh Food Capacity")) if fridge else None,
        capacity_freezer_cuft=_first_num(spec.get("Freezer Capacity")) if fridge else None,
        width_in=_inches(spec.get("Overall Width")) or (float(hw.group(2)) if hw else None),
        height_in=_inches(spec.get("Overall Height")) or (float(hw.group(1)) if hw else None),
        depth_in=_inches(spec.get("Overall Depth")) or (float(hw.group(3)) if hw else None),
        weight_lb=_first_num(spec.get("Net Weight")) or _first_num(spec.get("Approximate Shipping Weight")),
        voltage_v=elec["voltage_v"], amps=elec["amps"], frequency_hz=elec["frequency_hz"],
        energy_kwh_year=_first_num(spec.get("Energy Consumption (kWh/year)")),
        energy_star=es,
        ice_maker=(None if not ice else (not re.match(r"(none|no\b|optional)", ice, re.I))) if fridge else None,
        water_dispenser=(None if not disp else (bool(re.search(r"water", disp, re.I))
                                                 and not re.match(r"(none|non-dispenser)", disp, re.I))) if fridge else None,
        wifi_supported=wifi,
        wifi_evidence=f"GE spec table: WiFi Connect = {wifi_v}" if wifi_v else None,
        pod_features=feats,
        extra_specs={**full_spec_table(po), **extra_specs(major, sub, spec, rows)},
        image_url=main_image_url(po),
        **signals(fields.get("AverageOverallRating"), fields.get("TotalReviewCount"), fields.get("Product_First_Distribution_Date")),
    )
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=k, value=v) for s, k, v in rows]
    if claims:
        raw.append(RawSpec(brand=BRAND, model_number=model, source="web", section="Claims", key="Product Claims", value=claims))
    return record, raw, doc_links(po)


def model_from_url(url: str) -> str:
    u = urlparse(url)
    m = re.search(r"/appliance/(?:.*-)?([A-Za-z0-9]{4,})/?$", u.path)
    if u.scheme != "https" or not _ge_host(url) or not m:
        raise ValueError(f"not a GE Appliances US product URL: {url}")
    return m.group(1).upper()


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    url_model = model_from_url(url)
    po = extract_product(fetch_pdp_html(url))
    product, raw, links = parse_product(url, po)
    if product.model_number != url_model:
        print(f"warning: URL model {url_model} != page model {product.model_number}", file=sys.stderr)
    docs = []
    for dtype, link in links:
        if not _pdf_url_ok(link):
            print(f"skipped {dtype}: PDF url not https on an allowed host ({link[:100]})", file=sys.stderr)
            continue
        time.sleep(DELAY_S)
        d = common.download_pdf(BRAND, _safe_name(product.model_number), dtype, link)
        if d:
            docs.append(d)
    return product, docs, raw
