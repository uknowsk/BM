"""Café Appliances US adapter (https://www.cafeappliances.com; brand 'Café').

Same GE Appliances BigCommerce/Searchspring platform as ge_us.py, and this module is also the shared core of
haier_us.py (a `Site` describes one storefront: hosts, Searchspring site id, category -> sub key map).
  discover(): the category pages are Searchspring widgets (site id kid17f); the JSON the widget calls is fetched
    directly (bgfilter.categories_hierarchy=<path>), exactly like ge_us. Search results carry sku, sale `price`,
    PDP path in `custom_url` and the category paths.
  scrape(): the PDP embeds the product as JSON in `window.stencilBootstrap("product", "...")` (spec table in
    custom_fields Spec_<SECTION>_<Key>, Documents_<n>_<Label> PDFs). The pure parsing is delegated to ge_us
    (imported, not modified); only the classification (own category tree) and the brand are overridden here.
Cloudflare serves a challenge page to plain `requests` (HTTP 403 'Just a moment'), so the storefront pages are
always loaded in a browser: headless first, then a visible window (FRIDGE_BROWSER_MODE=auto|headless|visible,
read on every call). Nothing is done to defeat the challenge: if the browser is not let through, the load fails.
Scope (coordinator decision): cooking sub keys only; refrigerators (Café has built_in/french_door/side_by_side/compact on
this site) and laundry are intentionally not implemented. Not supported: hoods, warming drawers, dishwashers.
"""
import html as htmllib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import catalog
import common
import ge_us as ge
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
SS_HOST = "a.searchspring.io"
PAGE_SIZE = 100
DELAY_S = 1.0
MAX_REDIRECTS = 5
BOOTSTRAP = ge.BOOTSTRAP
_ACCESSORY = re.compile(r"accessor", re.I)


class SiteError(RuntimeError):
    """Page/API did not have the expected structure (site changed or blocked)."""


class SiteNotFound(SiteError):
    """HTTP 404/410: retrying with a browser cannot help."""


@dataclass(frozen=True)
class Site:
    brand: str
    base: str
    hosts: tuple[str, ...]
    ss_site: str
    sub_sources: dict = field(compare=False)
    needs_browser: bool = True  # Cloudflare challenge on plain requests
    root: str = ""  # first category segment (a PDP may list a path without it)


def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_final_url(site: Site, url: str) -> None:
    if urlparse(url).scheme != "https" or not _host_in(url, site.hosts):
        raise SiteError(f"unexpected url after navigation/request: {url[:120]}")


# ---------------------------------------------------------------- predicates (item = Searchspring-like dict)
_R = "Cafe Appliances>Major Appliances>"
_RNG = _R + "Cooking>Ranges>"
_MW = _R + "Cooking>Microwaves>"
_CT = _R + "Cooking>Cooktops>"
_WO = _R + "Cooking>Wall Ovens>"

# sub key -> (major, [(category path, predicate on the item or None)]). Dict order = classification priority
# (first match wins), used by discover() AND scrape() so a product has exactly one sub key.
CAFE_SUB_SOURCES: dict[str, tuple[str, list]] = {
    # sco = Advantium speed ovens (wall oven / combination), before electric_oven and microwave
    "sco": ("cooking", [(_WO + "Advantium Ovens", None)]),
    "microwave": ("cooking", [(_MW + "Countertop Microwave Ovens", None), (_MW + "Built In Microwave Ovens", None)]),
    "otr": ("cooking", [(_MW + "Over-the-Range Microwave Ovens", None)]),
    "induction": ("cooking", [(_RNG + "Induction Ranges", None), (_CT + "Induction Cooktops", None)]),
    "gas_oven": ("cooking", [(_RNG + "Gas Ranges", None), (_RNG + "Dual Fuel Ranges", None),
                             (_RNG + "Double Oven Ranges", ge._is_gas), (_R + "Cooking>Commercial-Style Ranges", ge._is_gas)]),
    "gas_cooktop": ("cooking", [(_CT + "Gas Cooktops", None)]),
    "radiant": ("cooking", [(_RNG + "Electric Ranges", ge._not_induction), (_RNG + "Double Oven Ranges", ge._not_gas),
                            (_CT + "Electric Cooktops", ge._not_induction)]),
    "electric_oven": ("cooking", [(_WO + n, None) for n in ("Single Wall Ovens", "Double Wall Ovens", "French Door Wall Oven")]),
}

CAFE = Site(brand="Café", base="https://www.cafeappliances.com", hosts=("cafeappliances.com",),
            ss_site="kid17f", sub_sources=CAFE_SUB_SOURCES, root="Cafe Appliances")
BRAND = CAFE.brand
SUPPORTED_SUBCATEGORIES = set(CAFE_SUB_SOURCES)


# ---------------------------------------------------------------- classification
def _item_categories(item: dict) -> set[str] | None:
    cats = {htmllib.unescape(str(c)).replace("/", ">") for c in item.get("categories_hierarchy") or []}
    return cats or None


def _po_categories(po: dict) -> set[str]:
    return {htmllib.unescape(c).replace("/", ">") for c in po.get("category") or []}


def classify(site: Site, cats: set[str], item: dict) -> str | None:
    """First sub_sources entry whose category path + predicate accept the product (shared by discover and scrape)."""
    cats = {_strip_root(site, c) for c in cats}
    for sub, (_, sources) in site.sub_sources.items():
        for path, pred in sources:
            path = _strip_root(site, path)
            if any(c == path or c.startswith(path + ">") for c in cats) and (pred is None or pred(item)):
                return sub
    return None


def _strip_root(site: Site, path: str) -> str:
    """Drop the brand root segment: some PDPs list 'Major Appliances>...' without 'Cafe Appliances>'."""
    return path[len(site.root) + 1:] if site.root and path.startswith(site.root + ">") else path


# ---------------------------------------------------------------- discover
def _attrs(r: dict) -> dict:
    out = {}
    color = ge._clean(r.get("spec_appearance_color_appearance") or "")
    if color:
        out["finish"] = color
    wifi = ge._yes_no(ge._clean(r.get("spec_features_wifi_connect") or "") or None)
    if wifi is not None:
        out["wifi"] = wifi
    return out


def parse_search(site: Site, data: dict, sub: str) -> list[tuple[Candidate, dict]]:
    """Searchspring response -> [(Candidate, raw result item)] for one sub key; off-domain urls dropped."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or "pagination" not in data:
        raise SiteError("Searchspring response has no results/pagination (structure changed?)")
    major = site.sub_sources[sub][0]
    out = []
    for r in results:
        sku, path = (r.get("sku") or "").strip().upper(), r.get("custom_url") or ""
        if not sku or not path or _ACCESSORY.search(str(r.get("item_commercial_category2") or "")):
            continue
        url = urljoin(site.base + "/", path)
        if not _host_in(url, site.hosts) or urlparse(url).scheme != "https":
            print(f"dropped off-domain candidate {sku}: {url[:100]}", file=sys.stderr)
            continue
        try:
            price = float(r.get("price")) if r.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        sig = ge.signals(r.get("rating"), r.get("ratingCount"), r.get("product_first_distribution_date"))
        out.append((Candidate(brand=site.brand, model_number=sku, name=htmllib.unescape(str(r.get("name") or sku)),
                              url=url, price_usd=price or None, category=major, subcategory=sub,
                              region=REGION, country=COUNTRY, currency=CURRENCY, attrs={**_attrs(r), **sig},
                              attrs_src={k: "listing" for k in sig}), r))
    return out


def _search_page(site: Site, path: str, page_no: int) -> dict:
    params = {"siteId": site.ss_site, "resultsFormat": "native", "resultsPerPage": PAGE_SIZE, "page": page_no,
              "bgfilter.categories_hierarchy": path, "bgfilter.is_part": "false",
              "bgfilter.is_obsolete": "false", "bgfilter.availability": "available"}
    url = f"https://{site.ss_site}.{SS_HOST}/api/search/search.json"
    r = requests.get(url, params=params, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise SiteError(f"unexpected Searchspring redirect (HTTP {r.status_code})")
    if not _host_in(r.url, (SS_HOST,)) or urlparse(r.url).scheme != "https":
        raise SiteError(f"unexpected Searchspring url {r.url[:120]}")
    r.raise_for_status()
    return r.json()


def discover_site(site: Site, subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in site.sub_sources:
        raise ValueError(f"{site.brand} US adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    first, other = True, 0
    for path, _pred in site.sub_sources[subcategory][1]:
        page_no, total_pages = 1, 1
        while len(found) < limit and page_no <= total_pages:
            if not first:
                time.sleep(DELAY_S)
            first = False
            data = _search_page(site, path, page_no)
            total_pages = int((data.get("pagination") or {}).get("totalPages") or 1)
            for cand, item in parse_search(site, data, subcategory):
                cats = _item_categories(item)
                if cats is not None and classify(site, cats, item) != subcategory:
                    other += 1  # belongs to another sub key (listed there by the same rule) or to none
                    continue
                found.setdefault(cand.model_number, cand)
            page_no += 1
        if len(found) >= limit:
            break
    if other:
        print(f"{site.brand}: discover({subcategory}): skipped {other} listed product(s) classified elsewhere", file=sys.stderr)
    return list(found.values())[:limit]


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return discover_site(CAFE, subcategory, limit)


# ---------------------------------------------------------------- page loading (browser; Cloudflare in front)
def _strategies(site: Site) -> list[str]:
    """FRIDGE_BROWSER_MODE is read on every call. auto = (requests if the site allows it), headless, visible."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return ["headless"]
    if mode == "visible":
        return ["visible"]
    return ["headless", "visible"] if site.needs_browser else ["requests", "headless", "visible"]


def _via_requests(site: Site, url: str) -> str:
    cur = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_final_url(site, cur)
        r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise SiteError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise SiteNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if common.looks_blocked(r.status_code, body) or r.status_code != 200:
        raise SiteError(f"blocked or bad status ({r.status_code})")
    if BOOTSTRAP not in body:
        raise SiteError("no stencilBootstrap product data in page")
    return body


def _via_browser(site: Site, url: str, headless: bool) -> str:
    probe = "() => document.documentElement.outerHTML.includes('window.stencilBootstrap(\"product\"')"
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_final_url(site, page.url)
            ge._consent(page)  # decline first
            try:
                page.wait_for_function(probe, timeout=25000)
            except PlaywrightTimeoutError:
                ge._consent(page, accept=True)  # a banner may be blocking rendering; retry once
                page.wait_for_function(probe, timeout=15000)
            _check_final_url(site, page.url)
            status = resp.status if resp else None
            if status in (404, 410):
                raise SiteNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise SiteError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_pdp_html(site: Site, url: str) -> str:
    _check_final_url(site, url)
    last: Exception | None = None
    for strategy in _strategies(site):
        try:
            return _via_requests(site, url) if strategy == "requests" else _via_browser(site, url, strategy == "headless")
        except SiteNotFound:
            raise
        except (SiteError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"{site.brand} load via {strategy} failed: {e}", file=sys.stderr)
    raise SiteError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- parsing (delegated to ge_us)
def _ge_category_for(sub: str) -> list[str]:
    """A GE-style category path for `sub` so ge.parse_product can run (its own inference is overridden afterwards)."""
    key = sub if sub in ge.SUB_SOURCES else "french_door"
    sources = ge.SUB_SOURCES[key][1]
    path = next((p for p, pred in sources if pred is None), sources[0][0])
    return [path.replace(">", "/")]


def parse_product(site: Site, url: str, po: dict) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    """Pure parse of productObj -> (record, RawSpec rows, doc links) with this site's brand and classification."""
    rows = ge.spec_rows(po)
    spec: dict[str, str] = {}
    for _, k, v in rows:
        spec.setdefault(k, v)
    fields = {f["name"]: ge._clean(f.get("value")) for f in po["custom_fields"] if f.get("name")}
    title = ge._clean((po.get("title") or "").split("|^|")[0])
    item = {"name": title, "item_commercial_category3": fields.get("item_commercial_category3", ""),
            "spec_features_configuration": spec.get("Configuration", ""),
            "spec_features_cooktop_burner_type": spec.get("Cooktop Burner Type", ""),
            "spec_features_fuel_type": spec.get("Fuel Type", ""),
            "spec_features_product_type": spec.get("Product Type", "")}
    sub = classify(site, _po_categories(po), item)
    if not sub:
        raise ValueError(f"not a supported {site.brand} product (categories: {sorted(_po_categories(po))[:3]})")
    major = site.sub_sources[sub][0]
    record, raw, links = ge.parse_product(url, dict(po, category=_ge_category_for(sub)))
    record.brand, record.category, record.subcategory = site.brand, major, sub
    record.region, record.country, record.currency = REGION, COUNTRY, CURRENCY
    if record.wifi_evidence:
        record.wifi_evidence = record.wifi_evidence.replace("GE spec table", f"{site.brand} spec table")
    raw = [r.model_copy(update={"brand": site.brand}) for r in raw]
    return record, raw, links


def model_from_url(site: Site, url: str) -> str:
    u = urlparse(url)
    m = re.search(r"/appliance/(?:.*-)?([A-Za-z0-9]{4,})/?$", u.path)
    if u.scheme != "https" or not _host_in(url, site.hosts) or not m:
        raise ValueError(f"not a {site.brand} US product URL: {url}")
    return m.group(1).upper()


def scrape_site(site: Site, url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    url_model = model_from_url(site, url)
    po = ge.extract_product(fetch_pdp_html(site, url))
    product, raw, links = parse_product(site, url, po)
    if product.model_number != url_model:
        print(f"warning: URL model {url_model} != page model {product.model_number}", file=sys.stderr)
    docs = []
    for dtype, link in links:
        if not ge._pdf_url_ok(link):
            print(f"skipped {dtype}: PDF url not https on an allowed host ({link[:100]})", file=sys.stderr)
            continue
        time.sleep(DELAY_S)
        d = common.download_pdf(site.brand, ge._safe_name(product.model_number), dtype, link)
        if d:
            docs.append(d)
    return product, docs, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return scrape_site(CAFE, url)
