"""Fisher & Paykel US adapter (https://www.fisherpaykel.com/us; brand 'Fisher & Paykel'), cooking only.

The site is a Salesforce Commerce Cloud (Demandware) storefront with server-rendered pages:
  discover(): the category grids only render 12 products and paginate through /on/demandware.store/... AJAX URLs that
    robots.txt disallows (as it does every filter/sort query string). So the product list comes from the sitemaps robots.txt
    advertises (https://www.fisherpaykel.com/us/sitemap_index.xml -> the CustomSiteMap files): every cooking product page
    URL is /us/cooking/<ranges|cooktops|ovens>/<style>/<slug>-<id>.html, and classify() derives the sub key from that
    path. The model number, name and price are then read from each candidate's product page (1 request each).
  scrape(): the product page has schema.org JSON-LD (name, productID, images), the price (itemprop price), the key-feature
    bullets and the full spec tables (<div class="card"><h3>Section</h3><table><th class="name">Label</th><td class="value">).
Plain requests work; if the site answers 403 the page is loaded in a browser (FRIDGE_BROWSER_MODE=auto|headless|visible,
read on every call; auto = requests, headless, visible). Requests are spaced >= 1 s. Prices are the page's list price.
Supported (cooking only, per coordinator): gas_oven (gas / dual-fuel / hybrid ranges), induction (induction ranges and
cooktops), gas_cooktop (gas cooktops and rangetops), radiant (electric ranges and cooktops), electric_oven (wall ovens,
double ovens, combi-steam ovens), sco (convection speed ovens = microwave + convection), microwave (microwave drawer,
compact microwave), otr (over-the-range microwave). Refrigerators, laundry, dishwashers, ventilation are not implemented.
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

import common
import ge_us as ge
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Fisher & Paykel"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
BASE = "https://www.fisherpaykel.com"
HOSTS = ("fisherpaykel.com",)
SITEMAP_INDEX = BASE + "/us/sitemap_index.xml"
DELAY_S = 1.2
MAX_DOCS = 4
MAX_REDIRECTS = 5
SITEMAP_TTL_S = 3600
SUPPORTED_SUBCATEGORIES = {"gas_oven", "induction", "gas_cooktop", "radiant", "electric_oven", "sco", "microwave", "otr"}
_MAJOR = "cooking"
_FUEL = {"gas_oven": "gas", "gas_cooktop": "gas", "induction": "induction", "radiant": "electric"}
_PATH = re.compile(r"^/us/cooking/(ranges|cooktops|ovens)/[^/]+/([^/]+)-(\d+)\.html$")


class FPError(RuntimeError):
    """Fisher & Paykel page did not have the expected structure (site changed or blocked)."""


class FPNotFound(FPError):
    """HTTP 404/410: a browser cannot help."""


def _host_ok(url: str) -> bool:
    u = urlparse(url)
    h = (u.hostname or "").lower()
    return u.scheme == "https" and any(h == s or h.endswith("." + s) for s in HOSTS)


# ---------------------------------------------------------------- classification (URL path only: discover and scrape agree)
def classify(path: str) -> str | None:
    m = _PATH.match(urlparse(path).path)
    if not m:
        return None
    kind, slug = m.group(1), m.group(2).lower()
    if "teppanyaki" in slug:
        return None
    if kind == "ranges" and "range" in slug:
        if "induction" in slug:
            return "induction"
        if re.search(r"electric|radiant", slug):
            return "radiant"
        return "gas_oven" if re.search(r"gas|dual-fuel|duel-fuel|hybrid", slug) else None
    if kind == "cooktops":
        if "induction" in slug:
            return "induction"
        if re.search(r"-gas-|gas-(cooktop|rangetop)|rangetop", slug):
            return "gas_cooktop"
        return "radiant" if "electric" in slug else None
    if kind == "ovens":
        if "over-the-range" in slug:
            return "otr"
        if "microwave" in slug:
            return "microwave"
        if "speed-oven" in slug:
            return "sco"
        return "electric_oven" if "oven" in slug else None
    return None


# ---------------------------------------------------------------- HTTP
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = DELAY_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _strategies() -> list[str]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    return ["headless"] if mode == "headless" else ["visible"] if mode == "visible" else ["requests", "headless", "visible"]


def _via_requests(url: str) -> str:
    cur = url
    for _hop in range(MAX_REDIRECTS + 1):
        if not _host_ok(cur):
            raise FPError(f"unexpected url {cur[:120]}")
        _throttle()
        r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise FPError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise FPNotFound(f"HTTP {r.status_code} for {url}")
    if r.status_code != 200:
        raise FPError(f"bad status {r.status_code} for {url}")
    return r.content.decode("utf-8", "replace")


def _via_browser(url: str, headless: bool) -> str:
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if not _host_ok(page.url):
                raise FPError(f"unexpected url after navigation: {page.url[:120]}")
            ge._consent(page)
            status = resp.status if resp else None
            if status in (404, 410):
                raise FPNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise FPError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch(url: str, need: str) -> str:
    """Page text via requests, then headless, then visible; `need` must appear in the body (else treated as a bad load)."""
    last: Exception | None = None
    for strategy in _strategies():
        try:
            body = _via_requests(url) if strategy == "requests" else _via_browser(url, strategy == "headless")
            if need not in body:
                raise FPError(f"expected content {need!r} missing")
            return body
        except FPNotFound:
            raise
        except (FPError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"{BRAND} load via {strategy} failed: {e}", file=sys.stderr)
    raise FPError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- discover (sitemap)
_sitemap_cache: tuple[float, list[str]] | None = None


def parse_sitemap_urls(xml: str) -> list[str]:
    return [htmllib.unescape(u).strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


def product_urls() -> list[str]:
    """Absolute https URLs of every cooking product page in the US sitemaps (cached for an hour)."""
    global _sitemap_cache
    if _sitemap_cache and time.monotonic() - _sitemap_cache[0] < SITEMAP_TTL_S:
        return _sitemap_cache[1]
    out: list[str] = []
    for sm in parse_sitemap_urls(fetch(SITEMAP_INDEX, "<sitemapindex")):
        if not _host_ok(sm) or "/us/" not in sm or "sitemap_0" in sm:
            continue
        out += [u for u in parse_sitemap_urls(fetch(sm, "<urlset")) if _host_ok(u) and classify(u)]
    out = list(dict.fromkeys(out))
    if not out:
        raise FPError("no cooking product URLs in the sitemaps (structure changed?)")
    _sitemap_cache = (time.monotonic(), out)
    return out


def _slug_width(url: str) -> float | None:
    m = re.search(r"/(\d{2})in-", url)
    return float(m.group(1)) if m else None


def _fuel(sub: str, url: str) -> str | None:
    if sub == "gas_oven":
        return "dual_fuel" if re.search(r"dual-fuel|duel-fuel|hybrid", url) else "gas"
    return _FUEL.get(sub)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"Fisher & Paykel US adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    for url in product_urls():
        if len(found) >= limit:
            break
        if classify(url) != subcategory:
            continue
        try:
            rec = parse_page(url, fetch(url, "application/ld+json"))
        except (FPError, ValueError) as e:
            print(f"{BRAND}: skipped {url[-60:]}: {e}", file=sys.stderr)
            continue
        attrs = {k: v for k, v in (("width_in", _slug_width(url)), ("fuel", _fuel(subcategory, url))) if v}
        if rec["wifi"] is not None:
            attrs["wifi"] = rec["wifi"]
        found.setdefault(rec["model"], Candidate(
            brand=BRAND, model_number=rec["model"], name=rec["name"], url=url, price_usd=rec["price"],
            category=_MAJOR, subcategory=subcategory, region=REGION, country=COUNTRY, currency=CURRENCY, attrs=attrs))
    return list(found.values())[:limit]


# ---------------------------------------------------------------- page parsing (pure)
def _text(fragment: str) -> str:
    t = htmllib.unescape(re.sub(r"<br\s*/?>", " ", fragment or ""))
    t = re.sub(r"<[^>]+>", " ", t).replace("⁄", "/")  # fraction slash
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"(?<=\d) ?/ ?(?=\d)", "/", t)  # '29 7 / 8' -> '29 7/8'


def _first_num(v: str | None) -> float | None:
    return ge._first_num(v)


def spec_tables(html: str) -> list[tuple[str, str, str]]:
    """(section, label, value) from every spec card; a label with no value cell text is a flag ('Yes')."""
    rows: list[tuple[str, str, str]] = []
    for card in re.finditer(r'<div class="card">\s*<h3 class="h5">(.*?)</h3>\s*<table>(.*?)</table>', html, re.S):
        section = _text(card.group(1))
        for r in re.finditer(r'<th class="name">(.*?)</th>\s*<td class="value">(.*?)</td>', card.group(2), re.S):
            label, value = _text(r.group(1)), _text(r.group(2))
            if label:
                rows.append((section, label, value or "Yes"))
    return rows


def ld_product(html: str) -> dict:
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("@type") == "Product":
            return j
    raise FPError("no schema.org Product JSON-LD in page")


def _doc_type(url: str) -> str | None:
    name = urlparse(url).path.rsplit("/", 1)[-1].lower()
    if "-fr-" in name or "-es-" in name or "-zh-" in name or "proposition65" in name:
        return None
    for pat, dtype in (("userguide|usermanual|manual", "Manual"), ("installguide|installation", "Installation"),
                       ("specif|techspec|datasheet", "SpecSheet"), ("energy", "EnergyGuide")):
        if re.search(pat, name):
            return dtype
    return None


def doc_links(html: str) -> list[tuple[str, str]]:
    picked: dict[str, str] = {}
    for u in re.findall(r'href="([^"]+\.pdf[^"]*)"', html):
        u = htmllib.unescape(u)
        if u.startswith("https://") and _host_ok(u) and (t := _doc_type(u)):
            picked.setdefault(t, u)
    return list(picked.items())[:MAX_DOCS]


def _inches(v: str | None) -> float | None:
    return ge._inches(v)


def _key_features(html: str) -> list[str]:
    block = re.search(r'<div class="key-features">(.*?)</div>', html, re.S)
    return [t for t in (_text(x) for x in re.findall(r"<li>(.*?)</li>", block.group(1) if block else "", re.S)) if t]


def parse_page(url: str, html: str) -> dict:
    """Light parse used by discover: model, name, price, wifi."""
    j = ld_product(html)
    model = (j.get("productID") or j.get("sku") or "").strip().upper()
    if not model:
        raise FPError("product page has no productID")
    pm = re.search(r'itemprop="price"\s+content="([\d.]+)"', html)
    name = _text(j.get("name") or model)
    rows = spec_tables(html)
    blob = " ".join(f"{k} {v}" for _, k, v in rows) + " " + " ".join(_key_features(html))
    wifi = True if re.search(r"wi-?fi|connected", blob, re.I) else None
    return {"model": model, "name": name, "price": float(pm.group(1)) if pm else None, "wifi": wifi}


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    sub = classify(url)
    if not sub:
        raise ValueError(f"not a supported Fisher & Paykel cooking product URL: {url}")
    j = ld_product(html)
    page = parse_page(url, html)
    model, name = page["model"], page["name"]
    rows = spec_tables(html)
    spec: dict[str, str] = {}
    for _, k, v in rows:
        spec.setdefault(k, v)
    feats = _key_features(html)
    caps = re.search(r"(\d+(?:\.\d+)?)\s*cu\.?\s*ft", " ".join(feats), re.I)

    def dim(key: str, ldkey: str) -> float | None:
        return _inches(spec.get(key)) or _inches((j.get(ldkey) or {}).get("value") if isinstance(j.get(ldkey), dict) else None)

    volts = re.search(r"(\d{3})\s*V", spec.get("Supply voltage", ""))
    amps = _first_num(spec.get("Service"))
    hz = _first_num(spec.get("Supply frequency"))
    extra = {f"{s} > {k}": v for s, k, v in rows if not s.lower().startswith("accessories")}
    if feats:
        extra["Key features"] = "; ".join(feats)
    fuel = _fuel(sub, url)
    if fuel:
        extra["Fuel"] = {"gas": "Gas", "dual_fuel": "Dual fuel", "induction": "Induction", "electric": "Electric"}[fuel]
    image = next((u for u in (j.get("image") or []) if isinstance(u, str) and u.startswith("https://") and _host_ok(u)), None)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, product_url=url, category=_MAJOR, subcategory=sub,
        region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=page["price"],
        capacity_total_cuft=_first_num(spec.get("Total capacity")) or (float(caps.group(1)) if caps else None) if sub in ("gas_oven", "radiant", "induction", "electric_oven", "sco") else None,
        width_in=dim("Width", "width"), height_in=dim("Height", "height"), depth_in=dim("Depth", "depth"),
        voltage_v=f"{volts.group(1)}V" if volts else None, amps=amps, frequency_hz=hz,
        wifi_supported=page["wifi"], wifi_evidence="Fisher & Paykel product page mentions Wi-Fi/connected" if page["wifi"] else None,
        pod_features=feats, extra_specs=extra, image_url=image)
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=k, value=v) for s, k, v in rows]
    return record, raw, doc_links(html)


def check_url(url: str) -> None:
    if not _host_ok(url) or not classify(url):
        raise ValueError(f"not a Fisher & Paykel US cooking product URL: {url}")


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    check_url(url)
    product, raw, links = parse_product(url, fetch(url, "application/ld+json"))
    docs = []
    for dtype, link in links:
        time.sleep(DELAY_S)
        d = common.download_pdf(BRAND, ge._safe_name(product.model_number), dtype, link)
        if d:
            docs.append(d)
    return product, docs, raw
