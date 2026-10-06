"""Bertazzoni US adapter (catalog.py contract): cooking only (ranges, cooktops/rangetops, ovens, microwaves).

Data source: https://us.bertazzoni.com (server-rendered, plain requests work). robots.txt disallows /downloads/ (the spec
sheet / manual PDFs live there) -> scrape() returns NO documents. No price is published (price_usd None).
  * category pages /products/ranges, /products/cooktops, /products/convection-ovens, /products/speciality-ovens:
    `<a class="card-body" href=".."><div class="abstract">MODEL</div><div class="tit-h3">name</div>`.
  * product page: schema.org Product JSON-LD (name, mpn, image), spec groups `<span class="group">` + `<ul>` of
    `<span class="label">` / `<span class="value"><span class="feature">`.
Fetching: plain requests, then headless, then visible (FRIDGE_BROWSER_MODE = auto | headless | visible); requests >= 1 s apart.
"""
import html as _html
import json
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

BRAND = "Bertazzoni"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://us.bertazzoni.com"
PAGE_HOSTS = ("us.bertazzoni.com",)
IMAGE_HOSTS = ("us.bertazzoni.com",)
DELAY_S = 1.5
MAX_REDIRECTS = 5

SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("speciality-ovens",),
    "sco": ("convection-ovens", "speciality-ovens"),
    "gas_oven": ("ranges",),
    "gas_cooktop": ("cooktops",),
    "electric_oven": ("convection-ovens", "speciality-ovens"),
    "induction": ("cooktops", "ranges"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)  # no OTR microwave in the US range
CATEGORIES = ("ranges", "cooktops", "convection-ovens", "speciality-ovens", "specialty-ovens", "built-in-ovens")
_URL_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,200}$")


# ---------------------------------------------------------------- fetching
class BertPageError(RuntimeError):
    pass


class BertNotFound(BertPageError):
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
        r = requests.get(cur, headers={"User-Agent": common.UA}, timeout=40, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise BertPageError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise BertNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise BertPageError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise BertPageError(f"expected marker {probe!r} missing (structure changed?)")
    return body


def _consent(page, accept: bool = False) -> None:
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
                _consent(page, accept=True)
                page.wait_for_function(check, arg=probe, timeout=15000)
            status = resp.status if resp else None
            if status in (404, 410):
                raise BertNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise BertPageError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except BertNotFound:
            raise
        except (BertPageError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"bertazzoni_us: load via {strategy} failed: {e}", file=sys.stderr)
    raise BertPageError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def classify(name: str, category: str) -> str | None:
    """Sub key from the product name and the URL category (ranges / cooktops / *-ovens); one rule for discover() and
    scrape(). Ranges: induction -> induction, gas/dual fuel -> gas_oven, electric -> radiant. Cooktops/rangetops: induction
    -> induction, gas -> gas_cooktop, other electric -> radiant. Ovens: speed oven -> sco, microwave -> microwave, warming
    drawers/accessories -> None, other (steam, convection) -> electric_oven."""
    n = (name or "").lower()
    if category == "ranges":
        if "induction" in n:
            return "induction"
        if "gas" in n or "dual fuel" in n:
            return "gas_oven"
        return "radiant" if "electric" in n else None
    if category == "cooktops":
        if "induction" in n:
            return "induction"
        if "gas" in n:
            return "gas_cooktop"
        return "radiant" if re.search(r"electric|ceramic|radiant", n) else None
    if category in ("convection-ovens", "speciality-ovens", "specialty-ovens", "built-in-ovens"):
        if "speed oven" in n:
            return "sco"
        if "microwave" in n:
            return "microwave"
        if "warming" in n or "coffee" in n:
            return None
        return "electric_oven"
    return None


def category_of(path: str) -> str:
    parts = [x for x in path.split("/") if x]
    return parts[2] if len(parts) >= 4 and parts[0] == "products" else ""


def _card_attrs(name: str) -> dict:
    n = name.lower()
    attrs: dict = {}
    m = re.search(r"\b(\d{2})\s*(?:\"|''|”|inch|in\b)?", name)
    if m and 12 <= int(m.group(1)) <= 60:
        attrs["width_in"] = float(m.group(1))
    if "dual fuel" in n:
        attrs["fuel"] = "dual_fuel"
    elif "induction" in n:
        attrs["fuel"] = "induction"
    elif "gas" in n:
        attrs["fuel"] = "gas"
    elif "electric" in n:
        attrs["fuel"] = "electric"
    m = re.search(r"(\d)\s*(?:brass\s+)?burner", n)
    if m:
        attrs["burners"] = int(m.group(1))
    return attrs


# ---------------------------------------------------------------- discover
_CARD = re.compile(r'<a class="card-body" href="([^"]+)">\s*<div class="abstract">(.*?)</div>\s*<div class="tit-h3">(.*?)</div>',
                   re.S)


def _clean(s: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def parse_listing(html: str) -> list[dict]:
    """[{model, path, name}] of one category page; New badges are stripped from the name."""
    out = []
    for m in _CARD.finditer(html):
        path, model, name = _html.unescape(m.group(1)), _clean(m.group(2)), re.sub(r"^New\s+", "", _clean(m.group(3)))
        if path.startswith("/products/") and model and name:
            out.append({"model": model, "path": path, "name": name, "new": 'class="is-new"' in m.group(3)})
    return out


def _candidate(card: dict, sub: str) -> Candidate:
    attrs = _card_attrs(card["name"])
    if card.get("new"):  # the site's own "New" badge on the listing card
        attrs["is_new"] = True
    return Candidate(brand=BRAND, model_number=card["model"], name=card["name"], url=BASE + card["path"],
                     price_usd=None, category="cooking", subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, attrs=attrs, attrs_src={k: "listing" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Bertazzoni US")
    found: dict[str, Candidate] = {}
    for page in SUB_SOURCES[subcategory]:
        try:
            cards = parse_listing(fetch_html(f"{BASE}/products/{page}", "card-body"))
        except BertNotFound:
            continue
        for c in cards:
            if classify(c["name"], category_of(c["path"])) == subcategory:
                found.setdefault(c["model"], _candidate(c, subcategory))
        print(f"bertazzoni_us: discover({subcategory}) {page}: {len(cards)} listed, {len(found)} kept", file=sys.stderr)
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def parse_url(url: str) -> tuple[str, str]:
    """(category, slug) of https://us.bertazzoni.com/products/<series>/<category>/<slug>."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not _host_in(url, PAGE_HOSTS) or u.query or len(parts) != 4 or parts[0] != "products"
            or parts[2] not in CATEGORIES or not _URL_SLUG.match(parts[3]) or not _URL_SLUG.match(parts[1])):
        raise ValueError(f"not a supported Bertazzoni US product URL: {url}")
    return parts[2], parts[3]


def product_ld(html: str) -> dict:
    for blob in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(blob)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
    raise BertPageError("no Product JSON-LD in product page (structure changed?)")


def spec_rows(html: str) -> list[tuple[str, str, str]]:
    """[(group, label, value)] from every spec group; multi-feature values are joined with ' | '."""
    rows = []
    for gm in re.finditer(r'<span class="group">(.*?)</span>\s*<ul[^>]*>(.*?)</ul>', html, re.S):
        group = _clean(gm.group(1))
        for lab, val in re.findall(r'<span class="label">(.*?)</span>\s*<span class="value">(.*?)</span>\s*</li>', gm.group(2), re.S):
            feats = [_clean(f) for f in re.findall(r'<span class="feature">(.*?)</span>', val, re.S)]
            value = " | ".join(f for f in feats if f)
            if _clean(lab) and value:
                rows.append((group, _clean(lab), value))
    return rows


def main_image_url(ld: dict) -> str | None:
    img = ld.get("image")
    for raw in img if isinstance(img, list) else [img]:
        if isinstance(raw, str) and raw.strip():
            url = urljoin(BASE + "/", raw.strip())
            return url if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS) else None
    return None


def _num(pattern: str, text: str | None) -> float | None:
    return common.num(pattern, text or "", re.I)


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec]]:
    category, _slug = parse_url(url)
    ld = product_ld(html)
    rows = spec_rows(html)
    if not rows:
        raise BertPageError("no spec groups found (structure changed?)")
    model = str(ld.get("mpn") or "").strip()
    if not model:
        raise BertPageError("Product JSON-LD has no mpn")
    name = _clean(ld.get("name") or "")
    flat = {l.lower(): v for _g, l, v in rows}
    sub = classify(name, category)
    size = _num(r"([\d.]+)", flat.get("size")) or _card_attrs(name).get("width_in")
    volt = flat.get("electrical supply", "")
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name or model, category="cooking", subcategory=sub,
        product_url=url, price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY, width_in=size,
        capacity_total_cuft=_num(r"(\d+(?:\.\d+)?)\s*(?:cu\.?\s*)?ft", flat.get("oven volume")),
        voltage_v=(re.match(r"\s*(\d+(?:[/-]\d+)?)\s*V", volt) or [None, None])[1],
        frequency_hz=_num(r"(\d+)\s*Hz", volt),
        extra_specs={f"{g} > {l}": v for g, l, v in rows}, image_url=main_image_url(ld))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=g, key=l, value=v) for g, l, v in rows]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    parse_url(url)
    html = fetch_html(url, "application/ld+json")
    record, raw = parse_product(url, html)
    return record, [], raw  # PDFs are under /downloads/, disallowed by robots.txt
