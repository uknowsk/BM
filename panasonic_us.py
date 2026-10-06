"""Panasonic US adapter (catalog.py contract): cooking only (microwave, sco).

Panasonic sells only microwave ovens / multi-ovens as home cooking appliances in the US (no ranges, cooktops, OTR or wall
ovens; refrigerators/washers are not sold). Source: shop.panasonic.com (Shopify, the official US store; robots.txt allows
/collections/*/products.json and /products/*).
  * discover: /collections/microwave-ovens/products.json?limit=250 (title, handle, variants[0].sku/price, images)
  * scrape: the product page (HTML): `const productName/productPrice` JS constants, the schema.org Product JSON-LD
    (description carries 'Dimensions: ...' and 'Weight: ...'), the 'Features' accordion bullets, og:image, and the
    help.na.panasonic.com user-manual PDF link. The page's own spec chart is filled client side and is empty in the HTML,
    so the spec table is intentionally small (title facts + dimensions + weight + feature bullets).
Price = variants[0].price (selling price; compare_at_price is not used). Plain requests; a 403/429/block page falls back to a
browser (FRIDGE_BROWSER_MODE auto|headless|visible), always closed.
"""
import html as _html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Panasonic"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://shop.panasonic.com"
PAGE_HOSTS = ("panasonic.com",)
DELAY_S = 1.0
MAX_PAGES = 3
SUPPORTED_SUBCATEGORIES = {"microwave", "sco"}
COLLECTIONS = ("microwave-ovens",)

_NOT_AN_OVEN = re.compile(r"trim kit|accessor|filter|turntable|replacement|mounting|cart\b|stand\b", re.I)
_SCO = re.compile(r"multi[- ]?oven|combination|air[- ]?fry|convection|home\s?chef|home\s?made", re.I)
_CUFT = re.compile(r"(\d+(?:\.\d+)?)\s*cu\.?\s*ft", re.I)
_WATT = re.compile(r"(\d{3,4})\s*W\b")
_MODEL = re.compile(r"\b(NN-[A-Z0-9]{4,})\b")
_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{2,150}$")


def classify(title: str) -> str | None:
    """'sco' for multi-ovens / microwave-convection / air-fry combination ovens, 'microwave' for other microwave ovens,
    None for accessories and anything that is not a microwave oven. Shared by discover() and scrape()."""
    t = title or ""
    if _NOT_AN_OVEN.search(t) or not re.search(r"microwave|multi[- ]?oven|\boven\b", t, re.I):
        return None
    return "sco" if _SCO.search(t) else "microwave"


def model_of(title: str, sku: str = "") -> str:
    m = _MODEL.search(title or "")
    return m.group(1) if m else (sku or "").strip()


# ---------------------------------------------------------------- fetching
def _modes() -> list[bool]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    return [True] if mode == "headless" else [False] if mode == "visible" else [True, False]


def _host_in(url: str, suffixes: tuple[str, ...] = PAGE_HOSTS) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url):
        raise ValueError(f"unexpected host: {url[:120]}")


class _Fetcher:
    """requests first (>= DELAY_S between requests); on 403/429/block page a browser page is used."""

    def __init__(self):
        self._last = 0.0
        self._pw = self._browser = self._page = None

    def get(self, url: str) -> str:
        _check_url(url)
        gap = DELAY_S - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        if self._page is None:
            cur = url
            for _hop in range(5):
                r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=45, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    cur = urljoin(cur, r.headers["Location"])
                    _check_url(cur)
                    continue
                if r.status_code == 200 and not common.looks_blocked(r.status_code, r.text[:200000]):
                    return r.text
                break
        return self._browser_get(url)

    def _browser_get(self, url: str) -> str:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        if self._page is None:
            last: Exception | None = None
            for headless in _modes():
                self._pw = sync_playwright().start()
                try:
                    self._browser = self._pw.chromium.launch(channel="chromium", headless=headless)
                    self._page = self._browser.new_context(user_agent=common.UA).new_page()
                    break
                except PlaywrightError as e:
                    last = e
                    self.close()
            if self._page is None:
                raise RuntimeError(f"Panasonic browser unavailable: {last}")
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _check_url(self._page.url)
        self._page.wait_for_timeout(2000)
        if common.looks_blocked(resp.status if resp else None, self._page.inner_text("body")):
            raise RuntimeError(f"Panasonic blocked the request (status {resp.status if resp else None}); not bypassing")
        return self._page.content()

    def close(self):
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception as e:  # noqa: BLE001
                print(f"panasonic_us: {fn} failed: {e}", file=sys.stderr)
        self._browser = self._page = self._pw = None


# ---------------------------------------------------------------- discover
def _https(u: str | None) -> str | None:
    if not u:
        return None
    u = "https:" + u if u.startswith("//") else u.replace("http://", "https://", 1)
    return u if urlparse(u).scheme == "https" and _host_in(u) else None


def product_to_candidate(p: dict, sub: str) -> Candidate | None:
    title, handle = " ".join(str(p.get("title") or "").split()), p.get("handle") or ""
    var = (p.get("variants") or [{}])[0]
    if classify(title) != sub or not _HANDLE.match(handle):
        return None
    try:
        price = float(var.get("price")) if var.get("price") not in (None, "") else None
    except ValueError:
        price = None
    attrs: dict = {}
    m = _CUFT.search(title)
    if m:
        attrs["capacity_total_cuft"] = float(m.group(1))
    return Candidate(brand=BRAND, model_number=model_of(title, var.get("sku") or ""), name=title,
                     url=f"{BASE}/products/{handle}", price_usd=price, category="cooking", subcategory=sub, region=REGION,
                     country=COUNTRY, currency=CURRENCY, attrs=attrs, attrs_src={k: "name" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Panasonic US")
    found: dict[str, Candidate] = {}
    f = _Fetcher()
    try:
        for col in COLLECTIONS:
            for page in range(1, MAX_PAGES + 1):
                products = json.loads(f.get(f"{BASE}/collections/{col}/products.json?limit=250&page={page}")).get("products") or []
                for p in products:
                    c = product_to_candidate(p, subcategory)
                    if c:
                        found.setdefault(c.model_number, c)
                if len(products) < 250 or len(found) >= limit:
                    break
    finally:
        f.close()
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def _frac(s: str) -> float:
    total = 0.0
    for part in s.split():
        total += int(part.split("/")[0]) / int(part.split("/")[1]) if "/" in part else float(part)
    return total


def parse_product(page: str, url: str) -> tuple[ProductRecord, list[RawSpec], str | None]:
    """(record, raw specs, manual PDF url) from a product page."""
    name = re.search(r"const productName = `(.*?)`", page)
    price = re.search(r"productPrice = Number\.parseFloat\(([\d.]+)\)", page)
    if not name:
        raise ValueError("Panasonic product page has no productName constant")
    title = " ".join(_html.unescape(name.group(1)).split())
    sku = (re.search(r"const productSku = `(.*?)`", page) or [None, ""])[1]
    desc = ""
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        # the store's JSON-LD is not always valid JSON (stray bracket), so read the description string with a regex
        m = re.search(r'"@type":\s*"Product".*?"description":\s*("(?:[^"\\]|\\.)*")', blk, re.S)
        if m:
            try:
                desc = _html.unescape(json.loads(m.group(1)))
            except ValueError:
                continue
            break
    table: dict[str, str] = {}
    cu, watt = _CUFT.search(title), _WATT.search(title)
    if cu:
        table["Specifications > Capacity (cu ft)"] = cu.group(1)
    if watt:
        table["Specifications > Microwave power (W)"] = watt.group(1)
    dm = re.search(r"Dimensions:\s*([^\n]+)", desc)
    wm = re.search(r"Weight:\s*([\d.]+)\s*lbs?", desc)
    w = h = d = None
    if dm:
        table["Specifications > Dimensions"] = " ".join(dm.group(1).split())
        parts = re.findall(r'(\d+(?:\s+\d+/\d+|\.\d+)?)"?\s*([WHD])\b', dm.group(1))
        got = {k: _frac(v) for v, k in parts}
        w, h, d = got.get("W"), got.get("H"), got.get("D")
    if wm:
        table["Specifications > Weight (lb)"] = wm.group(1)
    feats: list[str] = []
    for m in re.finditer(r'<details\s+class="product-info__accordion', page):
        seg = page[m.start():m.start() + 12000]
        t = re.search(r"<span\s*>([^<]+)</span>", seg)
        body = re.search(r'accordion__content">(.*?)</details>', seg, re.S)
        if not (t and body):
            continue
        sec = _html.unescape(t.group(1)).strip()
        for i, li in enumerate(re.findall(r"<li>(.*?)</li>", body.group(1), re.S), 1):
            txt = " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", li)).split())
            if txt:
                table[f"{sec} > {i}"] = txt
                if sec.lower() == "features" and not txt.startswith("IMPORTANT"):
                    feats.append(txt)
    img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    manual = re.search(r'href="(https://help\.na\.panasonic\.com/[^"]+\.pdf)"', page)
    model = model_of(title, sku)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=title, category="cooking", subcategory=classify(title),
        product_url=url, price_usd=float(price.group(1)) if price else None, region=REGION, country=COUNTRY,
        currency=CURRENCY, width_in=w, height_in=h, depth_in=d, weight_lb=float(wm.group(1)) if wm else None,
        capacity_total_cuft=float(cu.group(1)) if cu else None, pod_features=feats[:12], extra_specs=table,
        image_url=_https(_html.unescape(img.group(1))) if img else None)
    raw = []
    for k, v in table.items():
        sec, _, key = k.partition(" > ")
        raw.append(RawSpec(brand=BRAND, model_number=model, source="web", section=sec, key=key, value=v))
    return record, raw, manual.group(1) if manual else None


def _check_product_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() != "shop.panasonic.com" or not u.path.startswith("/products/"):
        raise ValueError(f"not a Panasonic US product URL: {url}")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_product_url(url)
    f = _Fetcher()
    try:
        record, raw, manual = parse_product(f.get(url), url)
    finally:
        f.close()
    docs: list[DocumentRecord] = []
    if manual and _host_in(manual):
        time.sleep(DELAY_S)
        d = common.download_pdf(BRAND, record.model_number, "Manual", manual)
        if d:
            docs.append(d)
    return record, docs, raw
