"""Smeg US adapter (catalog.py contract): cooking only (ovens/microwaves, ranges, cooktops; refrigerators not implemented per instruction).

Data source: https://www.smeg.com/us (smegusa.com redirects there). robots.txt allows everything; no price is published
(price_usd stays None -> price band "unknown"). Server-rendered HTML, plain requests work:
  * category pages (/us/ranges/all, /us/cooktops/all, /us/ovens/all, /us/ovens/microwave-ovens, /us/refrigerators/all):
    `<div id="<MODEL>" class="listItem ... product-preview">` cards whose `product-preview__description` is a
    ' | '-separated fact line ("Refrigerator | Professional | French-door | Freestanding | ...").
  * product page /us/products/<MODEL>: schema.org Product JSON-LD (image list; the JSON is NOT valid, read by regex),
    `<div class="product-detail">` sections (`h2.product-detail__title` + `detail__label`/`detail__txt` rows),
    manual links on doc.smeg.it.
Fetching: plain requests first, then headless Playwright, then a visible window (FRIDGE_BROWSER_MODE = auto | headless |
visible, read on every call). Requests are spaced >= 1 s.
"""
import html as _html
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Smeg"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://www.smeg.com/us"
PAGE_HOSTS = ("smeg.com",)
IMAGE_HOSTS = ("4flow.cloud", "smeg.com")
DOC_HOSTS = ("smeg.it", "smeg.com")
DELAY_S = 1.0
MAX_REDIRECTS = 5

# sub key -> (major key, listing pages). The listings overlap (a range is also a cooktop family), so classify() - one rule
# shared by discover() and scrape() - decides the sub key of every card from its fact line.
SUB_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "microwave": ("cooking", ("ovens/microwave-ovens", "ovens/all")),
    "sco": ("cooking", ("ovens/all", "ovens/microwave-ovens")),
    "electric_oven": ("cooking", ("ovens/all",)),
    "gas_oven": ("cooking", ("ranges/all",)),
    "gas_cooktop": ("cooking", ("cooktops/all",)),
    "induction": ("cooking", ("cooktops/all", "ranges/all")),
    "radiant": ("cooking", ("cooktops/all", "ranges/all")),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
DOC_TYPES = (("Installation Manual", "Installation"), ("User manual", "Manual"), ("Other Instructions", "Other"),
             ("Warranty certificate", "Warranty"))


# ---------------------------------------------------------------- fetching
class SmegPageError(RuntimeError):
    pass


class SmegNotFound(SmegPageError):
    pass


def _strategies() -> list[str]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return ["headless"]
    if mode == "visible":
        return ["visible"]
    return ["requests", "headless", "visible"]


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_final_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url, PAGE_HOSTS):
        raise ValueError(f"unexpected host after navigation: {url[:120]}")


_last_request = [0.0]


def _throttle() -> None:
    wait = DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def _via_requests(url: str, probe: str) -> str:
    cur = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_final_url(cur)
        _throttle()
        r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise SmegPageError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise SmegNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise SmegPageError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise SmegPageError(f"expected marker {probe!r} missing (structure changed?)")
    return body


def _consent(page, accept: bool = False) -> None:
    """Cookie banner: decline non-essential first; accept only when the page is stuck behind it."""
    names = ("Accept all", "Accept All", "Accept") if accept else ("Reject all", "Reject All", "Decline", "Only necessary")
    for label in names:
        try:
            page.get_by_role("button", name=label, exact=True).first.click(timeout=1500)
            return
        except (PlaywrightTimeoutError, PlaywrightError):
            continue


def _via_browser(url: str, probe: str, headless: bool) -> str:
    check = "(p) => document.documentElement.outerHTML.includes(p)"
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA).new_page()
            _throttle()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_final_url(page.url)
            _consent(page)
            try:
                page.wait_for_function(check, arg=probe, timeout=15000)
            except PlaywrightTimeoutError:
                _consent(page, accept=True)  # the banner may be blocking rendering; retry once
                page.wait_for_function(check, arg=probe, timeout=15000)
            status = resp.status if resp else None
            if status in (404, 410):
                raise SmegNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise SmegPageError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except SmegNotFound:
            raise
        except (SmegPageError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"smeg_us: load via {strategy} failed: {e}", file=sys.stderr)
    raise SmegPageError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def _tokens(desc: str) -> list[str]:
    return [t.strip().lower() for t in (desc or "").split("|") if t.strip()]


def classify(desc: str) -> str | None:
    """Sub key from a listing fact line ('Range | 36" | ... | Dual-Fuel | Cooktop type: Gas'); one rule for discover() and
    scrape(). Range: full gas / dual fuel -> gas_oven, induction -> induction, other electric -> radiant. Cooktop: by
    fuel (gas / induction / ceramic=radiant). Oven: combination microwave or speed oven -> sco, microwave with grill ->
    microwave, anything else (convection, steam) -> electric_oven. Hoods, accessories and the rest -> None."""
    t = _tokens(desc)
    if not t:
        return None
    joined = " | ".join(t)
    if t[0] == "cooktop":
        kind = t[1] if len(t) > 1 else ""
        return {"gas": "gas_cooktop", "induction": "induction", "ceramic": "radiant"}.get(kind)
    if "range" in t[:2]:
        if "full gas" in t or "dual-fuel" in t:
            return "gas_oven"
        if "induction" in joined:
            return "induction"
        return "radiant" if "full electric" in t else None
    if t[0] == "oven":
        method = next((x for x in t if x.startswith("cooking method:")), "")
        if "combi microwave" in method or "speed" in method:
            return "sco"
        if "microwave" in method:
            return "microwave"
        return "electric_oven"
    return None


def _major_of(sub: str) -> str:
    return SUB_SOURCES[sub][0]


def _card_attrs(desc: str) -> dict:
    t = _tokens(desc)
    attrs: dict = {}
    m = re.search(r"""\b(\d{2})\s*(?:"|''|inch)""", desc)
    if m:
        attrs["width_in"] = float(m.group(1))
    if "dual-fuel" in t:
        attrs["fuel"] = "dual_fuel"
    elif "full gas" in t or "cooktop type: gas" in t or t[:2] == ["cooktop", "gas"]:
        attrs["fuel"] = "gas"
    elif "cooktop type: induction" in t or t[:2] == ["cooktop", "induction"]:
        attrs["fuel"] = "induction"
    elif "full electric" in t or t[:2] == ["cooktop", "ceramic"]:
        attrs["fuel"] = "electric"
    return attrs


# ---------------------------------------------------------------- discover
_CARD = re.compile(r'<div[^>]*\bid="([^"]+)"[^>]*class="listItem.*?<div class="product-preview__description">(.*?)</div>',
                   re.S)


def parse_listing(html: str) -> list[tuple[str, str]]:
    """[(model, fact line)] of one category page, in page order."""
    out = []
    for m in _CARD.finditer(html):
        model, desc = m.group(1), _html.unescape(re.sub(r"\s+", " ", m.group(2))).strip()
        if _MODEL.match(model):
            out.append((model, desc))
    return out


def _candidate(model: str, desc: str, sub: str) -> Candidate:
    return Candidate(brand=BRAND, model_number=model, name=f"Smeg {model} - {desc}", url=f"{BASE}/products/{model}",
                     price_usd=None, category=_major_of(sub), subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, attrs=_card_attrs(desc), attrs_src={k: "listing" for k in _card_attrs(desc)})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Smeg US")
    found: dict[str, Candidate] = {}
    for page in SUB_SOURCES[subcategory][1]:
        try:
            cards = parse_listing(fetch_html(f"{BASE}/{page}", "product-preview"))
        except SmegNotFound:
            continue
        for model, desc in cards:
            if classify(desc) == subcategory:
                found.setdefault(model, _candidate(model, desc, subcategory))
        print(f"smeg_us: discover({subcategory}) {page}: {len(cards)} listed, {len(found)} kept", file=sys.stderr)
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def model_from_url(url: str) -> str:
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not _host_in(url, PAGE_HOSTS) or len(parts) != 3 or parts[:2] != ["us", "products"]
            or not _MODEL.match(parts[2])):
        raise ValueError(f"not a supported Smeg US product URL: {url}")
    return parts[2]


def _clean(s: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _frac_in(text: str) -> float | None:
    """'35 7/8 "' -> 35.875, '36 "' -> 36.0, '27 11/16 "' -> 27.6875."""
    m = re.match(r"\s*(\d+)(?:\s+(\d+)/(\d+))?\s*(?:\"|in\b|inch)", text or "")
    if not m:
        return None
    return int(m.group(1)) + (int(m.group(2)) / int(m.group(3)) if m.group(2) else 0)


def spec_sections(html: str) -> list[tuple[str, str, str]]:
    """[(section, label, value)] of every `product-detail` block; a repeated label inside a section is kept."""
    rows = []
    for chunk in re.split(r'<div class="product-detail"', html)[1:]:
        t = re.search(r'product-detail__title">(.*?)</h2>', chunk, re.S)
        section = _clean(t.group(1)) if t else ""
        for lab, val in re.findall(r'detail__label">(.*?)</span>\s*<span class="detail__txt">(.*?)</span>', chunk, re.S):
            label, value = _clean(lab).rstrip(":").strip(), _clean(val)
            if label and value:
                rows.append((section, label, value))
    return rows


def main_image_url(html: str) -> str | None:
    """First image of the Product JSON-LD (invalid JSON on this site, so read by regex); https on an image host."""
    i = html.find('"@type": "Product"')
    if i < 0:
        i = html.find('"@type":"Product"')
    m = re.search(r'"image"\s*:\s*\[\s*"([^"]+)"', html[i:i + 6000]) if i >= 0 else None
    if m:
        url = m.group(1)
        if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS):
            return url
    return None


def doc_links(html: str) -> list[tuple[str, str]]:
    """[(doc_type, url)] from the manuals block: https links on doc.smeg.it, one per document type."""
    out, used = [], set()
    for href, title in re.findall(r'<a href="([^"]+)" title="([^"]+)"[^>]*>[^<]*\(pdf\)', html):
        dtype = next((t for key, t in DOC_TYPES if key.lower() == title.strip().lower()), None)
        url = _html.unescape(href)
        if dtype and dtype not in used and urlparse(url).scheme == "https" and _host_in(url, DOC_HOSTS):
            used.add(dtype)
            out.append((dtype, url))
    return out


def _first(rows: list[tuple[str, str, str]], section: str, *labels: str) -> str | None:
    for sec, lab, val in rows:
        if sec.lower() == section.lower() and lab.lower() in labels:
            return val
    return None


def _num(pattern: str, text: str | None) -> float | None:
    return common.num(pattern, text or "", re.I)


def parse_product(model: str, url: str, html: str) -> tuple[ProductRecord, list[RawSpec]]:
    rows = spec_sections(html)
    if not rows:
        raise SmegPageError(f"no spec sections found for {model} (structure changed?)")
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    name = re.sub(r"\s*\|\s*Smegusa\.com\s*$|\s+Smeg\s*$", "", _clean(t.group(1))) if t else model
    if name and not name.lower().startswith("smeg"):
        name = f"Smeg {name}"
    name = name or model
    sub = classify(fact_line(rows))
    major = "cooking"
    extra: dict[str, str] = {}
    for s_, l, v in rows:  # full 'Section > Label' table; a repeated label inside a section is joined
        key = f"{s_} > {l}" if s_ else l
        extra[key] = f"{extra[key]} | {v}" if key in extra and v not in extra[key] else extra.get(key, v)
    w = _frac_in(_first(rows, "Logistic Information", "width") or "")
    h = _frac_in(_first(rows, "Logistic Information", "height") or "")
    d = _frac_in(_first(rows, "Logistic Information", "depth", "depth without handle", "product depth (handle included)") or "")
    volt = _first(rows, "Electrical Connection", "voltage", "voltage (v)")
    freq = _num(r"([\d.]+)", _first(rows, "Electrical Connection", "frequency", "frequency (hz)"))
    cap_total = _num(r"([\d.]+)\s*cu", _first(rows, "Performance / Energy Label", "net volume"))
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name or model, category=major, subcategory=sub,
        product_url=url, price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY,
        width_in=w, height_in=h, depth_in=d,
        weight_lb=_num(r"([\d.]+)\s*lb", _first(rows, "Logistic Information", "net weight")),
        voltage_v=volt.replace(" V", "").strip() if volt else None, frequency_hz=freq,
        capacity_total_cuft=cap_total,
        capacity_fridge_cuft=_num(r"([\d.]+)\s*cu", _first(rows, "Performance / Energy Label", "fresh food compartment - storage volume")),
        capacity_freezer_cuft=_num(r"([\d.]+)\s*cu", _first(rows, "Performance / Energy Label", "freezer volume")),
        extra_specs=extra, image_url=main_image_url(html))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=l, value=v) for s, l, v in rows]
    return record, raw


def fact_line(rows: list[tuple[str, str, str]]) -> str:
    """A listing-style fact line rebuilt from the product page's 'General information' block, so classify() stays the one
    rule shared by discover() (card text) and scrape() (page)."""
    g = lambda *labels: _first(rows, "General information", *labels) or ""
    family = g("product family")
    if family.lower() == "range":
        ct = g("cooktop type")
        return " | ".join(x for x in ("Range", g("range type"), f"Cooktop type: {ct}" if ct else "") if x)
    if family.lower() == "cooktop":
        return " | ".join(x for x in ("Cooktop", g("type")) if x)
    if family.lower() == "oven":
        return f"Oven | Cooking method: {g('cooking method')}"
    return family


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = model_from_url(url)
    html = fetch_html(url, "product-detail")
    record, raw = parse_product(model, url, html)
    docs: list[DocumentRecord] = []
    for dtype, durl in doc_links(html):
        rec = common.download_pdf(BRAND, model, dtype, durl)
        if rec:
            docs.append(rec)
    return record, docs, raw
