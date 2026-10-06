"""Maytag US adapter (catalog.py contract) + the Whirlpool-Corp OCC engine shared with jennair_us / amana_us.

Same platform as whirlpool_us.py / kitchenaid_us.py: Hybris OCC JSON (`/ws/v2/<site>/products/...`) and the AEM
document search, called with fetch() from inside a real browser page (Akamai 403s plain `requests`, also for PDFs).
New headless (channel="chromium") passes Akamai; a visible window is the fallback.
Differences from the older adapters (verified live on maytag.com / jennair.com / amana.com):
  * Spec table: the FULL OCC product carries `classifications` (section -> features with values + unit symbol), the same
    content the PDP shows. No AEM spec JSON is needed (its path differs per brand and is absent on JennAir/Amana).
  * Classification (`classify`) is name + primary category/URL based, shared by discover and scrape. The sites file
    products under inconsistent categories (Kitchen-Suite, D2C-Focus, duplicated Kitchen|Ranges|... trees).
  * Only products whose OCC `brand` is the site's own brand (or absent, JennAir) are candidates: maytag.com and
    amana.com also list Whirlpool / Unbranded hoods and microwaves.
Per-brand modules (`jennair_us.py`, `amana_us.py`) only define their `Site` and which sub keys the site really sells.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

PAGE_SIZE = 100
DELAY_S = 1.0
MAX_PDFS = 5
DOCS_URL = ("/services/search/contents.json?notincludeEmptyLang=false&query=%3Aformat%3Aapplication%2Fpdf"
            "%2Capplication%2Fzip%3Aproduct_sku%3A{m}&pdpdoc=true&pageSize=20&sort=created-desc"
            "&currentPage=1&fields=FULL&lang=en_us")
# site doc_type -> (our doc_type, priority); cad / warranty / dispensing guides etc. are skipped
DOC_MAP = {"energy-guide": ("EnergyGuide", 0), "owners-manual": ("Manual", 1), "product-guide": ("Manual", 1.5),
           "dimension-guide": ("SpecSheet", 2), "feature-sheet": ("SpecSheet", 2.5),
           "installation-instructions": ("Installation", 3), "safety-and-installation-instructions": ("Installation", 3),
           "instruction-sheet": ("Installation", 3.5), "reference-sheet": ("QuickSpecs", 4),
           "quick-start-guide": ("QuickSpecs", 4.5)}
CONSENT_DECLINE = ["Reject All", "Decline", "Reject"]
CONSENT_ACCEPT = ["Accept All", "Accept", "Allow All", "I Agree"]
IMAGE_QUERY = "?fmt=jpeg&wid=1200"  # bare Scene7 URL answers a tiny AVIF thumbnail (same platform as KitchenAid)

_headless_ok: bool | None = None  # mode that worked last; only a PREFERRED ORDER for auto mode


@dataclass(frozen=True)
class Site:
    brand: str                                  # display brand (catalog.BRAND_META key)
    base: str                                   # https://www.<host>
    host: str                                   # registrable domain pages / PDFs / images must be on
    occ: str                                    # OCC base path, e.g. /ws/v2/maytag-us
    codes: dict                                 # sub key -> OCC category codes to query (None = whole catalog)
    page: str = "/"                             # real page used to open the browser session

    @property
    def supported(self) -> set[str]:
        return set(self.codes)

    @property
    def brand_key(self) -> str:
        return re.sub(r"[^a-z]", "", self.brand.lower())


# ---------------------------------------------------------------- browser
def _modes() -> list[bool]:
    """Headless flags to try, in order. FRIDGE_BROWSER_MODE (auto | headless | visible) is read on every call."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    first = True if _headless_ok is None else _headless_ok
    return [first, not first]


def reset_browser_mode() -> None:
    """Forget which headless flag worked (server calls this when the browser mode setting changes)."""
    global _headless_ok
    _headless_ok = None


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _site_host(site: Site, url: str) -> bool:
    return _host_in(url, (site.host,))


def _pdf_url_ok(site: Site, url: str) -> bool:
    return urlparse(url).scheme == "https" and _site_host(site, url)


def _check_final_url(site: Site, url: str) -> None:
    if urlparse(url).scheme != "https" or not _site_host(site, url):
        raise ValueError(f"unexpected host after navigation: {url[:120]}")


def _click_first(page, labels: list[str]) -> bool:
    for label in labels:
        try:
            page.get_by_role("button", name=re.compile(rf"^{label}", re.I)).first.click(timeout=1500)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _dismiss_consent(page) -> None:
    """Best-effort: decline non-essential cookies; accept only if the banner is still blocking afterwards."""
    if _click_first(page, CONSENT_DECLINE):
        return
    try:
        blocking = page.locator("#onetrust-consent-sdk #onetrust-banner-sdk").is_visible()
    except PlaywrightError:
        blocking = False
    if blocking:
        _click_first(page, CONSENT_ACCEPT)


def _launch(p, headless: bool):
    if headless:
        try:  # new headless: old headless-shell is rejected by Akamai at the HTTP/2 layer
            return p.chromium.launch(headless=True, channel="chromium")
        except PlaywrightError:
            return common.launch_browser(p, headless=True)
    return common.launch_browser(p, headless=False)


class _Session:
    """Browser page on the brand site; fetches same-origin JSON / bytes with the page's cookies."""

    def __init__(self, page, label: str = "site"):
        self.page = page
        self.label = label

    def text(self, path: str) -> str:
        status, body = self.page.evaluate(
            "async u=>{const r=await fetch(u);return [r.status,await r.text()]}", path)
        if status != 200:
            raise RuntimeError(f"{self.label} fetch {path[:90]} -> HTTP {status}")
        return body

    def json(self, path: str):
        return json.loads(self.text(path))

    def pdf(self, path: str) -> bytes:
        js = ("async u=>{const r=await fetch(u);const b=new Uint8Array(await r.arrayBuffer());let s='';"
              "for(let i=0;i<b.length;i+=32768)s+=String.fromCharCode.apply(null,b.subarray(i,i+32768));"
              "return [r.status,btoa(s)]}")
        status, b64 = self.page.evaluate(js, path)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return base64.b64decode(b64)


def _close_quietly(browser, label: str = "maytag_us") -> None:
    """Close a browser in an error path; a failing close must not abort the mode-fallback loop."""
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"{label}: browser.close() failed: {e}", file=sys.stderr)


def _connect(p, site: Site, url: str):
    """Return (browser, page) on url; headless first, visible fallback if blocked or navigation fails."""
    global _headless_ok
    last_err: Exception | None = None
    _check_final_url(site, url)
    modes = _modes()
    for headless in modes:
        browser = None
        try:
            browser = _launch(p, headless)
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _check_final_url(site, page.url)
            try:  # slow render is not "blocked": wait for real content before judging
                page.wait_for_function("document.body && document.body.innerText.length >= 200", timeout=15000)
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            status = resp.status if resp else None
            if common.looks_blocked(status, page.inner_text("body")):
                raise RuntimeError(f"blocked (status {status})")
            _dismiss_consent(page)
            _headless_ok = headless
            return browser, page
        except (PlaywrightTimeoutError, PlaywrightError, RuntimeError, ValueError) as e:
            last_err = e
            _close_quietly(browser, site.brand.lower() + "_us")
        except BaseException:  # incl. KeyboardInterrupt: never leak the browser
            _close_quietly(browser, site.brand.lower() + "_us")
            raise
    raise RuntimeError(f"{site.brand} page unreachable (modes tried: {modes}): {last_err}")


@contextmanager
def _session(site: Site, url: str):
    with sync_playwright() as p:
        browser, page = _connect(p, site, url)
        try:
            yield _Session(page, site.brand)
        finally:
            browser.close()


# ---------------------------------------------------------------- classification (shared by discover and scrape)
_MARKS = re.compile(r"[®™]")
_EXCLUDE = re.compile(r"refurbish|downdraft system|liner|blower|ventilation|dishwasher|compactor|warming drawer|coffee"
                      r"|wine|beverage|ice machine|undercounter ice|accessor|\bkits?\b|nozzle|\bfilter\b")
_COOK_TOP = re.compile(r"cooktop|cook top|rangetop|range top")
_OTR = re.compile(r"over[- ]the[- ]range|oven hood combination|microwave hood")
_SCO = re.compile(r"combo|combination|speed[- ]?(cook|oven)|7-in-1")


def _norm(text) -> str:
    return " ".join(_MARKS.sub("", str(text or "")).lower().split())


def _loc(prod: dict) -> str:
    """Primary category path + URL path: the only location facts present in both OPT listings and FULL products."""
    return _norm(((prod.get("primaryCategory") or {}).get("pipedCategory") or "") + " "
                 + urlparse(prod.get("url") or "").path)


def _fridge_sub(n: str, loc: str) -> str | None:
    if re.search(r"built[- ]?in|column", n) or re.search(r"built-in-refrigerators|\|columns", loc):
        return "built_in"
    if re.search(r"under ?counter|\bcompact\b|(double|single)[- ]drawer|refrigerator drawers?", n) or "undercounter" in loc:
        return "compact"
    for sub, pat in (("french_door", r"french[- ]door"), ("side_by_side", r"side[- ]by[- ]side"),
                     ("top_freezer", r"top[- ]?(freezer|mount)"), ("bottom_freezer", r"bottom[- ]?(freezer|mount)")):
        if re.search(pat, n):
            return sub
    for sub, pat in (("french_door", r"french-door"), ("side_by_side", r"side-by-side"),
                     ("top_freezer", r"top-freezer"), ("bottom_freezer", r"bottom-freezer")):
        if re.search(pat, loc):
            return sub
    return None


def _cook_sub(n: str, loc: str, is_range: bool) -> str | None:
    """Fuel decides: induction -> induction; gas / dual fuel -> gas_oven (range with oven) or gas_cooktop; else radiant."""
    if "induction" in n or "induction" in loc:
        return "induction"
    if re.search(r"\bgas\b|dual[- ]fuel", n):
        return "gas_oven" if is_range else "gas_cooktop"
    if re.search(r"electric|radiant|ceramic|smooth|coil", n):
        return "radiant"
    if re.search(r"dual-fuel", loc):
        return "gas_oven" if is_range else "gas_cooktop"
    if re.search(r"\bgas\b|\|gas", loc):
        return "gas_oven" if is_range else "gas_cooktop"
    if "electric" in loc:
        return "radiant"
    return None


def classify(prod: dict) -> tuple[str, str | None]:
    """(major key, sub key or None); major 'other' for anything the benchmark does not cover (hoods, freezers,
    dishwashers, wine/beverage coolers, parts). Works on OPT listing items and FULL products alike."""
    n, loc = _norm(prod.get("name")), _loc(prod)
    if re.search(r"^parts|\|parts|parts-&", loc) or _EXCLUDE.search(n):
        return "other", None
    if re.search(r"\bhoods?\b", n) and not re.search(r"microwave|oven", n):
        return "other", None
    if re.search(r"\bfreezers?\b", n) and "refrigerator" not in n:
        return "other", None
    if "griddle" in n and not re.search(r"range|cooktop", n):
        return "other", None
    if "refrigerator" in n:
        return "refrigerator", _fridge_sub(n, loc)
    if re.search(r"laundry (center|tower)|wash ?tower|all[- ]in[- ]one|washer[- ]dryer", n):
        return "washer", "laundry_center"
    if re.search(r"\bdryers?\b", n):
        return "washer", "dryer"
    if re.search(r"\bwashers?\b", n):
        sub = ("front_load" if "front load" in n else "top_load" if "top load" in n
               else "front_load" if "front-load" in loc else "top_load" if "top-load" in loc else None)
        return "washer", sub
    if _OTR.search(n) or "over-the-range" in loc:
        return "cooking", "otr"
    if _SCO.search(n) and re.search(r"oven|microwave", n):
        return "cooking", "sco"
    if "microwave" in n:
        return "cooking", "microwave"
    if re.search(r"\brange\b", n):
        return "cooking", _cook_sub(n, loc, True)
    if _COOK_TOP.search(n):
        return "cooking", _cook_sub(n, loc, False)
    if "oven" in n or "wall-oven" in loc:
        return "cooking", "electric_oven"
    return "other", None


# ---------------------------------------------------------------- discover
def _brand_ok(site: Site, p: dict) -> bool:
    b = re.sub(r"[^a-z]", "", (p.get("brand") or "").lower())
    return not b or b == site.brand_key


def _price(p: dict) -> float | None:
    """Selling price only: a member price is never used (the site flags it with isMemberPrice)."""
    if p.get("isMemberPrice"):
        return None
    value = (p.get("price") or p.get("baseDisplayPrice") or {}).get("value")
    return float(value) if value else None


def occ_signals(p: dict) -> dict:
    """Consumer signals the site's own OCC JSON publishes (listing and FULL product): averageRating (5 point) and
    numberOfReviews. Without reviews the rating is meaningless, so both keys are omitted (= unknown). The OCC API
    exposes no new flag and no release date."""
    try:
        count, rating = int(p.get("numberOfReviews")), float(p.get("averageRating"))
    except (TypeError, ValueError):
        return {}
    if count <= 0 or not 0 <= rating <= 5:
        return {}
    return {"rating": round(rating, 2), "review_count": count}


def site_parse_search(site: Site, data: dict, sub_key: str) -> list[Candidate]:
    """Candidates of a listing page that classify() (the rule scrape() uses) files under sub_key."""
    out, other = [], 0
    for p in data.get("products", []):
        if not p.get("code") or not p.get("url") or not _brand_ok(site, p):
            continue
        major, sub = classify(p)
        if sub != sub_key:
            other += 1
            continue
        url = urljoin(site.base, p["url"])
        if not _site_host(site, url) or urlparse(url).scheme != "https":
            print(f"dropped off-domain candidate {p['code']}: {p['url'][:100]}", file=sys.stderr)
            continue
        out.append(Candidate(brand=site.brand, model_number=p["code"], name=_MARKS.sub("", p.get("name") or ""),
                             url=url, price_usd=_price(p), category=major, subcategory=sub_key,
                             attrs=(sig := occ_signals(p)), attrs_src={k: "listing" for k in sig}))
    if other:
        print(f"{site.brand.lower()}_us: parse_search({sub_key}): {other} listed product(s) classified under another "
              f"sub key or none, skipped", file=sys.stderr)
    return out


def _search_path(site: Site, code: str | None, page_no: int) -> str:
    query = f":relevance:category:{code}:showMajorProductsOnly:true" if code else ":relevance:showMajorProductsOnly:true"
    return (f"{site.occ}/products/search/singlesource?query={quote(query, safe='')}&pageSize={PAGE_SIZE}"
            f"&fields=OPT&lang=en_US&currentPage={page_no}&isBundle=false")


def site_discover(site: Site, subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in site.codes:
        raise ValueError(f"{site.brand} US adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    with _session(site, site.base + site.page) as s:
        first = True
        for code in site.codes[subcategory]:
            page_no, total_pages = 0, 1
            while len(found) < limit and page_no < total_pages:
                if not first:
                    time.sleep(DELAY_S)
                first = False
                data = s.json(_search_path(site, code, page_no))
                if "products" not in data or "pagination" not in data:
                    raise ValueError(f"{site.brand} listing JSON structure unrecognised")
                total_pages = data["pagination"]["totalPages"]
                for c in site_parse_search(site, data, subcategory):
                    found.setdefault(c.model_number, c)
                page_no += 1
            if len(found) >= limit:
                break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def site_model_from_url(site: Site, url: str) -> str:
    u = urlparse(url)
    m = re.search(r"/p\.[^/]*\.([a-z0-9]{5,})\.html$", u.path, re.I)
    if u.scheme != "https" or not _site_host(site, url) or not m:
        raise ValueError(f"not a {site.brand} US product URL: {url}")
    return m.group(1).upper()


def _first_num(v: str | None) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", v or "")
    return float(m.group(0).replace(",", "")) if m else None


_TAGS = re.compile(r"<[^>]+>")


def _clean(v) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", str(v))).strip()


def spec_rows(prod: dict) -> list[tuple[str, str, str]]:
    """(section, label, value) from the FULL product's `classifications`; value = values joined with ', ' + unit
    symbol (same text the PDP spec table shows, e.g. '35.938 in', '28.9 cu. ft.')."""
    rows = []
    for sec in prod.get("classifications") or []:
        for feat in sec.get("features") or []:
            vals = [_clean(v.get("value")) for v in feat.get("featureValues") or [] if v.get("value") not in (None, "")]
            vals = [v for v in vals if v]
            if not vals:
                continue
            unit = next((u.get("symbol") for u in feat.get("featureUnits") or [] if u.get("symbol")), "")
            rows.append((sec.get("name") or "", feat.get("name") or "", ", ".join(vals) + (f" {unit}" if unit else "")))
    return rows


def _noise(t: str) -> str:
    return _MARKS.sub("", t).strip()


def full_spec_table(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """EVERY Specs & Details row: 'Section > Label' -> full value."""
    out: dict[str, str] = {}
    for sec, k, v in rows:
        out.setdefault(f"{_noise(sec)} > {_noise(k)}", _noise(v))
    return out


def site_main_image_url(site: Site, prod: dict) -> str | None:
    """Hero image: OCC `picture` (else `thumbnail`) Scene7 path -> absolute https URL at 1200 px wide JPEG."""
    for key in ("picture", "thumbnail"):
        raw = prod.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        url = urljoin(site.base + "/", raw.strip())
        if urlparse(url).scheme == "https" and _host_in(url, (site.host, "scene7.com")):
            return url + ("" if "?" in url else IMAGE_QUERY)
    return None


def _yes_no(v: str | None) -> bool | None:
    if v is None:
        return None
    if re.match(r"(yes|true)\b", v, re.I):
        return True
    if re.match(r"(no|none|n/a)\b", v, re.I):
        return False
    return None


def _energy_star(f: dict[str, str]) -> bool | None:
    v = next((val for k, val in f.items() if re.match(r"energy star", k, re.I)), None)
    if not v:
        return None
    return True if re.search(r"energy star|^yes", v, re.I) else False if _yes_no(v) is False else None


def _wifi(f: dict[str, str]) -> tuple[bool | None, str | None]:
    conn, works, smart = f.get("Connectivity"), f.get("Works With"), f.get("Smart Appliance")
    seen = [(k, v) for k, v in (("Connectivity", conn), ("Works With", works), ("Smart Appliance", smart)) if v]
    if not seen:
        return None, None
    ev = "Smart Compatibility: " + "; ".join(f"{k} = {v}" for k, v in seen)
    if any(re.search(r"wi-?fi|app|alexa|google|ios|android", v, re.I) or _yes_no(v) is True for _, v in seen):
        return True, ev
    return (False, ev) if any(_yes_no(v) is False for _, v in seen) else (None, ev)


_FRIDGE_CAPACITY_KEYS = ("Total Refrigerator Capacity Volume", "Capacity")
_CAPACITY_KEYS = _FRIDGE_CAPACITY_KEYS + ("Total Capacity", "Washer Capacity", "Dryer Capacity", "Oven Capacity",
                                          "Total Oven Capacity", "Capacity Volume")


def site_parse_product(site: Site, model: str, url: str, prod: dict) -> tuple[ProductRecord, list[RawSpec]]:
    rows = spec_rows(prod) if isinstance(prod, dict) else []
    if not rows or not prod.get("name"):
        raise ValueError(f"{site.brand} PDP data structure unrecognised for {model}")
    major, sub = classify(prod)
    if major == "other":
        raise ValueError(f"not a supported {site.brand} product category for {model} ({prod['name'][:60]!r})")
    f: dict[str, str] = {}
    for _, k, v in rows:
        f.setdefault(k, v)
    fridge = major == "refrigerator"
    ice, disp = f.get("Icemaker"), f.get("Dispenser Type")
    wifi, wifi_ev = _wifi(f)
    name = _MARKS.sub("", prod["name"])
    cap = next((_first_num(f[k]) for k in _CAPACITY_KEYS if f.get(k)), None)
    if cap is None and (m := re.search(r"(\d+(?:\.\d+)?)\s*cu\.?\s*ft", name, re.I)):
        cap = float(m.group(1))
    feats = list(dict.fromkeys(_noise(x["title"]) for x in prod.get("productFeature") or [] if x.get("title")))
    record = ProductRecord(
        brand=site.brand, model_number=model, product_name=name, product_url=url, category=major, subcategory=sub,
        door_style=(f.get("Door Style Configuration") or f.get("Refrigerator Type")) if fridge else None,
        finish_color=f.get("Door Color") or (prod.get("color") or {}).get("name") or None,
        price_usd=_price(prod),
        capacity_total_cuft=cap,
        capacity_fridge_cuft=_first_num(f.get("Refrigerator Capacity Volume")) if fridge else None,
        capacity_freezer_cuft=_first_num(f.get("Freezer Capacity Volume")) if fridge else None,
        width_in=_first_num(f.get("Width")), height_in=_first_num(f.get("Height")),
        depth_in=_first_num(f.get("Depth")), weight_lb=_first_num(f.get("Net Weight")),
        voltage_v=f.get("Volts"), amps=_first_num(f.get("Amps")), frequency_hz=_first_num(f.get("Hz")),
        energy_star=_energy_star(f), wifi_supported=wifi, wifi_evidence=wifi_ev, pod_features=feats,
        ice_maker=(None if not ice else _yes_no(ice) is not False) if fridge else None,
        water_dispenser=(None if not disp else ("water" in disp.lower())) if fridge else None,
        extra_specs=full_spec_table(rows), image_url=site_main_image_url(site, prod), **occ_signals(prod))
    raw = [RawSpec(brand=site.brand, model_number=model, source="web", section=sec, key=k, value=v)
           for sec, k, v in rows]
    return record, raw


def site_parse_docs(site: Site, data: dict) -> list[tuple[str, str]]:
    """(our doc_type, absolute url) for English-US PDFs on the site host, priority-ordered, capped at MAX_PDFS."""
    picked: dict[str, tuple[float, str]] = {}
    for d in data.get("documents", []):
        kinds = [k for k in d.get("doc_type", []) if k in DOC_MAP]
        asset = d.get("asseturl", "")
        if not kinds or "en_us" not in d.get("language", []) or not asset.lower().endswith(".pdf"):
            continue
        url = urljoin(site.base, asset)
        if not _pdf_url_ok(site, url):
            continue
        dtype, prio = DOC_MAP[kinds[0]]
        if dtype not in picked or prio < picked[dtype][0]:
            picked[dtype] = (prio, url)
    return [(t, u) for t, (_, u) in sorted(picked.items(), key=lambda kv: kv[1][0])][:MAX_PDFS]


def parse_energy_kwh(text: str) -> float | None:
    v = common.num(r"(\d[\d,]*)\s*kWh\s*\n\s*Estimated Yearly Electricity Use", text, re.I)
    if v is not None:
        return v
    m = re.search(r"(\d[\d,]*)\s*kWh\s*\n\s*(\d[\d,]*)\s*kWh\s*\n\s*(\d[\d,]*)\s*\n", text)
    if not m:
        return None
    a, b, n = (float(g.replace(",", "")) for g in m.groups())
    return n if min(a, b) <= n <= max(a, b) else None


def _dest_path(site: Site, model: str, doc_type: str):
    """downloads/<brand>/<model>_<doc_type>.pdf with sanitised parts; must resolve inside downloads/."""
    dest = (common.DOWNLOADS / common._safe_name(site.brand, "unknown").lower()
            / f"{common._safe_name(model, 'x')}_{common._safe_name(doc_type, 'x')}.pdf")
    if not dest.resolve().is_relative_to(common.DOWNLOADS.resolve()):
        raise ValueError(f"download path escapes downloads/: {dest}")
    return dest


def _download(site: Site, s: _Session, model: str, doc_type: str, url: str) -> DocumentRecord | None:
    """Fetch a PDF through the browser page (plain HTTP is 403'd) and store it like common.download_pdf."""
    import fitz
    dest = _dest_path(site, model, doc_type)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _pdf_url_ok(site, url):
            raise ValueError("PDF url not https on an allowed host")
        data = s.pdf(url)
        if data[:4] != b"%PDF" or len(data) > common.MAX_PDF_BYTES:
            raise ValueError("not a PDF or too large")
        dest.write_bytes(data)
        with fitz.open(dest) as d:
            pages = d.page_count
    except Exception as e:  # PlaywrightError, fitz errors, HTTP errors: skip this document only
        print(f"skipped {doc_type} ({url}): {e}", file=sys.stderr)
        dest.unlink(missing_ok=True)
        return None
    return DocumentRecord(brand=site.brand, model_number=model, doc_type=doc_type, source_url=url,
                          local_path=str(dest.relative_to(common.ROOT)),
                          sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), pages=pages)


def site_scrape(site: Site, url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = site_model_from_url(site, url)
    with _session(site, url) as s:
        prod = s.json(f"{site.occ}/products/{model}?fields=FULL&lang=en_US")
        time.sleep(DELAY_S)
        docs_json = s.json(DOCS_URL.format(m=model))
        product, raw = site_parse_product(site, model, url, prod)
        docs, kwh = [], None
        for dtype, durl in site_parse_docs(site, docs_json):
            time.sleep(DELAY_S)
            d = _download(site, s, model, dtype, durl)
            if d:
                docs.append(d)
                if dtype == "EnergyGuide" and product.category == "refrigerator":
                    kwh = parse_energy_kwh(common.pdf_text(common.ROOT / d.local_path))
                    if kwh is None:
                        print(f"warning: no kWh/year found in Energy Guide for {model}", file=sys.stderr)
    return (product.model_copy(update={"energy_kwh_year": kwh}) if kwh is not None else product), docs, raw


# ---------------------------------------------------------------- Maytag
BRAND = "Maytag"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"

# maytag.com sells (verified on the live catalog, 116 major products): French door / side-by-side / top and bottom
# freezer refrigerators, top and front load washers + dryers, OTR microwaves, wall ovens (incl. microwave combos),
# gas and electric ranges, gas / electric / induction cooktops. No built-in or compact refrigerators, no countertop
# or built-in microwaves, no laundry centers (stackable washers/dryers are separate units).
# The whole catalog is queried (one or two 100-item pages) and classify() sorts it: the site files products under
# inconsistent categories (Kitchen-Suite, D2C-Focus, duplicated Kitchen|Ranges|... trees).
SITE = Site(brand=BRAND, base="https://www.maytag.com", host="maytag.com", occ="/ws/v2/maytag-us",
            codes={sub: (None,) for sub in (
                "otr", "sco", "electric_oven", "gas_oven", "gas_cooktop", "induction", "radiant")})
# SCOPE (user instruction): only cooking sub keys are exposed for now. Refrigerators (french_door, side_by_side,
# top_freezer, bottom_freezer) and laundry (top_load, front_load, dryer) are sold and classify() handles them, but
# they are intentionally NOT in SUPPORTED_SUBCATEGORIES / not live-verified.
BASE = SITE.base
SUPPORTED_SUBCATEGORIES = SITE.supported


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return site_discover(SITE, subcategory, limit)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return site_scrape(SITE, url)


def parse_search(data: dict, sub_key: str) -> list[Candidate]:
    return site_parse_search(SITE, data, sub_key)


def parse_product(model: str, url: str, prod: dict) -> tuple[ProductRecord, list[RawSpec]]:
    return site_parse_product(SITE, model, url, prod)


def parse_docs(data: dict) -> list[tuple[str, str]]:
    return site_parse_docs(SITE, data)


def model_from_url(url: str) -> str:
    return site_model_from_url(SITE, url)


def main_image_url(prod: dict) -> str | None:
    return site_main_image_url(SITE, prod)
