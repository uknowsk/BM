"""Beko US adapter (catalog.py contract): cooking only (microwave, otr, gas_oven, gas_cooktop, electric_oven, induction, radiant).

Source: www.beko.com/us-en (Adobe Experience Manager). The site sits behind Akamai: plain requests and the legacy headless shell get
403, the new headless Chromium (channel="chromium") is served normally, so every page goes through a browser (FRIDGE_BROWSER_MODE
auto = headless first, then a visible window; a block page is reported, never bypassed). robots.txt allows /us-en.
  * listing: /us-en/<category> (microwaves, ranges, wall-ovens, gas-cooktops, electric-cooktops, induction-cooktops); product cards
    (class ProductCardPLP__root) with link, '<name> <MODEL>' image title and key/value pairs ('Cooktop Type', 'Burner Configuration').
    Category pages overlap and one model can be listed under two URL forms: candidates are de-duplicated by model number.
  * product: <h1>MODEL: name</h1>, Product JSON-LD (image), spec sections (h3.ContentToggle__title + PropertyTable rows),
    PDF links under /content/dam/.
No price is published (cards are 'noPrice', 'Where To Buy'), so price_usd is None. PDF downloads are not attempted: plain requests to
the PDF links get Akamai 403 (common.download_pdf has no browser fallback), so the document links are stored as extra_specs
'Documents > <type>' instead and the DocumentRecord list is empty. Lengths on the page are in cm, weights in kg.
"""
import html as _html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import common
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Beko"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://www.beko.com"
ROOT = "/us-en/"
PAGE_HOSTS = ("beko.com",)
DELAY_S = 1.0

SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("microwaves",),
    "otr": ("microwaves",),
    "gas_oven": ("ranges",),
    "electric_oven": ("wall-ovens",),
    "gas_cooktop": ("gas-cooktops",),
    "induction": ("induction-cooktops", "ranges"),
    "radiant": ("electric-cooktops", "ranges"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)  # refrigerators/laundry/dishwashers exist on the site but are not implemented
_SKU = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
_LABELS = ("Cooktop Type", "Burner Configuration")


# ---------------------------------------------------------------- classification
def classify(name: str, cooktop_type: str = "", burners: str = "") -> str | None:
    """Cooking sub key. name = product name; cooktop_type / burners = the 'Cooktop Type' and 'Burner Configuration' values
    (the same two values on the listing card and on the product page). Shared by discover() and scrape()."""
    n = (name or "").lower()
    fuel = f"{cooktop_type} {burners}".lower()
    has_gas = bool(re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", f"{n} {fuel}"))
    if "microwave" in n:
        return "otr" if re.search(r"over[- ]the[- ]range|\botr\b", n) else "microwave"
    if "wall oven" in n:
        return "electric_oven"
    if re.search(r"cooktop|rangetop|range\b", n):   # 'range\b': the site has typos such as 'Slide-InRange'
        if "induction" in f"{n} {fuel}":
            return "induction"
        if has_gas:
            return "gas_cooktop" if re.search(r"cooktop|rangetop", n) else "gas_oven"
        if re.search(r"electric|vitroceramic|ceramic|radiant|coil|zone", f"{n} {fuel}"):
            return "radiant"
    return None


# ---------------------------------------------------------------- browser
def _modes() -> list[bool]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    return [True] if mode == "headless" else [False] if mode == "visible" else [True, False]


def _host_in(url: str, suffixes: tuple[str, ...] = PAGE_HOSTS) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url) or not urlparse(url).path.startswith(ROOT):
        raise ValueError(f"unexpected url: {url[:120]}")


class _Browser:
    """One new-headless Chromium page for the whole call (>= DELAY_S between navigations); always closed."""

    def __init__(self):
        self._last = 0.0
        self._pw = self._browser = self._page = None

    def _open(self):
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        last: Exception | None = None
        for headless in _modes():
            self._pw = sync_playwright().start()
            try:
                self._browser = self._pw.chromium.launch(channel="chromium", headless=headless)
                self._page = self._browser.new_context(user_agent=common.UA).new_page()
                resp = self._page.goto(f"{BASE}{ROOT}", wait_until="domcontentloaded", timeout=60000)
                self._page.wait_for_timeout(1500)
                if common.looks_blocked(resp.status if resp else None, self._page.inner_text("body")):
                    raise RuntimeError(f"blocked (status {resp.status if resp else None})")
                return
            except (PlaywrightError, RuntimeError) as e:
                last = e
                self.close()
        raise RuntimeError(f"Beko US page unreachable (modes tried: {_modes()}): {last}")

    def get(self, url: str) -> str:
        _check_url(url)
        if self._page is None:
            self._open()
        gap = DELAY_S - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _check_url(self._page.url)
        self._page.wait_for_timeout(2500)
        if common.looks_blocked(resp.status if resp else None, self._page.inner_text("body")):
            raise RuntimeError(f"Beko US blocked the request (status {resp.status if resp else None}); not bypassing")
        return self._page.content()

    def close(self):
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception as e:  # noqa: BLE001
                print(f"beko_us: {fn} failed: {e}", file=sys.stderr)
        self._browser = self._page = self._pw = None


# ---------------------------------------------------------------- discover
def _tokens(card: str) -> list[str]:
    return [t for t in (" ".join(_html.unescape(x).split()) for x in _TAG.sub("|", re.sub(r"(?s)<svg.*?</svg>", "", card)).split("|")) if t]


def parse_cards(page: str) -> list[dict]:
    """Product cards of a listing page: {model, name, url, image, cooktop_type, burners}."""
    start = page.find('id="productCardContainer"')
    out = []
    for card in (page[start:].split('class="ProductCardPLP__root')[1:] if start >= 0 else []):
        href = re.search(r'href="(/us-en/[^"#?]+)"', card)
        tag = re.search(r'<img[^>]*id="imagePLPBackground"[^>]*>', card)
        title = re.search(r'\stitle="([^"]*)"', tag.group(0)) if tag else None
        gtm = re.search(r"item_name&quot;:&quot;([^&]+)&quot;", card)
        img = re.search(r'\s(?:data-img|src)="(/content/dam/[^"]+)"', tag.group(0)) if tag else None
        if not (href and title and gtm):
            continue
        model = _html.unescape(gtm.group(1)).strip()
        name = " ".join(_html.unescape(title.group(1)).split())
        name = name[: -len(model)].strip() if name.endswith(model) else name
        tok = _tokens(card)
        pair = lambda label: next((tok[i + 1] for i, t in enumerate(tok[:-1]) if t == label), "")
        out.append({"model": model, "name": name, "url": urljoin(BASE, href.group(1)), "image": img.group(1) if img else None,
                    "cooktop_type": pair(_LABELS[0]), "burners": pair(_LABELS[1])})
    return out


def card_to_candidate(c: dict, sub: str) -> Candidate | None:
    if not _SKU.match(c["model"]) or classify(c["name"], c["cooktop_type"], c["burners"]) != sub \
            or not _host_in(c["url"]) or not urlparse(c["url"]).path.startswith(ROOT):
        return None
    attrs: dict = {}
    m = re.search(r"(\d+(?:\.\d+)?)\s*cu\.? ?ft", c["name"], re.I)
    if m:
        attrs["capacity_total_cuft"] = float(m.group(1))
    m = re.match(r"(\d{2})\W", c["name"])
    if m:
        attrs["width_in"] = float(m.group(1))   # the marketing size in the name (30" range, 36" cooktop)
    if sub in ("gas_oven", "gas_cooktop"):
        attrs["fuel"] = "dual fuel" if "dual fuel" in c["name"].lower() else "gas"
    elif sub in ("electric_oven", "radiant", "induction"):
        attrs["fuel"] = "electric"
    return Candidate(brand=BRAND, model_number=c["model"], name=f"{c['name']} {c['model']}".strip(), url=c["url"], price_usd=None,
                     category="cooking", subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY, attrs=attrs,
                     attrs_src={k: "name" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Beko US")
    found: dict[str, Candidate] = {}
    b = _Browser()
    try:
        for cat in SUB_SOURCES[subcategory]:
            for card in parse_cards(b.get(f"{BASE}{ROOT}{cat}")):
                cand = card_to_candidate(card, subcategory)
                if cand:
                    found.setdefault(cand.model_number, cand)
            if len(found) >= limit:
                break
    finally:
        b.close()
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def spec_sections(page: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section, [(label, value)])]; a row without a value (feature present) becomes 'Yes'."""
    out = []
    parts = re.split(r'<h3 class="ContentToggle__title">', page)[1:]
    for part in parts:
        title = _TAG.sub(" ", part.split("</h3>", 1)[0])
        body = part.split("</h3>", 1)[1].split('class="Mt(20px)"', 1)[0]
        rows = []
        for row in re.split(r'<div class="PropertyTable__row"', body)[1:]:
            lab = re.search(r"<label[^>]*>(.*?)</label>", row, re.S)
            val = re.search(r'<span role="textbox"[^>]*>(.*?)</span>', row, re.S)
            if lab:
                rows.append((lab.group(1), val.group(1) if val else ""))
        out.append((" ".join(_html.unescape(title).split()),
                    [(" ".join(_html.unescape(_TAG.sub(" ", k)).split()), " ".join(_html.unescape(_TAG.sub(" ", v)).split()) or "Yes")
                     for k, v in rows]))
    return [(s, r) for s, r in out if r]


def _cm_in(value: str) -> float | None:
    n = common.num(r"([\d.]+)", value)
    return round(units.mm_to_in(n * 10), 2) if n is not None and "cm" in value.lower() else None


_DOC_TYPES = ((re.compile(r"user[-_ ]?manual", re.I), "Manual"), (re.compile(r"warranty", re.I), "Warranty"),
              (re.compile(r"installation", re.I), "Installation"), (re.compile(r"energy[-_ ]label", re.I), "EnergyGuide"),
              (re.compile(r"/pdf/product/|product[-_ ]form", re.I), "SpecSheet"))


def doc_links(page: str) -> dict[str, str]:
    """doc type -> https PDF url (first of each type) from the /content/dam PDF links."""
    out: dict[str, str] = {}
    for href in re.findall(r'href="(/content/dam/[^"]+\.pdf)"', page):
        url = urljoin(BASE, href)
        dtype = next((t for rx, t in _DOC_TYPES if rx.search(href)), None)
        if dtype and dtype not in out and _host_in(url):
            out[dtype] = url
    return out


def parse_product(page: str, url: str) -> tuple[ProductRecord, list[RawSpec]]:
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if not h1:
        raise ValueError("Beko US product page has no h1")
    head = " ".join(_html.unescape(_TAG.sub(" ", h1.group(1))).split())
    model, _, name = head.partition(": ")
    if not name:
        raise ValueError(f"unexpected Beko US title: {head[:80]}")
    sections = spec_sections(page)
    table: dict[str, str] = {}
    raw: list[RawSpec] = []
    flat: dict[str, str] = {}
    for sec, rows in sections:
        for k, v in rows:
            key = f"{sec} > {k}"
            table[key] = f"{table[key]} | {v}" if key in table else v
            flat.setdefault(k.lower(), v)
            raw.append(RawSpec(brand=BRAND, model_number=model, source="web", section=sec, key=k, value=v))
    for dtype, durl in doc_links(page).items():
        table[f"Documents > {dtype}"] = durl
    sub = classify(name, flat.get("cooktop type", ""), flat.get("burner configuration", ""))
    ld = re.search(r'"@type":"Product".*?"image":"(https://[^"]+)"', page, re.S)
    kg = common.num(r"([\d.]+)\s*kg", flat.get("weight", ""), re.I)
    cu = re.search(r"(\d+(?:\.\d+)?)\s*cu\.? ?ft", name, re.I)
    volt, hz = flat.get("voltage", ""), common.num(r"([\d.]+)\s*hz", flat.get("frequency", ""), re.I)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=sub, product_url=url, price_usd=None,
        region=REGION, country=COUNTRY, currency=CURRENCY, finish_color=flat.get("color") or None,
        width_in=_cm_in(flat.get("width", "")), height_in=_cm_in(flat.get("height", "")), depth_in=_cm_in(flat.get("depth", "")),
        weight_lb=round(units.kg_to_lb(kg), 1) if kg else None, capacity_total_cuft=float(cu.group(1)) if cu else None,
        voltage_v=re.sub(r"\s*V\s*$", "", volt) or None, frequency_hz=hz, extra_specs=table,
        image_url=ld.group(1) if ld and _host_in(ld.group(1)) else None)
    return record, raw


def _check_product_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() != "www.beko.com" or not u.path.startswith(ROOT) or len(u.path) <= len(ROOT) + 8:
        raise ValueError(f"not a Beko US product URL: {url}")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_product_url(url)
    b = _Browser()
    try:
        record, raw = parse_product(b.get(url), url)
    finally:
        b.close()
    return record, [], raw
