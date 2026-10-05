"""KitchenAid US adapter: refrigeration and cooking (catalog.py contract).

Data source: the site's own JSON endpoints (Hybris OCC `/ws/v2/kitchenAid-us/...`, AEM spec JSON, AEM
document search), called with fetch() from inside a real browser page. The site sits behind Akamai, which
403s plain `requests` (also for PDFs), so everything - including PDF bytes - goes through the page.
Listing: one OCC category per product family (SEGMENT_CATEGORY, the codes the site's own PLP pages request);
SUB_RULES classifies each product into a catalog sub key from its PDP URL category segment + name.
KitchenAid US sells no laundry, so washer/dryer keys (plus top_freezer) are unsupported.
Browser: new headless chromium (channel="chromium") passes Akamai; old headless is rejected at HTTP/2, so
auto = new headless, then a visible window.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Callable, NamedTuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "KitchenAid"
BASE = "https://www.kitchenaid.com"
LISTING_PAGE = BASE + "/major-appliances/refrigeration/refrigerators.html"
OCC = "/ws/v2/kitchenAid-us"
SEARCH_Q = ":relevance:category:{cat}:showMajorProductsOnly:true"
SPEC_URL = ("/content/kitchenaid/en_us/products/majors/jcr:content/root/main/productTray/pdpTray/"
            "checkDimensions/par-section/specificationspdpv5.model.{m}.json")
DOCS_URL = ("/services/search/contents.json?notincludeEmptyLang=false&query=%3Aformat%3Aapplication%2Fpdf"
            "%2Capplication%2Fzip%3Aproduct_sku%3A{m}&pdpdoc=true&pageSize=20&sort=created-desc"
            "&currentPage=1&fields=FULL&lang=en_us")
PAGE_SIZE = 100
DELAY_S = 1.0
MAX_PDFS = 5
# site doc_type -> (our doc_type, priority); others (cad, warranty) skipped
DOC_MAP = {"energy-guide": ("EnergyGuide", 0), "owners-manual": ("Manual", 1), "dimension-guide": ("SpecSheet", 2),
           "instruction-sheet": ("Installation", 3), "reference-sheet": ("QuickSpecs", 4)}
CONSENT_BUTTONS = ["Reject All", "Decline", "Reject", "Accept All", "Accept", "Allow All", "I Agree"]

PAGE_HOSTS = ("kitchenaid.com",)
PDF_HOSTS = ("kitchenaid.com", "whirlpool.com")  # hosts observed serving KitchenAid PDFs

_headless_ok: bool | None = None  # mode that worked last; only a PREFERRED ORDER for auto mode


def _modes() -> list[bool]:
    """Headless flags to try, in order. FRIDGE_BROWSER_MODE (auto | headless | visible) is read on every call."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    first = True if _headless_ok is None else _headless_ok
    return [first, not first]


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _ka_host(url: str) -> bool:
    return _host_in(url, PAGE_HOSTS)


def _pdf_url_ok(url: str) -> bool:
    return urlparse(url).scheme == "https" and _host_in(url, PDF_HOSTS)


def _check_final_url(url: str) -> None:
    if not _ka_host(url):
        raise ValueError(f"unexpected host after navigation: {url[:120]}")


def _dismiss_consent(page) -> None:
    """Best-effort: decline non-essential cookies first, accept if only that is offered."""
    for label in CONSENT_BUTTONS:
        try:
            page.get_by_role("button", name=re.compile(rf"^{label}", re.I)).first.click(timeout=1500)
            return
        except PlaywrightTimeoutError:
            continue


class _Session:
    """Browser page on kitchenaid.com; fetches same-origin JSON / bytes with the page's cookies."""

    def __init__(self, page):
        self.page = page

    def text(self, path: str) -> str:
        status, body = self.page.evaluate(
            "async u=>{const r=await fetch(u);return [r.status,await r.text()]}", path)
        if status != 200:
            raise RuntimeError(f"KitchenAid fetch {path[:90]} -> HTTP {status}")
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


def _launch(p, headless: bool):
    if headless:
        try:  # new headless: Akamai rejects the old headless-shell at the HTTP/2 layer
            return p.chromium.launch(headless=True, channel="chromium")
        except PlaywrightError:
            return common.launch_browser(p, headless=True)
    return common.launch_browser(p, headless=False)


def _close_quietly(browser) -> None:
    """Close a browser in an error path; a failing close must not abort the mode-fallback loop."""
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"kitchenaid_us: browser.close() failed: {e}", file=sys.stderr)


def _connect(p, url: str):
    """Return (browser, page) on url; headless first, visible fallback if blocked or navigation fails."""
    global _headless_ok
    last_err: Exception | None = None
    _check_final_url(url)
    for headless in _modes():
        browser = None
        try:
            browser = _launch(p, headless)
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _check_final_url(page.url)
            try:  # slow render is not "blocked": wait for real content / network idle before judging
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
        except (PlaywrightTimeoutError, PlaywrightError, RuntimeError, ValueError) as e:  # goto raises net::ERR_* as Error
            last_err = e
            _close_quietly(browser)
        except BaseException:  # incl. KeyboardInterrupt: never leak the browser
            _close_quietly(browser)
            raise
    raise RuntimeError(f"KitchenAid page unreachable (modes tried: {_modes()}): {last_err}")


@contextmanager
def _session(url: str):
    with sync_playwright() as p:
        browser, page = _connect(p, url)
        try:
            yield _Session(page)
        finally:
            browser.close()


# ---------------------------------------------------------------- sub-category rules
class _Rule(NamedTuple):
    major: str
    segments: tuple      # /major-appliances/<segment>/ URL segments the product may live under
    pred: Callable[[str], bool]  # on the lower-cased product name


# URL segment of /major-appliances/<segment>/... -> OCC category code (found from the site's own PLP requests).
SEGMENT_CATEGORY = {
    "refrigeration": "MajorAppliancesRefrigeration", "ranges": "MajorAppliancesRanges",
    "wall-ovens": "MajorAppliancesWallOvens", "cooktops": "MajorAppliancesCooktops",
    "microwaves": "MajorAppliancesMicrowaves",
}
_OTR = re.compile(r"over[- ]the[- ]range|hood combination")
_BUILT_IN = re.compile(r"built[- ]?in")
_INDUCTION = re.compile(r"induction")
# sco (Speed Cook Oven): oven with a built-in microwave / speed-cook function (combo wall ovens, speed / more-in-one ovens)
_SCO = re.compile(r"combo wall oven|combination (wall )?oven|combo oven|speed oven|more-in-one")
_GAS_RANGE = re.compile(r"(?<![a-z])gas(?![a-z])|dual fuel")  # gas burners (dual fuel = gas cooktop + electric oven)
_NOT_FRIDGE = re.compile(r"wine|beverage")  # wine cellars / beverage centers are not refrigerators


def _is_fridge_name(n: str) -> bool:
    return "refrigerator" in n and not _NOT_FRIDGE.search(n)


def _fridge(pattern: str, allow_built_in: bool = False) -> Callable[[str], bool]:
    rx = re.compile(pattern)
    return lambda n: _is_fridge_name(n) and bool(rx.search(n)) and (allow_built_in or not _BUILT_IN.search(n))


# sub key -> rule. Order is the classification priority (first match wins). Edit the mapping here only.
# Not sold by KitchenAid US (no matching category/products): top_freezer, washer/dryer keys.
SUB_RULES: dict[str, _Rule] = {
    "built_in": _Rule("refrigerator", ("refrigeration",), _fridge(_BUILT_IN.pattern, allow_built_in=True)),
    "french_door": _Rule("refrigerator", ("refrigeration",), _fridge(r"french door|multi-door")),
    "side_by_side": _Rule("refrigerator", ("refrigeration",), _fridge(r"side[- ]by[- ]side")),
    "bottom_freezer": _Rule("refrigerator", ("refrigeration",), _fridge(r"bottom (mount|freezer)")),
    "compact": _Rule("refrigerator", ("refrigeration",), _fridge(r"undercounter")),
    "otr": _Rule("cooking", ("microwaves", "wall-ovens"), lambda n: bool(_OTR.search(n))),
    # sco: microwave-combination wall ovens and built-in speed / more-in-one ovens (never OTR, never a plain oven)
    "sco": _Rule("cooking", ("wall-ovens", "microwaves"), lambda n: not _OTR.search(n) and bool(_SCO.search(n))),
    "microwave": _Rule("cooking", ("microwaves",), lambda n: not _OTR.search(n) and not _SCO.search(n)),
    "induction": _Rule("cooking", ("ranges", "cooktops"), lambda n: bool(_INDUCTION.search(n))),
    # ASSUMPTION: gas_oven = gas and dual-fuel RANGES (KitchenAid sells no gas wall oven); gas cooktops/rangetops are no oven.
    "gas_oven": _Rule("cooking", ("ranges",), lambda n: bool(_GAS_RANGE.search(n))),
    # ASSUMPTION: radiant = electric ranges and electric/radiant cooktops (induction excluded by its earlier rule).
    "radiant": _Rule("cooking", ("ranges", "cooktops"),
                     lambda n: bool(re.search(r"electric|radiant", n)) and not _INDUCTION.search(n)
                     and not _GAS_RANGE.search(n)),
    # ASSUMPTION: electric_oven = single/double wall ovens without a microwave/speed-cook function.
    "electric_oven": _Rule("cooking", ("wall-ovens",), lambda n: not _OTR.search(n) and not _SCO.search(n)),
}
SUPPORTED_SUBCATEGORIES = set(SUB_RULES)
_MAJOR_BY_SEGMENT = {"refrigeration": "refrigerator", "ranges": "cooking", "wall-ovens": "cooking",
                     "cooktops": "cooking", "microwaves": "cooking"}


def _segment(url: str) -> str | None:
    m = re.search(r"/major-appliances/([^/]+)/", urlparse(url).path)
    return m.group(1) if m else None


def _family(url: str, name: str) -> str | None:
    """Product family (a SEGMENT_CATEGORY key): the PDP URL's category segment, or - for URLs filed under
    other segments such as /black-stainless/ or /hoods-and-vents/ - derived from the name."""
    seg = _segment(url)
    if seg in SEGMENT_CATEGORY:
        return seg
    n = (name or "").lower()
    for fam, pat in (("refrigeration", r"refrigerator"), ("microwaves", r"microwave|over[- ]the[- ]range"),
                     ("wall-ovens", r"wall oven|combination oven|single oven|double oven"),
                     ("cooktops", r"cooktop|rangetop"), ("ranges", r"range")):
        if re.search(pat, n):
            return fam
    return None


def _classify(fam: str | None, name: str) -> str | None:
    """First SUB_RULES entry (dict order = priority) accepting the family + name. The only classification
    rule: discover() and scrape() both go through it, so a product has exactly one sub key."""
    n = (name or "").lower()
    return next((s for s, r in SUB_RULES.items() if fam in r.segments and r.pred(n)), None)


def infer_subcategory(url: str, name: str) -> str | None:
    """Sub key from the product's family (URL segment / name) + name; None when no rule matches."""
    return _classify(_family(url, name), name)


# ---------------------------------------------------------------- discover

def _is_refrigerator(p: dict) -> bool:
    return _is_fridge_name((p.get("name") or "").lower())


def parse_search(data: dict, sub: str | None = None, segment: str | None = None) -> list[Candidate]:
    """OCC search page -> Candidates. With `sub`, only products its rule accepts (tagged category/subcategory);
    `segment` (the queried family) is only a fallback when a product's own URL/name gives no family; without `sub`,
    the legacy refrigerator-only filter."""
    rule = SUB_RULES[sub] if sub else None
    out, other = [], 0
    for p in data.get("products", []):
        if not p.get("code") or not p.get("url"):
            continue
        url = urljoin(BASE, p["url"])
        if rule:
            # the product's own family (what scrape() sees); the queried segment only when the URL says nothing
            if _classify(_family(url, p.get("name")) or segment, p.get("name") or "") != sub:
                other += 1
                continue
        elif not _is_refrigerator(p):
            continue
        if not _ka_host(url):
            print(f"dropped off-domain candidate {p['code']}: {p['url'][:100]}", file=sys.stderr)
            continue
        price = (p.get("price") or p.get("baseDisplayPrice") or {}).get("value")
        extra = {"category": rule.major, "subcategory": sub} if rule else {}
        out.append(Candidate(brand=BRAND, model_number=p["code"], name=p["name"], url=url,
                             price_usd=float(price) if price else None, **extra))
    if other:
        print(f"kitchenaid_us: parse_search({sub}): {other} listed product(s) classified under another sub key "
              f"or none, skipped", file=sys.stderr)
    return out


def _search_url(category: str, page_no: int) -> str:
    q = SEARCH_Q.format(cat=category).replace(":", "%3A")
    return (f"{OCC}/products/search/singlesource?query={q}&pageSize={PAGE_SIZE}"
            f"&fields=OPT&lang=en_US&currentPage={page_no}&isBundle=false")


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"KitchenAid US adapter does not support sub category {subcategory!r}")
    rule = SUB_RULES[subcategory]
    found: dict[str, Candidate] = {}
    with _session(LISTING_PAGE) as s:
        first = True
        for seg in rule.segments:
            page_no, total_pages = 0, 1
            while len(found) < limit and page_no < total_pages:
                if not first:
                    time.sleep(DELAY_S)
                first = False
                data = s.json(_search_url(SEGMENT_CATEGORY[seg], page_no))
                if "products" not in data or "pagination" not in data:
                    raise ValueError("KitchenAid listing JSON structure unrecognised")
                total_pages = data["pagination"]["totalPages"]
                for c in parse_search(data, subcategory, seg):
                    found.setdefault(c.model_number, c)
                page_no += 1
            if len(found) >= limit:
                break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape

def model_from_url(url: str) -> str:
    u = urlparse(url)
    m = re.search(r"\.([a-z0-9]{6,})\.html$", u.path, re.I)
    if u.scheme != "https" or not _ka_host(url) or not m:
        raise ValueError(f"not a KitchenAid US product URL: {url}")
    return m.group(1).upper()


def _first_num(v: str | None) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", v or "")
    return float(m.group(0).replace(",", "")) if m else None


def _flatten(spec: dict) -> dict[str, str]:
    return {x["name"]: str(x["value"]) for sec in spec["specSections"] for x in sec["specs"]
            if x.get("value") not in (None, "")}


def _noise(t) -> str:
    """Strip registered/trademark marks, collapse whitespace; line breaks inside a value become ' | '."""
    lines = [" ".join(l.replace("®", "").replace("™", "").split()) for l in str(t).splitlines()]
    return " | ".join(l for l in lines if l)


def full_spec_table(spec: dict) -> dict[str, str]:
    """EVERY Specs & Details row: 'Section > Label' -> full value."""
    out: dict[str, str] = {}
    for sec in spec["specSections"]:
        for x in sec["specs"]:
            if x.get("value") not in (None, "") and _noise(x["value"]):
                out.setdefault(f"{_noise(sec['name'])} > {_noise(x['name'])}", _noise(x["value"]))
    return out


IMAGE_HOSTS = ("kitchenaid.com", "whirlpool.com", "scene7.com")
IMAGE_QUERY = "?fmt=jpeg&wid=1200"  # bare Scene7 URL answers a 1.4 KB AVIF thumbnail; verified on the live site


def main_image_url(prod: dict) -> str | None:
    """Hero image: OCC `picture` (else `thumbnail`) Scene7 path -> absolute https URL at 1200 px wide JPEG."""
    for key in ("picture", "thumbnail"):
        raw = prod.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        url = urljoin(BASE + "/", raw.strip())
        if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS):
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


def _energy_star(v: str | None) -> bool | None:
    if not v:
        return None
    return True if re.search(r"energy star", v, re.I) else False if _yes_no(v) is False else None


# Curated category-specific specs: (label, KitchenAid spec keys (first present wins), "num" keeps only the number).
_NUM = "num"
_EXTRA: dict[str, list[tuple]] = {
    "refrigerator": [("Installation", ("Installation Configuration", "Installation Option")),
                     ("Refrigeration configuration", ("Refrigeration Configuration",)), ("Counter depth", ("Counter Depth",)),
                     ("Number of doors", ("Number of Doors",), _NUM), ("Defrost type", ("Defrost Type",)), ("Automatic defrost", ("Automatic Defrost",)),
                     ("Cooling type", ("Cooling Type",)), ("Sabbath mode", ("Sabbath Mode",)),
                     ("Dispenser", ("Dispenser Type",)), ("Types of ice", ("Types of Ice",)),
                     ("Ice storage", ("Ice Capacity Storage",)), ("Water filter location", ("Water Filter Location",)),
                     ("Door finish", ("Door Finish",)), ("Garage ready", ("Garage Ready",))],
    "cooking": [("Fuel type", ("Fuel Type",)),
                ("Product type", ("Range Type", "Microwave Type", "Oven Type")),
                ("Configuration", ("Installation Configuration", "Range Configuration", "Oven Configuration",
                                   "Microwave Configuration", "Cooktop Configuration")),
                ("Cooktop element style", ("Cooktop Element Style",)),
                ("Burners/elements", ("Number of Cooking Element-Burners",), _NUM),
                ("Upper oven capacity (cu ft)", ("Upper Oven Capacity",), _NUM),
                ("Lower oven capacity (cu ft)", ("Lower Oven Capacity",), _NUM),
                ("Cooking system", ("Oven Cooking System", "Convection Element Type")),
                ("Convection", ("Convection",)), ("Air fry", ("Air Fry",)),
                ("Oven cleaning", ("Oven Cleaning Type",)), ("Oven racks", ("Number of Oven Racks", "Number of Upper Oven Racks"), _NUM),
                ("Microwave cooking power (W)", ("Cooking Power",), _NUM), ("Power levels", ("Number of Power Levels",), _NUM),
                ("Sensor cooking", ("Sensor Cooking",)), ("Turntable diameter (in)", ("Turntable Diameter Size",), _NUM),
                ("Vent CFM", ("CFM Motor Class",), _NUM), ("Ventilation type", ("Ventilation Type", "Venting")),
                ("Control type", ("Oven Control Type", "Control Type", "Cooktop Control Type")),
                ("Smart appliance", ("Smart Appliance",)), ("Input watts (W)", ("Watts",), _NUM)],
}


def _extra_specs(major: str, f: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, keys, *kind in _EXTRA.get(major, []):
        v = next((f[k] for k in keys if f.get(k, "").strip()), None)
        if v is None:
            continue
        if kind and (n := _first_num(v)) is not None:
            v = str(int(n)) if float(n).is_integer() else str(n)
        out[label] = v.strip()
    if major == "cooking":
        cut = [f.get(k) for k in ("Cutout Width", "Cutout Height", "Cutout Depth")]
        if all(cut):
            out["Cutout dimensions (W x H x D)"] = " x ".join(str(c) for c in cut)
        detail = [(k, v) for k, v in f.items() if re.search(r"Element-Burner Power$|Element Burner Power$", k)]
        if detail:
            out["Burner/element detail"] = "; ".join(
                f"{re.sub(r' Element[- ]Burner Power$', '', k)}: {v}" for k, v in detail)
    return out


def parse_product(model: str, url: str, spec: dict, prod: dict) -> tuple[ProductRecord, list[RawSpec]]:
    if not isinstance(spec, dict) or not spec.get("specSections") or not prod.get("name"):
        raise ValueError(f"KitchenAid PDP data structure unrecognised for {model}")
    fam = _family(url, prod["name"]) or _family(prod.get("url") or "", prod["name"])
    major = _MAJOR_BY_SEGMENT.get(fam or "")
    if not major:
        raise ValueError(f"not a supported KitchenAid product category for {model} ({prod['name'][:60]!r})")
    sub = infer_subcategory(url, prod["name"])
    fridge = major == "refrigerator"
    f = _flatten(spec)
    es = f.get("Energy Star® Qualified")
    ice, disp = f.get("Icemaker"), f.get("Dispenser Type")
    smart, conn = f.get("Smart Appliance"), f.get("Connectivity")
    conn = conn or smart
    wifi = _yes_no(conn)
    if wifi is None and conn and re.search(r"android|ios|alexa|wi-?fi|app\b", conn, re.I):
        wifi = True
    price = (prod.get("price") or {}).get("value")
    feats = list(dict.fromkeys(x["title"].strip() for x in prod.get("productFeature") or [] if x.get("title")))
    name_cap = re.search(r"(\d+(?:\.\d+)?)\s*cu\.?\s*ft", prod["name"], re.I)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=prod["name"], product_url=url,
        category=major, subcategory=sub,
        door_style=(f.get("Door Style Configuration") or f.get("Refrigerator Type")) if fridge else None,
        finish_color=f.get("Door Color") or (prod.get("color") or {}).get("name") or f.get("Cabinet Color"),
        price_usd=float(price) if price else None,
        capacity_total_cuft=_first_num(f.get("Total Refrigerator Capacity Volume") or f.get("Capacity"))
        or (float(name_cap.group(1)) if name_cap else None),
        capacity_fridge_cuft=_first_num(f.get("Refrigerator Capacity Volume")) if fridge else None,
        capacity_freezer_cuft=_first_num(f.get("Freezer Capacity Volume")) if fridge else None,
        width_in=_first_num(f.get("Width")), height_in=_first_num(f.get("Height")),
        depth_in=_first_num(f.get("Depth")), weight_lb=_first_num(f.get("Net Weight")),
        voltage_v=f.get("Volts"), amps=_first_num(f.get("Amps")), frequency_hz=_first_num(f.get("Hz")),
        energy_star=_energy_star(es),
        ice_maker=(None if not ice else _yes_no(ice) is not False) if fridge else None,
        water_dispenser=(None if not disp else ("water" in disp.lower())) if fridge else None,
        wifi_supported=wifi,
        wifi_evidence=f"Smart Compatibility: Connectivity/Smart Appliance = {conn}" if wifi is not None else None,
        pod_features=feats,
        extra_specs={**full_spec_table(spec), **_extra_specs(major, f)},
        image_url=main_image_url(prod),
    )
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=sec["name"], key=x["name"],
                   value=str(x["value"])) for sec in spec["specSections"] for x in sec["specs"]
           if x.get("value") not in (None, "")]
    return record, raw


def parse_docs(data: dict) -> list[tuple[str, str]]:
    """(our doc_type, absolute url) for English-US PDFs, priority-ordered, capped at MAX_PDFS."""
    picked: dict[str, tuple[int, str]] = {}
    for d in data.get("documents", []):
        kinds = [k for k in d.get("doc_type", []) if k in DOC_MAP]
        if not kinds or "en_us" not in d.get("language", []) or not d.get("asseturl", "").lower().endswith(".pdf"):
            continue
        dtype, prio = DOC_MAP[kinds[0]]
        picked.setdefault(dtype, (prio, urljoin(BASE, d["asseturl"])))
    return [(t, u) for t, (_, u) in sorted(picked.items(), key=lambda kv: kv[1][0])][:MAX_PDFS]


def parse_energy_kwh(text: str) -> float | None:
    v = common.num(r"(\d[\d,]*)\s*kWh\s*\n\s*Estimated Yearly Electricity Use", text, re.I)
    if v is not None:
        return v
    # Alt layout: "<lo> kWh\n<hi> kWh\n<this model>\n" (range ends, then the model's own figure)
    m = re.search(r"(\d[\d,]*)\s*kWh\s*\n\s*(\d[\d,]*)\s*kWh\s*\n\s*(\d[\d,]*)\s*\n", text)
    if not m:
        return None
    a, b, n = (float(g.replace(",", "")) for g in m.groups())
    return n if min(a, b) <= n <= max(a, b) else None


def _dest_path(model: str, doc_type: str):
    """downloads/kitchenaid/<model>_<doc_type>.pdf with sanitised parts; must resolve inside downloads/."""
    safe = lambda v: re.sub(r"[^A-Za-z0-9._-]", "_", v).strip(".")
    dest = common.DOWNLOADS / BRAND.lower() / f"{safe(model)}_{safe(doc_type)}.pdf"
    if not dest.resolve().is_relative_to(common.DOWNLOADS.resolve()):
        raise ValueError(f"download path escapes downloads/: {dest}")
    return dest


def _download(s: _Session, model: str, doc_type: str, url: str) -> DocumentRecord | None:
    """Fetch a PDF through the browser page (plain HTTP is 403'd) and store it like common.download_pdf."""
    import fitz
    dest = _dest_path(model, doc_type)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _pdf_url_ok(url):
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
    return DocumentRecord(brand=BRAND, model_number=model, doc_type=doc_type, source_url=url,
                          local_path=str(dest.relative_to(common.ROOT)),
                          sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), pages=pages)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = model_from_url(url)
    with _session(url) as s:
        spec = s.json(SPEC_URL.format(m=model))
        prod = s.json(f"{OCC}/products/{model}?fields=FULL&lang=en_US")
        docs_json = s.json(DOCS_URL.format(m=model))
        product, raw = parse_product(model, url, spec, prod)
        docs, kwh = [], None
        for dtype, durl in parse_docs(docs_json):
            d = _download(s, model, dtype, durl)
            if d:
                docs.append(d)
                if dtype == "EnergyGuide" and product.category == "refrigerator":
                    kwh = parse_energy_kwh(common.pdf_text(common.ROOT / d.local_path))
                    if kwh is None:
                        print(f"warning: no kWh/year found in Energy Guide for {model}", file=sys.stderr)
    return (product.model_copy(update={"energy_kwh_year": kwh}) if kwh is not None else product), docs, raw
