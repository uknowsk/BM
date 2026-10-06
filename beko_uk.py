"""Beko UK adapter (catalog.py contract): cooking only (microwave, sco, gas_oven, gas_cooktop, electric_oven, induction, radiant).

Source: www.beko.co.uk (Beko's UK site; the 'beko.com/uk' address does not exist -- the UK site has its own domain; robots.txt
allows everything except filter/compare/where-to-buy endpoints). Pages are server rendered:
  * listing: /appliances/<group>/<category>?page=N, 12 products per page, schema.org ItemList JSON-LD (name, url, sku, image)
  * product: <h1>name <span>SKU</span></h1>, Product JSON-LD (image), and spec tables under
    <h4 data-optlabel="Specifications_Heading_<section>"> as <td class="left">label</td><td class="right">value</td> rows.
Beko UK publishes no prices (retailers only), so price_local is None. Plain requests first; 403/429/block page falls back to a browser
(FRIDGE_BROWSER_MODE auto|headless|visible), always closed. Units: the table mixes mm and 'centimeters' under an '(mm)' label, so
every length is read with its own unit word.
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
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Beko"
COUNTRY, REGION, CURRENCY = "uk", "eu", "GBP"
BASE = "https://www.beko.co.uk"
PAGE_HOSTS = ("beko.co.uk",)
DELAY_S = 1.0
MAX_PAGES = 6

_MW, _CK, _OV = "appliances/cooking/microwaves", "appliances/cooking/freestanding", "appliances/integrated-appliances/ovens"
_IMW, _HOB, _RNG = "appliances/integrated-appliances/microwaves", "appliances/integrated-appliances/hobs", "appliances/cooking/range-cookers"
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": (_MW, _IMW),
    "sco": (_IMW, _OV, _MW),
    "gas_oven": (_CK, _RNG),
    "gas_cooktop": (_HOB,),
    "electric_oven": (_OV,),
    "induction": (_HOB, _CK, _RNG),
    "radiant": (_HOB, _CK, _RNG),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)  # refrigerators and laundry exist on the site but are intentionally not implemented
_SKU = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")


# ---------------------------------------------------------------- classification
def classify(name: str) -> str | None:
    """Cooking sub key from the product name. Shared by discover() and scrape().
    Microwaves: built-in 'combination'/'oven microwave' = sco (oven + microwave), every other microwave = microwave.
    Cookers (freestanding / range): induction, else gas or dual fuel -> gas_oven, else electric (ceramic, solid plate) -> radiant.
    Hobs: gas -> gas_cooktop, induction -> induction, ceramic / sealed plate / solid plate -> radiant.
    Built-in ovens -> electric_oven. Anything else (hoods, ...) -> None."""
    n = (name or "").lower()
    gas = bool(re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", n))
    if re.search(r"\bhood\b|extractor|chimney|accessor", n):
        return None
    if "microwave" in n:
        built_in = bool(re.search(r"built[- ]?in|integrated", n))
        return "sco" if (built_in and "combination" in n) or re.search(r"oven microwave", n) else "microwave"
    if "cooker" in n:
        if "induction" in n:
            return "induction"
        return "gas_oven" if gas else "radiant"
    if re.search(r"\bhob\b", n):
        if "induction" in n:
            return "induction"
        return "gas_cooktop" if gas else "radiant"
    return "electric_oven" if re.search(r"\boven\b", n) else None


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
                raise RuntimeError(f"Beko UK browser unavailable: {last}")
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _check_url(self._page.url)
        self._page.wait_for_timeout(2000)
        if common.looks_blocked(resp.status if resp else None, self._page.inner_text("body")):
            raise RuntimeError(f"Beko UK blocked the request (status {resp.status if resp else None}); not bypassing")
        return self._page.content()

    def close(self):
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception as e:  # noqa: BLE001
                print(f"beko_uk: {fn} failed: {e}", file=sys.stderr)
        self._browser = self._page = self._pw = None


# ---------------------------------------------------------------- discover
def parse_listing(page: str) -> tuple[list[dict], int]:
    """(ItemList products {name, url, sku, image}, numberOfItems) of a listing page."""
    for blk in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            j = json.loads(blk)
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("@type") == "ItemList":
            items = [e["item"] for e in j.get("itemListElement") or [] if isinstance(e.get("item"), dict)]
            return items, int(j.get("numberOfItems") or len(items))
    raise ValueError("Beko UK listing has no ItemList JSON-LD")


def _clean_name(name: str) -> str:
    return " ".join(_html.unescape(name).replace("™", "").replace("®", "").split())


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    name, url, sku = _clean_name(str(item.get("name") or "")), str(item.get("url") or ""), str(item.get("sku") or "")
    if classify(name) != sub or not _SKU.match(sku) or urlparse(url).scheme != "https" or not _host_in(url) \
            or "/product/" not in url:
        return None
    attrs: dict = {}
    m = re.search(r"(\d{2,3})\s?cm\b", name)
    if m:
        attrs["width_in"] = round(units.mm_to_in(int(m.group(1)) * 10), 1)
    m = re.search(r"(\d+)\s*litre", name, re.I)
    if m:
        attrs["capacity_total_cuft"] = round(units.l_to_cuft(int(m.group(1))), 2)
    if sub in ("gas_oven", "gas_cooktop"):
        attrs["fuel"] = "dual fuel" if "dual fuel" in name.lower() else "gas"
    elif sub in ("electric_oven", "radiant", "induction"):
        attrs["fuel"] = "electric"
    if re.search(r"wi-?fi|smart", name, re.I):
        attrs["wifi"] = True
    return Candidate(brand=BRAND, model_number=sku, name=name, url=url, price_usd=None, category="cooking", subcategory=sub,
                     region=REGION, country=COUNTRY, currency=CURRENCY, price_local=None, attrs=attrs,
                     attrs_src={k: "name" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Beko UK")
    found: dict[str, Candidate] = {}
    f = _Fetcher()
    try:
        for path in SUB_SOURCES[subcategory]:
            seen = 0
            for page in range(1, MAX_PAGES + 1):
                items, total = parse_listing(f.get(f"{BASE}/{path}" + (f"?page={page}" if page > 1 else "")))
                seen += len(items)
                for it in items:
                    c = item_to_candidate(it, subcategory)
                    if c:
                        found.setdefault(c.model_number, c)
                if not items or seen >= total or len(found) >= limit:
                    break
            if len(found) >= limit:
                break
    finally:
        f.close()
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def _text(s: str) -> str:
    return " ".join(_html.unescape(_TAG.sub(" ", s)).split())


def spec_sections(page: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section, [(label, value)])] from the specification tables; empty-valued rows are dropped."""
    out = []
    for m in re.finditer(r'<h4[^>]*data-optlabel="Specifications_Heading_([^"]*)"[^>]*>.*?</h4>(.*?)(?=<h4|</section|<h2|\Z)', page, re.S):
        rows = re.findall(r'<td class="left[^"]*"[^>]*>\s*<span class="cont">(.*?)</span>\s*</td>\s*'
                          r'<td class="right[^"]*">\s*<span class="cont">(.*?)</span>', m.group(2), re.S)
        clean = [(_text(k), _text(v)) for k, v in rows]
        out.append((_html.unescape(m.group(1)).strip(), [(k, v) for k, v in clean if k and v]))
    return out


def _mm(value: str) -> float | None:
    """Length in mm from '1790', '203.5 centimeters', '56x55' (first number), '60 cm' (value unit wins over the label)."""
    n = common.num(r"([\d.]+)", value.replace(",", "."))
    if n is None:
        return None
    v = value.lower()
    return n * 10 if re.search(r"centimet|\bcm\b", v) else n * 1000 if re.search(r"\bmetre|\bm\b", v) else n


def _kg(value: str) -> float | None:
    return common.num(r"([\d.]+)", value.replace(",", "."))


_DOC_BY_NAME = ((re.compile(r"_COMBINED\.pdf$", re.I), "EnergyGuide"), (re.compile(r"\.pdf$", re.I), "SpecSheet"))


def doc_links(page: str) -> list[tuple[str, str]]:
    """[(doc_type, url)] PDFs on beko.co.uk hosts (energy label '_COMBINED', product information sheet). The user manual is
    hosted on bekoplc.blob.core.windows.net (not an allowed download host) and is skipped."""
    out, seen = [], set()
    for u in re.findall(r'href="(https://[^"]+\.pdf)"', page):
        if u in seen or not _host_in(u):
            continue
        seen.add(u)
        dtype = next(t for rx, t in _DOC_BY_NAME if rx.search(u))
        if dtype not in [t for t, _ in out]:
            out.append((dtype, u))
    return out


def parse_product(page: str, url: str) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if not h1:
        raise ValueError("Beko UK product page has no h1")
    span = re.search(r"<span>([^<]+)</span>", h1.group(1))
    name = _clean_name(_TAG.sub(" ", h1.group(1)))
    model = (span.group(1).strip() if span else name.split()[-1])
    sections = spec_sections(page)
    table: dict[str, str] = {}
    raw: list[RawSpec] = []
    flat: dict[str, str] = {}
    for sec, rows in sections:
        if sec.lower() == "model codes":
            continue
        for k, v in rows:
            key = f"{sec} > {k}"
            table[key] = f"{table[key]} | {v}" if key in table else v
            flat.setdefault(k.lower(), v)
            raw.append(RawSpec(brand=BRAND, model_number=model, source="web", section=sec, key=k, value=v))
    cls = units.eu_energy_class(flat.get("energy efficiency class") or flat.get("energy rating"))
    if cls:
        table["Energy > EU energy class"] = cls
    pick = lambda *labels: next((flat[x] for x in labels if x in flat), "")
    dims = {a: _mm(pick(f"product {a} (mm)")) for a in ("height", "width", "depth")}
    kg = _kg(pick("net weight (kg)", "weight (kg)"))
    # one cavity only: ranges list 'Volume (litre)' per cavity, so their capacity stays in extra_specs
    litres = _kg(pick("capacity (litres)", "volume"))
    sub = classify(name)
    ld = re.search(r'"image":\s*"(https://[^"]+)"', page)
    img = ld.group(1) if ld else (re.search(r'<meta property="og:image" content="(https://[^"]+)"', page) or [None, None])[1]
    wifi = bool(re.search(r"wi-?fi|smart", name, re.I))
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=sub, product_url=url,
        price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=None,
        width_in=round(units.mm_to_in(dims["width"]), 2) if dims["width"] else None,
        height_in=round(units.mm_to_in(dims["height"]), 2) if dims["height"] else None,
        depth_in=round(units.mm_to_in(dims["depth"]), 2) if dims["depth"] else None,
        weight_lb=round(units.kg_to_lb(kg), 1) if kg else None,
        capacity_total_cuft=round(units.l_to_cuft(litres), 2) if litres else None,
        wifi_supported=True if wifi else None, wifi_evidence=f"name: {name}" if wifi else None,
        extra_specs=table, image_url=img if img and _host_in(img) else None)
    return record, raw, doc_links(page)


def _check_product_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() != "www.beko.co.uk" or "/product/" not in u.path \
            or not u.path.startswith("/appliances/"):
        raise ValueError(f"not a Beko UK product URL: {url}")


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
