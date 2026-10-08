"""Whirlpool US adapter (catalog.py contract): refrigerators, washers/dryers, cooking.

Same Whirlpool Corp platform as kitchenaid_us.py: Hybris OCC JSON (`/ws/v2/whirlpool-us/...`), AEM spec JSON and
AEM document search, all called with fetch() from inside a real browser page (Akamai 403s plain `requests`,
also for PDFs). Old headless (chromium-headless-shell) dies with ERR_HTTP2_PROTOCOL_ERROR; new headless
(channel="chromium") works, with a visible window as the last fallback.
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
import catalog
from catalog import Candidate
from maytag_us import occ_signals  # same OCC platform; shared rating/review extraction
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Whirlpool"
BASE = "https://www.whirlpool.com"
OCC = "/ws/v2/whirlpool-us"
SPEC_URL = ("/content/whirlpoolv2/en_us/products/majors/jcr:content/root/main/productTray/pdpTray/"
            "checkDimensions/par-section/specificationspdpv5.model.{m}.json")
DOCS_URL = ("/services/search/contents.json?notincludeEmptyLang=false&query=%3Aformat%3Aapplication%2Fpdf"
            "%2Capplication%2Fzip%3Aproduct_sku%3A{m}&pdpdoc=true&pageSize=20&sort=created-desc"
            "&currentPage=1&fields=FULL&lang=en_us")
PAGE_SIZE = 24
DELAY_S = 1.0
MAX_PDFS = 5
# site doc_type -> (our doc_type, priority); cad / warranty / cycle guides etc. are skipped
DOC_MAP = {"energy-guide": ("EnergyGuide", 0), "owners-manual": ("Manual", 1), "product-guide": ("Manual", 1.5),
           "dimension-guide": ("SpecSheet", 2), "feature-sheet": ("SpecSheet", 2.5),
           "installation-instructions": ("Installation", 3), "safety-and-installation-instructions": ("Installation", 3),
           "reference-sheet": ("QuickSpecs", 4)}
CONSENT_DECLINE = ["Reject All", "Decline", "Reject"]
CONSENT_ACCEPT = ["Accept All", "Accept", "Allow All", "I Agree"]

PAGE_HOSTS = ("whirlpool.com",)
PDF_HOSTS = ("whirlpool.com", "whirlpoolcorp.com")

_headless_ok: bool | None = None  # mode that worked last; only a PREFERRED ORDER for auto mode


@dataclass(frozen=True)
class _Sub:
    major: str
    page: str                      # real page used to open the browser session
    codes: tuple[str, ...]         # OCC category codes queried by discover()
    path: str                      # regex on pipedCategory strings, used by classify()
    name_in: str | None = None
    name_ex: str | None = None
    alt: tuple[str, str] | None = None   # (path regex, name regex): second way in, both must match


_PAGE = {"refrigerator": "/kitchen/refrigeration/refrigerators.html", "washer": "/laundry/washers.html",
         "cooking": "/kitchen/cooking/ranges.html"}
_FRIDGE_EX = r"refurbish|beverage|wine|\bicemaker\b"
_SCO_NAME = r"combination|combo|speed[- ]?(cook|oven)|wall oven.*microwave|microwave.*wall oven"  # wall-oven names with a microwave/speed function
_WASH_EX =r"refurbish|laundry (center|tower)|stacked|all[- ]in[- ]one|washer[- ]dryer"

# Order matters for classify(): first match wins (side_by_side before french_door, laundry_center before
# washers, otr before microwave). Counter-depth is NOT a sub key: a counter-depth fridge is classified by the
# door style its name states (the `alt` rules), never just because it is counter-depth.
SUBS: dict[str, _Sub] = {
    "side_by_side": _Sub("refrigerator", _PAGE["refrigerator"], ("KitchenRefrigerationRefrigeratorsSide-by-Side",),
                         r"Refrigerators\|Side-by-Side", name_ex=_FRIDGE_EX,
                         alt=(r"Refrigerators\|Counter-Depth", r"side[- ]by[- ]side")),
    "french_door": _Sub("refrigerator", _PAGE["refrigerator"], ("KitchenRefrigerationRefrigeratorsFrenchDoor",),
                        r"Refrigerators\|French-Door", name_ex=_FRIDGE_EX,
                        alt=(r"Refrigerators\|Counter-Depth", r"french[- ]door")),
    "top_freezer": _Sub("refrigerator", _PAGE["refrigerator"], ("KitchenRefrigerationRefrigeratorsTopFreezer",),
                        r"Refrigerators\|Top-Freezer", name_ex=_FRIDGE_EX),
    "bottom_freezer": _Sub("refrigerator", _PAGE["refrigerator"], ("KitchenRefrigerationRefrigeratorsBottomFreezer",),
                           r"Refrigerators\|Bottom-Freezer", name_ex=_FRIDGE_EX),
    "compact": _Sub("refrigerator", _PAGE["refrigerator"], ("KitchenCompactSmallSpaces",),
                    r"Refrigerators\|Undercounter", name_in=r"refrigerator", name_ex=_FRIDGE_EX),
    "laundry_center": _Sub("washer", _PAGE["washer"], ("LaundryStackedLaundryCenters",),
                           r"Stacked-Laundry-Centers|Laundry-Sets", name_in=r"laundry (center|tower)|stacked",
                           name_ex=r"refurbish"),
    "top_load": _Sub("washer", _PAGE["washer"], ("LaundryWashersTopLoad",), r"Laundry\|Washers\|(HE-)?Top-Load",
                     name_ex=_WASH_EX),
    "front_load": _Sub("washer", _PAGE["washer"], ("LaundryWashersFrontLoad",), r"Laundry\|Washers\|Front-Load",
                       name_ex=_WASH_EX),
    "dryer": _Sub("washer", _PAGE["washer"], ("LaundryDryers",), r"Laundry\|Dryers",
                  name_ex=r"refurbish|laundry (center|tower)|stacked"),
    "otr": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingMicrowavesOvertheRange",),
                r"Microwaves\|Over-the-Range", name_ex=r"refurbish"),
    # sco = Speed Cook Oven: a wall oven with a built-in microwave (combination wall oven) or a speed oven.
    "sco": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingWallOvens", "KitchenCookingMicrowavesBuilt-In"),
                r"Wall-Ovens\|Microwave-Oven-Combo", name_ex=r"refurbish",
                alt=(r"Cooking\|(Wall-Ovens|Microwaves\|Built-In)", _SCO_NAME)),
    "microwave": _Sub("cooking", _PAGE["cooking"],
                      ("KitchenCookingMicrowavesCountertop", "KitchenCookingMicrowavesBuilt-In"),
                      r"Cooking\|Microwaves", name_ex=r"refurbish"),
    "induction": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingCooktops", "KitchenCookingRanges"),
                      r"Cooking\|(Cooktops|Ranges)", name_in=r"induction", name_ex=r"refurbish|\bhood\b"),
    # gas_oven = gas / dual-fuel RANGES (Whirlpool sells no gas wall oven; gas cooktops are gas_cooktop).
    "gas_oven": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingRanges",), r"Cooking\|Ranges",
                     name_in=r"\bgas\b|dual[- ]fuel", name_ex=r"refurbish|\bhood\b"),
    # gas_cooktop = oven-less gas cooktops (the Cooktops category; a gas range stays gas_oven).
    "gas_cooktop": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingCooktops",), r"Cooking\|Cooktops",
                        name_in=r"\bgas\b", name_ex=r"refurbish|\bhood\b|induction"),
    # radiant = electric ranges and electric (ceramic glass / radiant / coil) cooktops, induction excluded.
    "radiant": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingRanges", "KitchenCookingCooktops"),
                    r"Cooking\|(Ranges|Cooktops)", name_in=r"electric|ceramic|radiant|smooth ?top|coil",
                    name_ex=r"refurbish|\bhood\b|induction|\bgas\b|dual[- ]fuel"),
    "electric_oven": _Sub("cooking", _PAGE["cooking"], ("KitchenCookingWallOvens",), r"Cooking\|Wall-Ovens",
                          name_ex=r"refurbish|" + _SCO_NAME),
}
# Not sold by Whirlpool US (verified: built-in refrigerator category 400s): built_in.
SUPPORTED_SUBCATEGORIES = set(SUBS)


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


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _wp_host(url: str) -> bool:
    return _host_in(url, PAGE_HOSTS)


def _pdf_url_ok(url: str) -> bool:
    return urlparse(url).scheme == "https" and _host_in(url, PDF_HOSTS)


def _check_final_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _wp_host(url):
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
    """Browser page on whirlpool.com; fetches same-origin JSON / bytes with the page's cookies."""

    def __init__(self, page):
        self.page = page

    def text(self, path: str) -> str:
        status, body = self.page.evaluate(
            "async u=>{const r=await fetch(u);return [r.status,await r.text()]}", path)
        if status != 200:
            raise RuntimeError(f"Whirlpool fetch {path[:90]} -> HTTP {status}")
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


def _close_quietly(browser) -> None:
    """Close a browser in an error path; a failing close must not abort the mode-fallback loop."""
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"whirlpool_us: browser.close() failed: {e}", file=sys.stderr)


def _connect(p, url: str):
    """Return (browser, page) on url; headless first, visible fallback if blocked or navigation fails."""
    global _headless_ok
    last_err: Exception | None = None
    _check_final_url(url)
    modes = _modes()
    for headless in modes:
        browser = None
        try:
            browser = _launch(p, headless)
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _check_final_url(page.url)
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
            _close_quietly(browser)
        except BaseException:  # incl. KeyboardInterrupt: never leak the browser
            _close_quietly(browser)
            raise
    raise RuntimeError(f"Whirlpool page unreachable (modes tried: {modes}): {last_err}")


@contextmanager
def _session(url: str):
    with sync_playwright() as p:
        browser, page = _connect(p, url)
        try:
            yield _Session(page)
        finally:
            browser.close()


# ---------------------------------------------------------------- classification / discover
def _name_ok(sub: _Sub, name: str) -> bool:
    if sub.name_in and not re.search(sub.name_in, name, re.I):
        return False
    return not (sub.name_ex and re.search(sub.name_ex, name, re.I))


def _matches(sub: _Sub, piped: list[str], name: str) -> bool:
    if any(re.search(sub.path, c) for c in piped) and _name_ok(sub, name):
        return True
    return bool(sub.alt and any(re.search(sub.alt[0], c) for c in piped) and re.search(sub.alt[1], name, re.I)
                and not (sub.name_ex and re.search(sub.name_ex, name, re.I)))


def _piped(prod: dict) -> list[str]:
    cats = [c.get("pipedCategory") or "" for c in prod.get("categories") or []]
    return cats + [(prod.get("primaryCategory") or {}).get("pipedCategory") or ""]


def classify(prod: dict) -> tuple[str, str | None]:
    """(major key, sub key or None) from a product's category paths and name."""
    piped, name = _piped(prod), prod.get("name") or ""
    for key, sub in SUBS.items():
        if _matches(sub, piped, name):
            return sub.major, key
    joined = " ".join(piped)
    if "Kitchen|Refrigeration" in joined:
        return "refrigerator", None
    if re.search(r"Laundry\|(Washers|Dryers|Laundry-Sets|Stacked)", joined):
        return "washer", None
    if "Kitchen|Cooking" in joined:
        return "cooking", None
    return "other", None


def parse_search(data: dict, sub_key: str) -> list[Candidate]:
    """Candidates of a listing page that classify() (the rule scrape() uses) files under sub_key."""
    sub = SUBS[sub_key]
    out, other = [], 0
    for p in data.get("products", []):
        if not p.get("code") or not p.get("url"):
            continue
        if classify(p)[1] != sub_key:
            other += 1
            continue
        url = urljoin(BASE, p["url"])
        if urlparse(url).scheme != "https" or not _wp_host(url):
            print(f"dropped off-domain candidate {p['code']}: {p['url'][:100]}", file=sys.stderr)
            continue
        price = (p.get("price") or p.get("baseDisplayPrice") or {}).get("value")
        out.append(Candidate(brand=BRAND, model_number=p["code"], name=p["name"], url=url,
                             price_usd=float(price) if price else None, category=sub.major, subcategory=sub_key,
                             attrs=(sig := occ_signals(p)), attrs_src={k: "listing" for k in sig}))
    if other:
        print(f"whirlpool_us: parse_search({sub_key}): {other} listed product(s) classified under another sub key "
              f"or none, skipped", file=sys.stderr)
    return out


def _search_path(code: str, page_no: int) -> str:
    q = quote(f":newestProduct:category:{code}:showMajorProductsOnly:true", safe="")  # the site's own newest-first sort
    return f"{OCC}/products/search/singlesource?query={q}&pageSize={PAGE_SIZE}&fields=OPT&lang=en_US&currentPage={page_no}&isBundle=false"


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUBS:
        raise ValueError(f"unsupported subcategory {subcategory!r}")
    sub = SUBS[subcategory]
    found: dict[str, Candidate] = {}
    with _session(BASE + sub.page) as s:
        for code in sub.codes:
            page_no, total_pages = 0, 1
            while len(found) < limit and page_no < total_pages:
                data = s.json(_search_path(code, page_no))
                if "products" not in data or "pagination" not in data:
                    raise ValueError("Whirlpool listing JSON structure unrecognised")
                total_pages = data["pagination"]["totalPages"]
                for c in parse_search(data, subcategory):
                    found.setdefault(c.model_number, c)
                page_no += 1
                if len(found) < limit and page_no < total_pages:
                    time.sleep(DELAY_S)
    out = list(found.values())[:limit]
    # a sub spread over several site categories has no single order, so no rank there
    return catalog.stamp_newest_order(out) if len(sub.codes) == 1 else out


# ---------------------------------------------------------------- scrape
def model_from_url(url: str) -> str:
    u = urlparse(url)
    m = re.search(r"/p\.[^/]*\.([a-z0-9]{6,})\.html$", u.path, re.I)
    if u.scheme != "https" or not _wp_host(url) or not m:
        raise ValueError(f"not a Whirlpool US product URL: {url}")
    return m.group(1).upper()


def _first_num(v: str | None) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", v or "")
    return float(m.group(0).replace(",", "")) if m else None


_TAGS = re.compile(r"<[^>]+>")


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", str(v))).strip()


def _rows(spec: dict) -> list[tuple[str, str, str]]:
    return [(sec["name"], x["name"], _clean(x["value"])) for sec in spec["specSections"] for x in sec["specs"]
            if x.get("value") not in (None, "") and _clean(x["value"])]


def _noise(t: str) -> str:
    return t.replace("®", "").replace("™", "").strip()


def full_spec_table(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """EVERY Specs & Details row: 'Section > Label' -> full value."""
    out: dict[str, str] = {}
    for sec, k, v in rows:
        out.setdefault(f"{_noise(sec)} > {_noise(k)}", _noise(v))
    return out


IMAGE_HOSTS = ("whirlpool.com", "scene7.com")
IMAGE_QUERY = "?fmt=jpeg&wid=1200"  # bare Scene7 URL answers a tiny AVIF thumbnail (same platform as KitchenAid)


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


_FRIDGE_KEYS = {"Door Style Configuration", "Refrigerator Type", "Total Refrigerator Capacity Volume", "Capacity",
                "Refrigerator Capacity Volume", "Freezer Capacity Volume", "Icemaker", "Dispenser Type"}
_COMMON_KEYS = {"Width", "Height", "Depth", "Net Weight", "Volts", "Amps", "Hz", "Door Color", "Prop 65"}


def parse_product(model: str, url: str, spec: dict, prod: dict) -> tuple[ProductRecord, list[RawSpec]]:
    if not isinstance(spec, dict) or not spec.get("specSections") or not prod.get("name"):
        raise ValueError(f"Whirlpool PDP data structure unrecognised for {model}")
    major, sub = classify(prod)
    rows = _rows(spec)
    f: dict[str, str] = {}
    for _, k, v in rows:
        f.setdefault(k, v)
    price = (prod.get("price") or {}).get("value")
    feats = list(dict.fromkeys(x["title"].strip() for x in prod.get("productFeature") or [] if x.get("title")))
    wifi, wifi_ev = _wifi(f)
    kw = dict(
        brand=BRAND, model_number=model, product_name=prod["name"], product_url=url, category=major,
        subcategory=sub, finish_color=f.get("Door Color") or (prod.get("color") or {}).get("name") or None,
        price_usd=float(price) if price else None,
        width_in=_first_num(f.get("Width")), height_in=_first_num(f.get("Height")),
        depth_in=_first_num(f.get("Depth")), weight_lb=_first_num(f.get("Net Weight")),
        voltage_v=f.get("Volts"), amps=_first_num(f.get("Amps")), frequency_hz=_first_num(f.get("Hz")),
        energy_star=_energy_star(f), wifi_supported=wifi, wifi_evidence=wifi_ev, pod_features=feats)
    consumed = set(_COMMON_KEYS)
    if major == "refrigerator":
        ice, disp = f.get("Icemaker"), f.get("Dispenser Type")
        kw.update(
            door_style=f.get("Door Style Configuration") or f.get("Refrigerator Type"),
            capacity_total_cuft=_first_num(f.get("Total Refrigerator Capacity Volume") or f.get("Capacity")),
            capacity_fridge_cuft=_first_num(f.get("Refrigerator Capacity Volume")),
            capacity_freezer_cuft=_first_num(f.get("Freezer Capacity Volume")),
            ice_maker=None if not ice else _yes_no(ice) is not False,
            water_dispenser=None if not disp else ("water" in disp.lower()))
        consumed |= _FRIDGE_KEYS
    extra: dict[str, str] = {}
    for sec, k, v in rows:
        if k not in consumed:
            extra[k if k not in extra else f"{sec}: {k}"] = v
    kw["extra_specs"] = {**full_spec_table(rows), **extra}  # full sectioned table + legacy flat labels
    kw["image_url"] = main_image_url(prod)
    kw.update(occ_signals(prod))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=sec, key=k, value=v) for sec, k, v in rows]
    return ProductRecord(**kw), raw


def parse_docs(data: dict) -> list[tuple[str, str]]:
    """(our doc_type, absolute url) for English-US PDFs on allowed hosts, priority-ordered, capped at MAX_PDFS."""
    picked: dict[str, tuple[float, str]] = {}
    for d in data.get("documents", []):
        kinds = [k for k in d.get("doc_type", []) if k in DOC_MAP]
        asset = d.get("asseturl", "")
        if not kinds or "en_us" not in d.get("language", []) or not asset.lower().endswith(".pdf"):
            continue
        url = urljoin(BASE, asset)
        if not _pdf_url_ok(url):
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


def _dest_path(model: str, doc_type: str):
    """downloads/whirlpool/<model>_<doc_type>.pdf with sanitised parts; must resolve inside downloads/."""
    safe = lambda v: re.sub(r"[^A-Za-z0-9._-]", "_", v).strip(".") or "x"
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
    return product.model_copy(update={"energy_kwh_year": kwh}), docs, raw
