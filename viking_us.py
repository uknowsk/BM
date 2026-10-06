"""Viking US adapter (catalog.py contract): cooking only (ranges, rangetops, cooktops, wall ovens, microwaves).

Data source: https://vikingrange.com (server-rendered, plain requests work). robots.txt: Crawl-delay 10 -> DELAY_S = 10;
only /cdn-cgi/, /backend/, /modules/ are disallowed. No login. AI-training bots are barred and the file carries
`ai-train=no`; this adapter only reads product facts on demand for the user's own comparison (search=yes).
  * category pages /products/cook/<section>: `products__item` cards (series, product_name link, model) -> /model/<M>/sku/<SKU>.
  * product page: `<script id="page-json">` (profile: title, model, sku, series, pricing.list/umrp), HTML sections
    `products__profile__section` (specifications flex items, highlights, documents), photoswipe JSON (middleby-cdn.com).
Price: the SKU's `pricing.list` (list price), not the UMRP. Fetching: plain requests, then headless, then visible
(FRIDGE_BROWSER_MODE = auto | headless | visible).
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

BRAND = "Viking"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"
BASE = "https://vikingrange.com"
PAGE_HOSTS = ("vikingrange.com",)
IMAGE_HOSTS = ("middleby-cdn.com", "vikingrange.com")
DOC_HOSTS = ("middleby-cdn.com", "vikingrange.com")
DELAY_S = 10.0  # robots.txt Crawl-delay
REQUEST_DELAY_S = DELAY_S  # published to the UI (catalog.request_delay) so users are told searches are slow
MAX_REDIRECTS = 5

SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("microwaves", "wall-ovens"),
    "sco": ("wall-ovens", "microwaves"),
    "otr": ("microwaves",),
    "gas_oven": ("ranges",),
    "gas_cooktop": ("rangetops", "cooktops"),
    "electric_oven": ("wall-ovens",),
    "induction": ("cooktops", "rangetops", "ranges"),
    "radiant": ("cooktops", "rangetops", "ranges"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$")
DOC_TYPES = (("specifications sheet", "SpecSheet"), ("installation instructions", "Installation"),
             ("use & care manual", "Manual"), ("warranty", "Warranty"))
SECTIONS = ("ranges", "rangetops", "cooktops", "wall-ovens", "microwaves")


# ---------------------------------------------------------------- fetching
class VikingPageError(RuntimeError):
    pass


class VikingNotFound(VikingPageError):
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
        raise VikingPageError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise VikingNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise VikingPageError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise VikingPageError(f"expected marker {probe!r} missing (structure changed?)")
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
                raise VikingNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise VikingPageError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except VikingNotFound:
            raise
        except (VikingPageError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"viking_us: load via {strategy} failed: {e}", file=sys.stderr)
    raise VikingPageError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def classify(name: str, model: str, section: str) -> str | None:
    """Sub key from the product name, model code and URL section (ranges / rangetops / cooktops / wall-ovens /
    microwaves); one rule for discover() and scrape(). Speed/combi-speed ovens -> sco (even when listed under wall ovens),
    microwave hoods -> otr, other microwaves -> microwave, wall ovens -> electric_oven; ranges: gas/dual fuel ->
    gas_oven, induction -> induction, electric -> radiant; rangetops/cooktops: gas -> gas_cooktop, induction ->
    induction, electric -> radiant. Warmers, downdrafts without a fuel word, ventilation, refrigeration -> None."""
    n = (name or "").lower()
    if "discontinued" in n or "warmer" in n or "drawer" in n and "micro" not in n:
        return None
    if re.search(r"speed", n):
        return "sco"
    if section == "microwaves":
        return "otr" if "hood" in n else "microwave"
    if section == "wall-ovens":
        return "electric_oven"
    if section not in ("ranges", "rangetops", "cooktops"):
        return None
    induction = "induction" in n or (("downdraft" in n) and re.match(r"M?VI", model or "") is not None)
    if induction:
        return "induction"
    if section == "ranges" and re.search(r"gas|dual fuel", n):
        return "gas_oven"
    if section in ("rangetops", "cooktops") and "gas" in n:
        return "gas_cooktop"
    if "electric" in n:
        return "radiant"
    return None


def section_of(path: str) -> str:
    parts = [x for x in path.split("/") if x]
    return parts[2] if len(parts) > 2 and parts[:2] == ["products", "cook"] else ""


def _card_attrs(name: str) -> dict:
    n = name.lower()
    attrs: dict = {}
    m = re.search(r"(\d{2})\"\s*W", name)
    if m:
        attrs["width_in"] = float(m.group(1))
    if "dual fuel" in n:
        attrs["fuel"] = "dual_fuel"
    elif "induction" in n:
        attrs["fuel"] = "induction"
    elif "gas" in n:
        attrs["fuel"] = "gas"
    elif "electric" in n:
        attrs["fuel"] = "electric"
    return attrs


# ---------------------------------------------------------------- discover
_CARD = re.compile(
    r'class="products__item"\s+data-group-id="([^"]+)".*?data-attribute="series">\s*<p[^>]*>\s*<em>(.*?)</em>.*?'
    r'data-attribute="product_name">\s*<p[^>]*>\s*<a href="([^"]+)">(.*?)</a>', re.S)


def parse_listing(html: str) -> list[dict]:
    """[{model, series, path, name}] of one category page (one card per model; first SKU link)."""
    out = []
    for m in _CARD.finditer(html):
        model, series, path, name = m.group(1), _clean(m.group(2)), _html.unescape(m.group(3)), _clean(m.group(4))
        if _MODEL.match(model) and path.startswith("/products/cook/"):
            out.append({"model": model, "series": series, "path": path, "name": name,
                        "new": any(re.match(r"new\b", _clean(t), re.I) for t in _BADGE.findall(m.group(0)))})
    return out


_BADGE = re.compile(r'data-badge="[^"]*">([^<]*)')


def _candidate(card: dict, sub: str) -> Candidate:
    attrs = _card_attrs(card["name"])
    if card.get("new"):  # the site's own NEW badge on the listing card (Viking shows only BESTSELLER today)
        attrs["is_new"] = True
    return Candidate(brand=BRAND, model_number=card["model"], name=card["name"], url=BASE + card["path"],
                     price_usd=None, category="cooking", subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, attrs=attrs, attrs_src={k: "listing" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Viking US")
    found: dict[str, Candidate] = {}
    for section in SUB_SOURCES[subcategory]:
        try:
            cards = parse_listing(fetch_html(f"{BASE}/products/cook/{section}", "products__item"))
        except VikingNotFound:
            continue
        for c in cards:
            if classify(c["name"], c["model"], section_of(c["path"])) == subcategory:
                found.setdefault(c["model"], _candidate(c, subcategory))
        print(f"viking_us: discover({subcategory}) {section}: {len(cards)} listed, {len(found)} kept", file=sys.stderr)
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def parse_url(url: str) -> tuple[str, str, str]:
    """(section, model, sku) of https://vikingrange.com/products/cook/<section>/model/<MODEL>/sku/<SKU>."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not _host_in(url, PAGE_HOSTS) or len(parts) != 7 or parts[:2] != ["products", "cook"]
            or parts[2] not in SECTIONS or parts[3] != "model" or parts[5] != "sku"
            or not _MODEL.match(parts[4]) or not _MODEL.match(parts[6])):
        raise ValueError(f"not a supported Viking product URL: {url}")
    return parts[2], parts[4], parts[6]


def _clean(s: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def page_profile(html: str) -> dict:
    m = re.search(r'<script id="page-json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise VikingPageError("no page-json in product page (structure changed?)")
    try:
        prof = json.loads(m.group(1)).get("profile")
    except ValueError as e:
        raise VikingPageError(f"page-json unreadable: {e}") from e
    if not isinstance(prof, dict) or not prof.get("model"):
        raise VikingPageError("page-json has no product profile")
    return prof


def _section_block(html: str, key: str) -> str:
    m = re.search(r'data-key="%s"' % re.escape(key), html)
    if not m:
        return ""
    nxt = html.find('class="products__profile__section"', m.end())
    return html[m.end(): nxt if nxt > 0 else len(html)]


def spec_rows(html: str) -> list[tuple[str, str, str]]:
    """[('Specifications', label, value)] from the specification flex items."""
    block = _section_block(html, "specifications")
    rows = []
    for t, c in re.findall(r'flex__item__title">\s*<p[^>]*>(.*?)</p>.*?flex__item__content">(.*?)</div>', block, re.S):
        label, value = _clean(t), _clean(c)
        if label and value:
            rows.append(("Specifications", label, value))
    return rows


def highlights(html: str) -> list[str]:
    block = _section_block(html, "highlights")
    heads = re.findall(r'<p class="[^"]*heading[^"]*"[^>]*>(.*?)</p>', block, re.S)
    return [h for h in (_clean(x) for x in heads) if h and h.lower() != "highlights"]


def main_image_url(html: str) -> str | None:
    m = re.search(r'data-photoswipe-config="photos">(\[.*?\])</script>', html, re.S)
    if m:
        try:
            for item in json.loads(m.group(1)):
                url = item.get("src") or ""
                if urlparse(url).scheme == "https" and _host_in(url, IMAGE_HOSTS):
                    return url
        except ValueError:
            pass
    return None


def doc_links(html: str) -> list[tuple[str, str]]:
    block = _section_block(html, "documents")
    out, used = [], set()
    for href, text in re.findall(r'<a [^>]*href="([^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>', block, re.S):
        label = _clean(text).lower()
        dtype = next((t for key, t in DOC_TYPES if key in label), None)
        url = _html.unescape(href)
        if dtype and dtype not in used and urlparse(url).scheme == "https" and _host_in(url, DOC_HOSTS):
            used.add(dtype)
            out.append((dtype, url))
    return out


def _num(pattern: str, text: str | None) -> float | None:
    return common.num(pattern, text or "", re.I)


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec]]:
    section, model, sku = parse_url(url)
    prof = page_profile(html)
    rows = spec_rows(html)
    if not rows:
        raise VikingPageError(f"no specifications for {model} (structure changed?)")
    title = _clean(prof.get("title") or "")
    sub = classify(title, model, section)
    spec = {l.lower(): v for _s, l, v in rows}
    dims = re.findall(r"(\d+(?:\.\d+)?)\s*\"", spec.get("product dimensions", ""))
    price = (prof.get("pricing") or {}).get("list") or {}
    try:
        price_usd = float(price.get("value")) if price.get("value") else None
    except (TypeError, ValueError):
        price_usd = None
    record = ProductRecord(
        brand=BRAND, model_number=sku, product_name=f"{prof.get('series') or ''} {title}".strip() or sku,
        category="cooking", subcategory=sub, product_url=url, price_usd=price_usd, region=REGION, country=COUNTRY,
        currency=CURRENCY,
        width_in=float(dims[0]) if len(dims) == 3 else None, depth_in=float(dims[1]) if len(dims) == 3 else None,
        height_in=float(dims[2]) if len(dims) == 3 else None,
        weight_lb=_num(r"([\d.,]+)\s*lb", spec.get("weight")),
        pod_features=highlights(html),
        extra_specs={f"{s} > {l}": v for s, l, v in rows} | {"Model": model, "SKU": sku, "Series": prof.get("series") or ""},
        image_url=main_image_url(html))
    badges = (prof.get("badges") or {}).get("profile") if isinstance(prof.get("badges"), dict) else None
    if isinstance(badges, dict) and any(re.match(r"new\b", str(t), re.I) for t in badges.values()):
        record.is_new = True
    raw = [RawSpec(brand=BRAND, model_number=sku, source="web", section=s, key=l, value=v) for s, l, v in rows]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    parse_url(url)
    html = fetch_html(url, "products__profile")
    record, raw = parse_product(url, html)
    docs: list[DocumentRecord] = []
    for dtype, durl in doc_links(html):
        rec = common.download_pdf(BRAND, record.model_number, dtype, durl)
        if rec:
            docs.append(rec)
    return record, docs, raw
