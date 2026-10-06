"""Shared helpers for the Sub-Zero Group sites (Wolf, Sub-Zero; adapters wolf_us.py / subzero_us.py).

https://www.subzero-wolf.com is an Adobe Edge Delivery + Adobe Commerce storefront. Product pages are client-rendered and
geo-redirect non-US visitors to a country picker, so this module reads the catalogue from the storefront's own
Commerce "catalog service" GraphQL endpoint instead (robots.txt disallows /api and /admin, not /szg-api):
  * https://www.subzero-wolf.com/config.json  -> public storefront settings: commerce endpoint + the public store-view
    headers/x-api-key the web page itself sends. They are read at runtime (never stored in the repo).
  * <commerce-endpoint> (POST GraphQL): productSearch(filter manufacturername / is_accessory / mnseries) for discovery,
    products(skus:[sku]) for one product. `attributes` (visible_in_pdp) carry the full spec table.
Orderable products are 'simple' SKUs (one per finish/fuel/handing); the base model is the `modelnumber` attribute.
Product page URL = /products/<urlKey>/<sku with '__' written as '-'>. Price = `base_price` of that SKU (list price).
Spec/manual PDFs are not downloaded (the spec library is behind other endpoints / robots-disallowed paths): no documents.
Fetching: plain requests, then headless Chromium, then a visible window (FRIDGE_BROWSER_MODE = auto | headless | visible);
requests >= 1 s apart.
"""
import html as _html
import json
import os
import re
import sys
import time
from datetime import date
from urllib.parse import urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BASE = "https://www.subzero-wolf.com"
PAGE_HOSTS = ("subzero-wolf.com",)
IMAGE_HOSTS = ("adobeaemcloud.com", "subzero-wolf.com")
DELAY_S = 1.0
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
PAGE_SIZE = 50
MAX_PAGES = 12

# attribute names that are plumbing, not specs (kept out of extra_specs; they stay out of RawSpec too)
_HIDDEN = {"base_accessory_config", "combinedmodelnumber", "combinedsku", "configurabledetailsurl", "is_accessory",
           "is_spec_product", "parent_sku", "product_classification", "sitecoreurl", "stark", "features", "modelnumber",
           "specmodelnumber", "sz_categories", "mnrangetopconfig_filter"}

PRODUCT_FIELDS = """sku name description urlKey inStock
  images { url roles }
  attributes { name label value roles }
  ... on SimpleProductView { price { final { amount { value currency } } regular { amount { value } } } }"""
SEARCH_QUERY = """query($f: [SearchClauseInput!], $p: Int) {
  productSearch(phrase: "", filter: $f, page_size: %d, current_page: $p) {
    total_count page_info { current_page total_pages }
    items { productView { %s } } } }""" % (PAGE_SIZE, PRODUCT_FIELDS)
PRODUCT_QUERY = "query($skus: [String!]!) { products(skus: $skus) { %s } }" % PRODUCT_FIELDS


class SZError(RuntimeError):
    pass


def _strategies() -> list[str]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return ["headless"]
    if mode == "visible":
        return ["visible"]
    return ["requests", "headless", "visible"]


def host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_https_host(url: str) -> None:
    if urlparse(url).scheme != "https" or not host_in(url, PAGE_HOSTS):
        raise ValueError(f"unexpected host: {url[:120]}")


_last_request = [0.0]


def _throttle() -> None:
    wait = DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


# ---------------------------------------------------------------- transport
def _requests_json(method: str, url: str, headers: dict | None = None, body: dict | None = None):
    _check_https_host(url)
    _throttle()
    r = requests.request(method, url, headers={"User-Agent": common.UA, **(headers or {})}, json=body, timeout=40,
                         allow_redirects=False)
    if r.status_code != 200:
        raise SZError(f"blocked or bad status ({r.status_code}) for {url}")
    return r.json()


_BROWSER_FETCH_JS = """async ({u, method, headers, body}) => {
  const r = await fetch(u, {method, headers, body: body ? JSON.stringify(body) : undefined, redirect: 'manual'});
  if (!r.ok) return {s: r.status};
  return {s: 200, j: await r.json()};
}"""


def _browser_json(method: str, url: str, headers: dict | None, body: dict | None, headless: bool):
    """Same request from inside a real browser page on the site's own origin (config.json is a plain JSON document, so no
    geo redirect / consent banner is involved)."""
    _check_https_host(url)
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA).new_page()
            _throttle()
            page.goto(BASE + "/config.json", wait_until="domcontentloaded", timeout=45000)
            _check_https_host(page.url)
            res = page.evaluate(_BROWSER_FETCH_JS, {"u": url, "method": method, "headers": headers or {}, "body": body})
            if res.get("s") != 200:
                raise SZError(f"browser fetch failed ({res.get('s')}) for {url}")
            return res["j"]
        finally:
            browser.close()


def fetch_json(method: str, url: str, headers: dict | None = None, body: dict | None = None):
    last: Exception | None = None
    for strategy in _strategies():
        try:
            if strategy == "requests":
                return _requests_json(method, url, headers, body)
            return _browser_json(method, url, headers, body, strategy == "headless")
        except (SZError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"subzerowolf: {method} via {strategy} failed: {e}", file=sys.stderr)
    raise SZError(f"could not fetch {url}: {last}")


_config_cache: dict = {}


def storefront_config() -> tuple[str, dict]:
    """(commerce endpoint, request headers) from the site's public config.json; cached per process."""
    if not _config_cache:
        cfg = fetch_json("GET", BASE + "/config.json")["public"]["default"]
        endpoint = cfg.get("commerce-endpoint") or ""
        _check_https_host(endpoint)
        hdr = dict(cfg.get("headers", {}).get("cs") or {})
        if not hdr.get("x-api-key"):
            raise SZError("config.json carries no storefront headers (structure changed?)")
        _config_cache["v"] = (endpoint, {**hdr, "Content-Type": "application/json", "Store": "en_us"})
    return _config_cache["v"]


def graphql(query: str, variables: dict) -> dict:
    endpoint, headers = storefront_config()
    out = fetch_json("POST", endpoint, headers, {"query": query, "variables": variables})
    if out.get("errors") or "data" not in out:
        raise SZError(f"GraphQL error: {str(out.get('errors'))[:200]}")
    return out["data"]


# ---------------------------------------------------------------- parsing
def attrs_of(pv: dict) -> dict[str, str]:
    return {a["name"]: str(a.get("value") or "") for a in pv.get("attributes") or [] if a.get("name")}


def base_model(attrs: dict) -> str:
    """'DF30450/S/P/LP' -> 'DF30450/S/P' style key: the model without propane suffix, so fuel variants collapse."""
    m = (attrs.get("modelnumber") or "").strip()
    return re.sub(r"[-/]LP$", "", m)


def product_url(pv: dict) -> str:
    sku = str(pv.get("sku") or "")
    key = str(pv.get("urlKey") or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", sku) or not re.fullmatch(r"[a-z0-9-]+", key):
        raise SZError(f"unexpected sku/urlKey {sku!r}/{key!r}")
    return f"{BASE}/products/{key}/{sku.replace('__', '-', 1)}"


def sku_from_url(url: str) -> str:
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not host_in(url, PAGE_HOSTS) or len(parts) != 3 or parts[0] != "products"
            or not re.fullmatch(r"[a-z0-9-]+", parts[1]) or not re.fullmatch(r"\d+(?:-[0-9a-f]+)?", parts[2])):
        raise ValueError(f"not a supported Sub-Zero/Wolf product URL: {url}")
    return parts[2].replace("-", "__", 1)


def _price(pv: dict, attrs: dict) -> float | None:
    for raw in (attrs.get("base_price"), ((pv.get("price") or {}).get("regular") or {}).get("amount", {}).get("value")):
        try:
            if raw not in (None, "") and float(raw) > 0:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _num(pattern: str, text: str | None) -> float | None:
    return common.num(pattern, text or "", re.I)


def feature_bullets(html: str) -> list[str]:
    return [t for t in (" ".join(_html.unescape(re.sub(r"<[^>]+>", " ", li)).split())
                        for li in re.findall(r"<li[^>]*>(.*?)</li>", html or "", re.S)) if t][:12]


def main_image_url(pv: dict) -> str | None:
    imgs = [i for i in pv.get("images") or [] if isinstance(i, dict) and i.get("url")]
    imgs.sort(key=lambda i: "image" not in (i.get("roles") or []))
    for i in imgs:
        if urlparse(i["url"]).scheme == "https" and host_in(i["url"], IMAGE_HOSTS):
            return i["url"]
    return None


def is_new(attrs: dict) -> bool:
    """True when the catalogue's own `news_to_date` ('Set Product as New to Date') is today or later. Empty = not flagged."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", attrs.get("news_to_date") or "")
    try:
        return bool(m) and date(*map(int, m.groups())) >= date.today()
    except ValueError:
        return False


def candidate_from(pv: dict, brand: str, sub: str, fuel: str | None, category: str = "cooking") -> Candidate:
    a = attrs_of(pv)
    attrs: dict = {}
    if is_new(a):
        attrs["is_new"] = True
    w = _num(r"([\d.]+)", a.get("mnwidth"))
    if w:
        attrs["width_in"] = w
    if fuel:
        attrs["fuel"] = fuel
    b = _num(r"(\d+)", a.get("mnburners"))
    if b:
        attrs["burners"] = int(b)
    return Candidate(brand=brand, model_number=base_model(a) or pv["sku"], name=pv["name"], url=product_url(pv),
                     price_usd=_price(pv, a), category=category, subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, attrs=attrs, attrs_src={k: "listing" for k in attrs})


def listing_items(data: dict) -> list[dict]:
    """Orderable product views of one productSearch page (spec-library shells without a page URL are dropped)."""
    out = []
    for it in data["productSearch"]["items"]:
        pv = it["productView"]
        a = attrs_of(pv)
        if a.get("is_spec_product") == "yes" or not a.get("sitecoreurl") or a.get("is_accessory") == "yes":
            continue
        out.append(pv)
    return out


def search_products(brand_name: str, series: list[str], classify, fuel_of, limit: int, sub: str, log: str) -> list[Candidate]:
    """Walk productSearch pages for the brand/series, one candidate per base model whose classify() == sub."""
    found: dict[str, Candidate] = {}
    flt = [{"attribute": "manufacturername", "eq": brand_name}, {"attribute": "is_accessory", "eq": "no"},
           {"attribute": "is_spec_product", "eq": "no"}, {"attribute": "mnseries", "in": series}]
    page, pages = 1, 1
    while page <= min(pages, MAX_PAGES) and len(found) < limit:
        data = graphql(SEARCH_QUERY, {"f": flt, "p": page})
        pages = data["productSearch"]["page_info"]["total_pages"]
        for pv in listing_items(data):
            a = attrs_of(pv)
            if classify(a.get("mnseries", ""), pv["name"]) != sub:
                continue
            key = base_model(a) or pv["sku"]
            found.setdefault(key, candidate_from(pv, brand_name, sub, fuel_of(a.get("mnseries", ""))))
        page += 1
    print(f"{log}: {len(found)} base models kept ({page - 1} page(s))", file=sys.stderr)
    return list(found.values())[:limit]


def parse_product(url: str, pv: dict, brand: str, classify, category: str = "cooking") -> tuple[ProductRecord, list[RawSpec]]:
    a = attrs_of(pv)
    series = a.get("mnseries", "")
    model = base_model(a) or str(pv.get("sku"))
    spec_rows = [(next((x.get("label") for x in pv["attributes"] if x["name"] == k), k).strip(), v)
                 for k, v in a.items() if k not in _HIDDEN and v.strip() and not v.strip().startswith("{")]
    extra = {f"Specifications > {l}": v for l, v in spec_rows}
    extra["Series"] = series
    supply = a.get("electricalsupply", "")
    cap = _num(r"([\d.]+)", a.get("oven1overallcapacity"))
    record = ProductRecord(
        brand=brand, model_number=model, product_name=pv.get("name") or model, category=category,
        subcategory=classify(series, pv.get("name") or ""), product_url=url, price_usd=_price(pv, a), region=REGION,
        country=COUNTRY, currency=CURRENCY, finish_color=a.get("mnfinish") or a.get("mnaccessorybezels") or None,
        width_in=_num(r"([\d.]+)", a.get("overallwidth")), height_in=_num(r"([\d.]+)", a.get("overallheight")),
        depth_in=_num(r"([\d.]+)", a.get("overalldepth")), weight_lb=_num(r"([\d.]+)", a.get("specweight")),
        voltage_v=(re.match(r"\s*([\d/.\-]+)\s*V", supply) or [None, None])[1], frequency_hz=_num(r"(?:\d+/)?(\d+)\s*Hz", supply),
        capacity_total_cuft=cap, pod_features=feature_bullets(a.get("features", "")), extra_specs=extra,
        image_url=main_image_url(pv), is_new=True if is_new(a) else None)
    raw = [RawSpec(brand=brand, model_number=model, source="web", section="Specifications", key=l, value=v)
           for l, v in spec_rows]
    return record, raw


def scrape_product(url: str, brand_name: str, brand: str, classify) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    sku = sku_from_url(url)
    data = graphql(PRODUCT_QUERY, {"skus": [sku]})
    views = data.get("products") or []
    if not views:
        raise SZError(f"no product for sku {sku}")
    pv = views[0]
    if attrs_of(pv).get("manufacturername") != brand_name:
        raise ValueError(f"{url} is not a {brand_name} product")
    record, raw = parse_product(url, pv, brand, classify)
    return record, [], raw  # spec sheets/manuals are not fetched (see module docstring)
