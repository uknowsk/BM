"""Hisense US adapter (catalog.py contract): cooking only (microwave, otr, gas_oven, radiant).

Source: www.hisense-usa.com is a Wix Stores site. Category pages (/category/<slug>) and product pages
(/product-page/<slug>) embed their data as JSON in <script id="wix-warmup-data">:
  * category page: appsWarmupData.<app>.products_default_*.{list,totalCount}; items carry sku, name, urlPart, price, media.
    The first page holds 24 items, '?page=2' returns the rest.
  * product page: recordsByCollectionId (Stores/Products, Products1 with supportDocuments, Specs when the page embeds
    them, ProductsKeyFeatures with feature sentences).
Price: Hisense does not sell directly; the Wix price field is a placeholder ($0 / $10.50), so price_usd is only kept when
it is >= MIN_REAL_PRICE (otherwise None). Pages are fetched with plain requests first (robots.txt allows it); a 403/429 or
a block page falls back to a browser (FRIDGE_BROWSER_MODE auto|headless|visible), which is always closed.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlparse

import requests

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Hisense"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://www.hisense-usa.com"
PAGE_HOSTS = ("hisense-usa.com",)
IMAGE_HOSTS = ("wixstatic.com", "hisense-usa.com")
DELAY_S = 1.0
MAX_PAGES = 4
MIN_REAL_PRICE = 100.0
MAX_DOCS = 4

# sub key -> category slugs that list it (cooking only; refrigerators/washers are intentionally not implemented)
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("microwave-ovens", "over-the-range-microwave"),
    "otr": ("microwave-ovens", "over-the-range-microwave"),
    "gas_oven": ("cooking-ranges",),
    "electric_oven": ("cooking-ranges",),
    "induction": ("cooking-ranges",),
    "radiant": ("cooking-ranges",),
    "gas_cooktop": ("cooking-ranges",),
}
# Hisense US sells ranges (oven included), no wall ovens / cooktops / SCO today; those keys stay unsupported until a
# product exists: electric_oven, induction, gas_cooktop are handled by classify() but listed only when found.
SUPPORTED_SUBCATEGORIES = {"microwave", "otr", "gas_oven", "radiant"}

_WARMUP = re.compile(r'<script type="application/json" id="wix-warmup-data">(.*?)</script>', re.S)
_SKU = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_CUFT = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*cu\.?\s*-?\s*ft", re.I)


# ---------------------------------------------------------------- classification
def classify(name: str) -> str | None:
    """Cooking sub key from a product name (single source of truth for discover() and scrape()).
    over-the-range -> otr; other microwave -> microwave; induction; gas range/oven -> gas_oven; gas cooktop ->
    gas_cooktop; electric range/oven -> radiant; anything else (accessories, TVs, ...) -> None."""
    n = (name or "").lower()
    if re.search(r"over[- ]the[- ]range|\botr\b", n):
        return "otr"
    if "microwave" in n:
        return "microwave"
    gas = bool(re.search(r"(?<![a-z])gas(?![a-z])", n))
    if re.search(r"cooktop|rangetop", n):
        if "induction" in n:
            return "induction"
        return "gas_cooktop" if gas else "radiant"
    if re.search(r"\brange\b|\boven\b", n):
        if "induction" in n:
            return "induction"
        if gas:
            return "gas_oven"
        return "radiant" if "electric" in n else None
    return None


# ---------------------------------------------------------------- fetching
def _modes() -> list[bool]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    return [True] if mode == "headless" else [False] if mode == "visible" else [True, False]


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url, PAGE_HOSTS):
        raise ValueError(f"unexpected host: {url[:120]}")


class _Fetcher:
    """requests first (one GET per call, >= DELAY_S apart); on 403/429/block page a browser page is used instead."""

    def __init__(self):
        self._last = 0.0
        self._pw = self._browser = self._page = None

    def _wait(self):
        gap = DELAY_S - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def get(self, url: str) -> str:
        _check_url(url)
        self._wait()
        if self._page is None:
            cur = url
            for _hop in range(5):
                r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=45, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    cur = requests.compat.urljoin(cur, r.headers["Location"])
                    _check_url(cur)
                    continue
                if r.status_code == 200 and not common.looks_blocked(r.status_code, r.text[:200000]):
                    return r.text
                break
        return self._browser_get(url)

    def _browser_get(self, url: str) -> str:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        if self._page is None:
            self._pw = sync_playwright().start()
            last: Exception | None = None
            for headless in _modes():
                try:
                    self._browser = self._pw.chromium.launch(channel="chromium", headless=headless)
                    self._page = self._browser.new_context(user_agent=common.UA).new_page()
                    break
                except PlaywrightError as e:
                    last = e
                    self.close()
                    self._pw = sync_playwright().start()
            if self._page is None:
                raise RuntimeError(f"Hisense browser unavailable: {last}")
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _check_url(self._page.url)
        self._page.wait_for_timeout(2000)
        html = self._page.content()
        if common.looks_blocked(resp.status if resp else None, self._page.inner_text("body")):
            raise RuntimeError(f"Hisense blocked the request (status {resp.status if resp else None}); not bypassing")
        return html

    def close(self):
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception as e:  # noqa: BLE001 - closing must not mask the real error
                print(f"hisense_us: {fn} failed: {e}", file=sys.stderr)
        self._browser = self._page = self._pw = None


# ---------------------------------------------------------------- parsing
def warmup(html: str) -> dict:
    m = _WARMUP.search(html)
    if not m:
        raise ValueError("Hisense page has no wix-warmup-data")
    return json.loads(m.group(1))


def parse_listing(html: str) -> tuple[list[dict], int]:
    """(product items, totalCount) of a category page."""
    apps = warmup(html).get("appsWarmupData") or {}
    for app in apps.values():
        for key, val in (app.items() if isinstance(app, dict) else []):
            if key.startswith("products_") and isinstance(val, dict) and isinstance(val.get("list"), list):
                return val["list"], int(val.get("totalCount") or len(val["list"]))
    raise ValueError("Hisense listing payload unrecognised")


def _price(item: dict) -> float | None:
    p = item.get("price")
    return float(p) if isinstance(p, (int, float)) and p >= MIN_REAL_PRICE else None


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    sku, name, part = item.get("sku"), item.get("name") or "", item.get("urlPart") or ""
    if not sku or not _SKU.match(sku) or not re.fullmatch(r"[^/\s]+", part) or classify(name) != sub:
        return None
    attrs: dict = {}
    m = _CUFT.search(name)
    if m:
        attrs["capacity_total_cuft"] = float(m.group(1))
    if sub in ("gas_oven", "gas_cooktop"):
        attrs["fuel"] = "gas"
    elif sub in ("radiant", "induction", "electric_oven"):
        attrs["fuel"] = "electric"
    return Candidate(brand=BRAND, model_number=sku, name=" ".join(name.split()), url=f"{BASE}/product-page/{quote(part)}",
                     price_usd=_price(item), category="cooking", subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, attrs=attrs, attrs_src={k: "name" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Hisense US")
    found: dict[str, Candidate] = {}
    f = _Fetcher()
    try:
        for slug in SUB_SOURCES[subcategory]:
            for page in range(1, MAX_PAGES + 1):
                items, total = parse_listing(f.get(f"{BASE}/category/{slug}" + (f"?page={page}" if page > 1 else "")))
                before = len(found)
                for it in items:
                    c = item_to_candidate(it, subcategory)
                    if c:
                        found.setdefault(c.model_number, c)
                if len(found) >= limit or len(items) >= total or not items or (page > 1 and len(found) == before):
                    break
            if len(found) >= limit:
                break
    finally:
        f.close()
    return list(found.values())[:limit]


def _records(w: dict) -> dict[str, dict]:
    apps = w.get("appsWarmupData") or {}
    for app in apps.values():
        if isinstance(app, dict) and "dataStore" in app:
            return app["dataStore"].get("recordsByCollectionId") or {}
    return {}


def _frac(s: str) -> float:
    total = 0.0
    for part in s.split():
        total += int(part.split("/")[0]) / int(part.split("/")[1]) if "/" in part else float(part)
    return total


def _dims_in(text: str) -> tuple[float, float, float] | None:
    """(W, D, H) inches from '35.9" x 33.3" x 70.3"' style text (assumes W x D x H as Hisense labels them)."""
    nums = re.findall(r"\d+(?:\s+\d+/\d+|\.\d+)?", text.replace('"', " ").replace("”", " "))
    return tuple(_frac(n) for n in nums[:3]) if len(nums) >= 3 else None  # type: ignore[return-value]


def _wix_image(src: str | None) -> str | None:
    """wix:image://v1/<id>/<name>#... -> 800 px JPEG on static.wixstatic.com."""
    m = re.match(r"wix:image://v1/([^/]+)/", src or "")
    return f"https://static.wixstatic.com/media/{m.group(1)}/v1/fit/w_800,h_800,q_90/file.jpg" if m else None


def _wix_doc(src: str) -> str | None:
    m = re.match(r"wix:document://v1/([A-Za-z0-9_]+\.pdf)/", src)
    return f"{BASE}/_files/ugd/{m.group(1)}" if m else None


_DOC_TYPES = (("spec", "SpecSheet"), ("warranty", "Warranty"), ("install", "Installation"), ("manual", "Manual"),
              ("instructions for use", "Manual"))


def doc_links(product_row: dict) -> list[tuple[str, str]]:
    """[(doc_type, https pdf url)] from Products1.supportDocuments, one per type, at most MAX_DOCS."""
    out, seen = [], set()
    for d in product_row.get("supportDocuments") or []:
        if not isinstance(d, str) or not d.startswith("wix:document://"):
            continue
        url, label = _wix_doc(d), unquote(d.rsplit("/", 1)[-1]).lower()
        dtype = next((t for k, t in _DOC_TYPES if k in label), None)
        if url and dtype and dtype not in seen and _host_in(url, PAGE_HOSTS):
            seen.add(dtype)
            out.append((dtype, url))
    return out[:MAX_DOCS]


def parse_product(html: str, url: str) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    w = warmup(html)
    recs = _records(w)
    store = next(iter((recs.get("Stores/Products") or {}).values()), None)
    if not store:
        raise ValueError("Hisense product payload has no Stores/Products record")
    detail = next(iter((recs.get("Products1") or {}).values()), {})
    sku, name = store["sku"], " ".join(str(store.get("name") or detail.get("title") or store["sku"]).split())
    specs = sorted((r for r in (recs.get("Specs") or {}).values() if r.get("details")),
                   key=lambda r: (r.get("subheaderOrder", 0), r.get("termOrder", 0)))
    table: dict[str, str] = {}
    raw: list[RawSpec] = []
    for r in specs:
        sec, key, val = str(r.get("subheader") or "").strip(), str(r["term"]).strip(), " ".join(str(r["details"]).split())
        table[f"{sec} > {key}" if sec else key] = val
        raw.append(RawSpec(brand=BRAND, model_number=sku, source="web", section=sec, key=key, value=val))
    feats = [" ".join(str(r["description"]).split()) for r in (recs.get("ProductsKeyFeatures") or {}).values()
             if r.get("description")]
    for i, ft in enumerate(feats, 1):
        table[f"Key features > Feature {i}"] = ft
        raw.append(RawSpec(brand=BRAND, model_number=sku, source="web", section="Key features", key=f"Feature {i}", value=ft))
    by_term = {k.split(" > ")[-1].lower(): v for k, v in table.items()}
    dims = next((_dims_in(v) for k, v in by_term.items() if "dimension" in k and "ship" not in k and "carton" not in k), None)
    wt = common.num(r"([\d.,]+)\s*lb", by_term.get("net weight", ""), re.I)
    sub = classify(name)
    m = _CUFT.search(name)
    if m:
        table["Specifications > Capacity (cu ft)"] = m.group(1)
    record = ProductRecord(
        brand=BRAND, model_number=sku, product_name=name, category="cooking", subcategory=sub, product_url=url,
        price_usd=_price(store), region=REGION, country=COUNTRY, currency=CURRENCY,
        width_in=dims[0] if dims else None, depth_in=dims[1] if dims else None, height_in=dims[2] if dims else None,
        weight_lb=wt, pod_features=feats[:12], extra_specs=table, image_url=_wix_image(store.get("mainMedia")))
    return record, raw, doc_links(detail)


def _check_product_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not _host_in(url, PAGE_HOSTS) or not u.path.startswith("/product-page/"):
        raise ValueError(f"not a Hisense US product URL: {url}")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_product_url(url)
    f = _Fetcher()
    try:
        record, raw, links = parse_product(f.get(url), url)
    finally:
        f.close()
    docs: list[DocumentRecord] = []
    for dtype, durl in links:
        time.sleep(DELAY_S)
        d = common.download_pdf(BRAND, record.model_number, dtype, durl)
        if d:
            docs.append(d)
    return record, docs, raw
