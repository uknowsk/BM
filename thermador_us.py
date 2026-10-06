"""Thermador US adapter (catalog.py contract, cooking only): wall ovens / speed ovens / microwaves, ranges, cooktops.

It also holds the small BSH-platform core that gaggenau_us / gaggenau_de / siemens_de import (Site, browser session,
flight-payload helpers, listing pager, document picker): thermador.com, gaggenau.com and siemens-home.bsh-group.com
run the same Next.js app as bosch-home.com (see bosch_us.py). Pages embed their data as flight chunks
(`self.__next_f.push([1,"<json string>"])`):
  * category page `<base>/mkt-category/<path>[?pageNumber=N]` (12 items/page, SSR): `"productList":{"items":[...],
    "total":N,"page":P}`; items carry productCode, urlPath (`/mkt-product/<path>/<MODEL>`), productName parts and
    `price` {kind RECOMMENDED_RETAIL_PRICE | SHOP_PRICE, amount, currency}.
  * product page `<base>/mkt-product/<path>/<MODEL>`: `"specifications":[{"name","key","specifications":[rows]}]`,
    `"pricing"`, `"technicalDocuments"`, `"productJsonLd"`, `"title":{"valueClass","headline"}`.
Robots.txt (checked 2026-10-06) only disallows /manual/ (interactive manuals), */search/*, */comparison/*, graphql and
ajax paths; none of those is requested. thermador.com/us (home page, sitemap.xml) answers a maintenance 503 page, the
Next.js category/product pages answer normally. Prices are the recommended retail price (list), never promotions.
HTML is fetched with fetch() from inside a real browser page (same-origin); PDFs go through common.download_pdf.
"""
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Thermador"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
DELAY_S = 1.0
MAX_PAGES = 8


@dataclass(frozen=True)
class Site:
    """One BSH-platform storefront (see module docstring)."""
    brand: str
    tag: str  # log prefix
    base: str  # https://host/<country>/<lang>, no trailing slash
    page_hosts: tuple
    pdf_hosts: tuple
    image_hosts: tuple
    decline: str  # cookie-banner button labels: decline first, accept only when the dialog blocks
    accept: str


SITE = Site(BRAND, "thermador_us", "https://www.thermador.com/us/en", ("thermador.com",),
            ("thermador.com", "bsh-group.com"), ("bsh-group.com", "thermador.com"), "Decline all", "Accept all")

# sub key -> (major key, category pages (under /mkt-category/) whose listings make up the sub group).
# Cooking only (coordinator decision): refrigerators/washers are not implemented; radiant is not sold.
# Assumptions: sco = speed ovens + "Combination" wall ovens (oven + microwave); electric_oven = the other single/double/triple/steam
# wall ovens; gas_oven = gas and dual-fuel ranges; gas_cooktop = gas cooktops + rangetops; induction = induction
# cooktops + induction ranges; microwave = built-in + MicroDrawer; otr = over-the-range.
_OVENS = ("ovens/wall-ovens", "ovens/double-ovens", "ovens/triple-ovens")
SUB_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "microwave": ("cooking", ("ovens/ovensmicrowaves",)),
    "otr": ("cooking", ("ovens/ovensmicrowaves",)),
    "sco": ("cooking", ("ovens/speed-ovens",) + _OVENS),
    "electric_oven": ("cooking", _OVENS + ("ovens/steam-ovens",)),
    "gas_oven": ("cooking", ("ranges/30-ranges", "ranges/36-ranges", "ranges/48-ranges", "ranges/60-ranges")),
    "gas_cooktop": ("cooking", ("cooktops-rangetops/rangetops", "cooktops-rangetops/gas-cooktops")),
    "induction": ("cooking", ("cooktops-rangetops/induction-cooktops", "ranges/30-ranges", "ranges/36-ranges",
                              "ranges/48-ranges", "ranges/60-ranges")),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
ROOT_MAJOR = {"ranges": "cooking", "ovens": "cooking", "cooktops-rangetops": "cooking"}

DOC_TYPES = {"energy-label": "EnergyGuide", "product-specification": "SpecSheet", "user-manuals": "Manual",
             "installation-instruction": "Installation"}  # 'user-interactive-manuals' (/manual/) is disallowed by robots
_CODE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_NOT_A_PRODUCT = re.compile(r"accessor|cleaning|spare|/knobs?/", re.I)


# ---------------------------------------------------------------- browser (shared core)

def modes() -> list[bool]:
    """Headless flags to try, in order. FRIDGE_BROWSER_MODE (auto | headless | visible) is read on every call."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    return [True, False]


def host_in(url: str, suffixes: tuple) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def check_url(site: Site, url: str) -> None:
    if urlparse(url).scheme != "https" or not host_in(url, site.page_hosts):
        raise ValueError(f"unexpected host: {url[:120]}")


def _consent_visible(page, site: Site) -> bool:
    try:
        return page.get_by_role("button", name=site.accept).first.is_visible(timeout=500)
    except PlaywrightError:
        return False


def dismiss_consent(page, site: Site) -> None:
    """Decline non-essential cookies first; accept only if the dialog is still there afterwards."""
    for label in (site.decline, site.accept):
        if label == site.accept and not _consent_visible(page, site):
            return
        try:
            page.get_by_role("button", name=label).first.click(timeout=2500)
            page.wait_for_timeout(300)
        except PlaywrightTimeoutError:
            if label == site.decline:
                return  # no banner


class Session:
    """Browser page on the site; fetches same-origin text with the page's cookies."""

    def __init__(self, page, site: Site):
        self.page, self.site = page, site

    def text(self, url: str) -> str:
        check_url(self.site, url)
        status, body = self.page.evaluate("async u=>{const r=await fetch(u);return [r.status,await r.text()]}", url)
        if status != 200:
            raise RuntimeError(f"{self.site.tag} fetch {url[:100]} -> HTTP {status}")
        return body

    html = text


def _close_quietly(browser, tag: str) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"{tag}: browser.close() failed: {e}", file=sys.stderr)


def _connect(p, site: Site, url: str):
    """(browser, page) on url; headless first, visible fallback (auto mode). Blocked/unreachable -> RuntimeError."""
    check_url(site, url)
    last_err: Exception | None = None
    tried = modes()
    for headless in tried:
        browser = None
        try:
            browser = common.launch_browser(p, headless=headless)
            page = browser.new_context(user_agent=common.UA).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            check_url(site, page.url)
            try:  # slow render is not "blocked": wait for real content before judging
                page.wait_for_function("document.body && document.body.innerText.length >= 200", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            dismiss_consent(page, site)
            if common.looks_blocked(resp.status if resp else None, page.inner_text("body")):
                raise RuntimeError(f"blocked (status {resp.status if resp else None})")
            return browser, page
        except (PlaywrightTimeoutError, PlaywrightError, RuntimeError, ValueError) as e:
            last_err = e
            _close_quietly(browser, site.tag)
        except BaseException:
            _close_quietly(browser, site.tag)
            raise
    raise RuntimeError(f"{site.brand} page unreachable (modes tried: {tried}): {last_err}")


@contextmanager
def session(site: Site, url: str):
    with sync_playwright() as p:
        browser, page = _connect(p, site, url)
        try:
            yield Session(page, site)
        finally:
            browser.close()


# ---------------------------------------------------------------- flight payload (shared core)

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def flight_text(html: str) -> str:
    """Concatenate the JSON-string payload of every self.__next_f.push([1,"..."]) chunk."""
    chunks = _PUSH.findall(html)
    if not chunks:
        raise ValueError("page has no self.__next_f flight payload chunks")
    return "".join(json.loads(c) for c in chunks)


def json_after(text: str, marker: str, open_char: str = "["):
    """Decode the JSON value that starts at the first `open_char` after `marker`; ValueError if marker is missing."""
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"payload marker not found: {marker!r}")
    return json.JSONDecoder().raw_decode(text, text.index(open_char, i))[0]


def title_of(flight: str) -> tuple[str, str]:
    """(valueClass, headline) of the product title object."""
    m = re.search(r'"title":(?=\{"valueClass":)', flight)
    if not m:
        raise ValueError("title marker not found")
    obj = json.JSONDecoder().raw_decode(flight, m.end())[0]
    return str(obj.get("valueClass") or ""), str(obj.get("headline") or "")


def clean_name(parts) -> str:
    """Name from productName/title parts: registered/trademark marks dropped, whitespace collapsed."""
    text = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    return " ".join(text.replace("®", "").replace("™", "").replace("_IN", " in").split())


def parse_listing(html: str) -> tuple[list[dict], int]:
    """(raw productList items, total) of one category page. ValueError if the payload shape is unrecognised."""
    flight = flight_text(html)
    i = flight.find('"productList":{"items":')
    if i < 0:
        raise ValueError("productList payload marker not found")
    pl = json.JSONDecoder().raw_decode(flight, i + len('"productList":'))[0]
    if not isinstance(pl.get("items"), list) or not isinstance(pl.get("total"), int):
        raise ValueError("productList structure unrecognised")
    return pl["items"], pl["total"]


def iter_listing(s: Session, cat: str, kind: str = "mkt-category") -> Iterator[dict]:
    """Items of `<base>/<kind>/<cat>`, page by page (?pageNumber=N from 2), until `total` items were seen."""
    seen, page_no, total = 0, 1, 1
    while seen < total and page_no <= MAX_PAGES:
        url = f"{s.site.base}/{kind}/{cat}" + (f"?pageNumber={page_no}" if page_no > 1 else "")
        items, total = parse_listing(s.html(url))
        if not items:
            return
        seen += len(items)
        yield from items
        page_no += 1
        if seen < total:
            time.sleep(DELAY_S)


def item_price(item: dict) -> float | None:
    """List price of a listing item (recommended retail / shop price); promotions are never read."""
    p = item.get("price") or {}
    ok = p.get("kind") in ("RECOMMENDED_RETAIL_PRICE", "SHOP_PRICE") and isinstance(p.get("amount"), (int, float))
    return float(p["amount"]) if ok and p["amount"] > 0 else None


def item_url(site: Site, item: dict, kind: str, root: str | None = None) -> str | None:
    """Absolute https product URL of a listing item on the site's own hosts, else None (invalid / other root)."""
    code, path = item.get("productCode"), item.get("urlPath") or ""
    if not code or not _CODE.match(str(code)) or not path.startswith(f"/{kind}/") or _NOT_A_PRODUCT.search(path):
        return None
    if root and not path.startswith(f"/{kind}/{root}/"):
        return None
    url = urljoin(site.base + "/", path.lstrip("/"))
    if not host_in(url, site.page_hosts):
        print(f"{site.tag}: dropped off-domain candidate {code}: {url[:100]}", file=sys.stderr)
        return None
    return url


def item_wifi(item: dict) -> bool | None:
    tags = item.get("technicalTags")
    return True if isinstance(tags, list) and any(isinstance(t, dict) and t.get("isHomeConnect") for t in tags) else None


# ---------------------------------------------------------------- site-published signals (shared core)
# Rating / review count / NEW flag / release date exactly as the site publishes them; a missing key = unknown.

_NEW_BADGE = re.compile(r"^\s*(new|neu|new arrival|neuheit)\s*!?\s*$", re.I)
_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def rating_signals(average, count, best=5) -> dict:
    """{'rating', 'review_count'} on the 5-point scale; {} when there are no reviews (0 / 0 means none) or bad values."""
    ok = (isinstance(average, (int, float)) and isinstance(count, (int, float)) and not isinstance(average, bool)
          and isinstance(best, (int, float)) and best > 0 and count > 0 and average > 0)
    if not ok:
        return {}
    rating = round(float(average) * 5 / float(best), 2)
    return {"rating": rating, "review_count": int(count)} if 0 < rating <= 5 else {}


def release_signals(value) -> dict:
    """{'release_date', 'release_src': 'site'} from a product-page date string (YYYY[-MM[-DD]] prefix); {} otherwise."""
    m = _DATE.match(value) if isinstance(value, str) else None
    if not m:
        return {}
    return {"release_date": "-".join(g for g in m.groups() if g), "release_src": "site"}


def item_signals(item: dict) -> dict:
    """Signals of a listing item: rating {average, count} and a NEW badge text (primary/secondary badge)."""
    r = item.get("rating")
    out = rating_signals(r.get("average"), r.get("count")) if isinstance(r, dict) else {}
    badges = item.get("buyAreaOptionBadges")
    if isinstance(badges, dict) and any(isinstance(b, dict) and _NEW_BADGE.match(str(b.get("text") or ""))
                                        for b in badges.values()):
        out["is_new"] = True
    if item.get("isNewProduct") is True:
        out["is_new"] = True
    return out


def page_signals(flight: str) -> dict:
    """Signals of a product page: schema.org aggregateRating, product.isNewProduct (True only) and releaseDate."""
    out: dict = {}
    m = re.search(r'"aggregateRating":(?=\{)', flight)
    if m:
        try:
            agg = json.JSONDecoder().raw_decode(flight, m.end())[0]
        except ValueError:
            agg = {}
        out.update(rating_signals(_as_number(agg.get("ratingValue")), _as_number(agg.get("reviewCount") or agg.get("ratingCount")),
                                  _as_number(agg.get("bestRating")) or 5))
    m = re.search(r'"isNewProduct":(true|false)', flight)  # first hit = the page's own product object
    if m and m.group(1) == "true":
        out["is_new"] = True
    m = re.search(r'"releaseDate":(null|"[^"]*")', flight)
    if m and m.group(1) != "null":
        out.update(release_signals(m.group(1).strip('"')))
    return out


def _as_number(v):
    if isinstance(v, str):
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return None
    return v


# ---------------------------------------------------------------- spec table (shared core)

def noise(t) -> str:
    """Strip registered/trademark marks; line breaks inside a value become ' | '."""
    lines = [" ".join(l.replace("®", "").replace("™", "").split()) for l in str(t).splitlines()]
    return " | ".join(l for l in lines if l)


def spec_rows(sections: list[dict]) -> list[dict]:
    """One dict per non-empty spec row: sec_key, sec, key (platform code), label, value (+unit; booleans Yes/No)."""
    out = []
    for sec in sections:
        for r in sec.get("specifications") or []:
            val = noise(r["value"]["text"])
            if val.startswith("specifications.translatedBoolean."):
                val = val.rsplit(".", 1)[1].capitalize()
            if not val:
                continue
            if r.get("unit"):
                val += f" {r['unit']}"
            out.append({"sec_key": sec.get("key") or "", "sec": noise(sec.get("name") or ""), "key": r.get("key") or "",
                        "label": noise(r["name"]["text"]), "value": val})
    return out


def main_image_url(site: Site, flight: str) -> str | None:
    """Hero image: first entry of the page's schema.org `productJsonLd.image` list (absolute https on an image host)."""
    m = re.search(r'"productJsonLd":(?=\{)', flight)
    if not m:
        return None
    try:
        img = json.JSONDecoder().raw_decode(flight, m.end())[0].get("image")
    except ValueError:
        return None
    for raw in img if isinstance(img, list) else [img]:
        if isinstance(raw, str) and raw.strip():
            url = urljoin(site.base + "/", raw.strip())
            return url if urlparse(url).scheme == "https" and host_in(url, site.image_hosts) else None
    return None


def pick_docs(site: Site, technical_documents: list[dict], doc_types: dict[str, str] = DOC_TYPES) -> list[tuple[str, str]]:
    """(our doc_type, url): the first https PDF per wanted titleKey on an allowed host."""
    out, used = [], set()
    by_type: dict[str, list[str]] = {}
    for d in technical_documents:
        dtype, u = doc_types.get(d.get("titleKey")), d.get("url") or ""
        if dtype and u.lower().endswith(".pdf") and urlparse(u).scheme == "https" and host_in(u, site.pdf_hosts):
            by_type.setdefault(dtype, []).append(u)
    for dtype in doc_types.values():
        urls = by_type.get(dtype) or []
        if urls and urls[0] not in used:
            used.add(urls[0])
            out.append((dtype, urls[0]))
    return out


def download_docs(site: Site, brand: str, model: str, flight: str) -> list[DocumentRecord]:
    try:
        technical = json_after(flight, '"technicalDocuments":[')
    except ValueError:
        return []
    docs = []
    for dtype, durl in pick_docs(site, technical):
        rec = common.download_pdf(brand, model, dtype, durl)
        if rec:
            docs.append(rec)
    return docs


def frac(s: str) -> float:
    """'35 5/8' -> 35.625; '3/8 + 3 7/8' -> 4.25"""
    total = 0.0
    for chunk in str(s).split("+"):
        for part in chunk.split():
            if "/" in part:
                a, b = part.split("/")
                total += int(a) / int(b)
            else:
                total += float(part)
    return total


def dims_hwd(text: str) -> tuple[float, float, float] | None:
    """(H, W, D) numbers of a 'H x W x D' string (inch fractions allowed), else None."""
    parts = [p for p in re.split(r"\s*[xX×]\s*", " ".join(text.replace("''", " ").replace('"', " ").split())) if p.strip()]
    try:
        vals = [frac(p) for p in parts]
    except (ValueError, ZeroDivisionError):
        return None
    return (vals[0], vals[1], vals[2]) if len(vals) == 3 else None


def boolean(v: str | None) -> bool | None:
    if not v:
        return None
    return True if v.lower().startswith("yes") else False if v.lower().startswith("no") else None


# ---------------------------------------------------------------- classification (Thermador)

def classify(path: str, name: str = "") -> str | None:
    """Sub key from a /mkt-product/ path + product name. Single source of truth for discover() and scrape();
    mutually exclusive by precedence. None = not one of our groups (wine coolers, warming drawers, accessories ...)."""
    rel = path.split("/mkt-product/", 1)[-1].strip("/").lower()
    n = " ".join((name or "").lower().replace("built in", "built-in").split())
    top = rel.split("/")[0]
    if _NOT_A_PRODUCT.search("/" + rel) or "wine" in rel or "wine" in n:
        return None
    if top == "ranges":
        if "induction" in n:
            return "induction"
        return "gas_oven" if re.search(r"(?<![a-z])gas(?![a-z])|dual fuel", n) else None
    if top == "cooktops-rangetops":
        if "induction" in rel or "induction" in n:
            return "induction"
        return "gas_cooktop" if re.search(r"gas|rangetop", rel + " " + n) else None
    if top == "ovens":
        if "warming" in rel or "warming" in n:
            return None
        if "over-the-range" in n:
            return "otr"
        if "ovensmicrowaves" in rel or re.search(r"microwave|microdrawer", n) and "combination" not in n:
            return "microwave"
        if "/speed-ovens/" in "/" + rel or re.search(r"speed oven|combination", n):
            return "sco"
        return "electric_oven"
    return None


# ---------------------------------------------------------------- discover

_WIDTH_IN = re.compile(r"(?<![\d.])(\d{2})\s*(?:''|\"|_IN|\s?in\b)")


def listing_attrs(item: dict, name: str, sub: str) -> dict:
    """Candidate.attrs from what the listing states (standard units); a missing key = unknown."""
    import filters
    attrs: dict = {}
    m = _WIDTH_IN.search(name)
    if m and 12 <= int(m.group(1)) <= 60:
        attrs["width_in"] = float(m.group(1))
    if item_wifi(item):
        attrs["wifi"] = True
    fin = filters.finish_word(name)
    if fin:
        attrs["finish"] = fin
    attrs.update(item_signals(item))
    return attrs


def item_to_candidate(item: dict, major: str, sub: str, root: str) -> Candidate | None:
    """Candidate for a listing item, or None if invalid or classified under another sub key."""
    url = item_url(SITE, item, "mkt-product", root)
    if not url:
        return None
    name = clean_name(item.get("productName") or [])
    if classify(item["urlPath"], name) != sub:
        return None
    price = item_price(item)
    attrs = listing_attrs(item, name, sub)
    return Candidate(brand=BRAND, model_number=item["productCode"], name=name or item["productCode"], url=url,
                     price_usd=price, category=major, subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, price_local=price, attrs=attrs, attrs_src={k: "listing" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Thermador US")
    major, cats = SUB_SOURCES[subcategory]
    found: dict[str, Candidate] = {}
    with session(SITE, f"{SITE.base}/mkt-category/{cats[0]}") as s:
        for cat in cats:
            listed, kept_before = 0, len(found)
            for item in iter_listing(s, cat):
                listed += 1
                c = item_to_candidate(item, major, subcategory, cat.split("/")[0])
                if c:
                    found.setdefault(c.model_number, c)
                if len(found) >= limit:
                    break
            print(f"thermador_us: discover({subcategory}) {cat}: {listed} listed, {len(found) - kept_before} kept",
                  file=sys.stderr)
            if len(found) >= limit:
                break
            time.sleep(DELAY_S)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape

def model_from_url(url: str) -> tuple[str, str]:
    """(model, root) from https://www.thermador.com/us/en/mkt-product/<root>/.../<MODEL>; ValueError otherwise."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not host_in(url, SITE.page_hosts) or parts[:3] != ["us", "en", "mkt-product"]
            or len(parts) < 5 or ".." in parts or not _CODE.match(parts[-1]) or parts[3] not in ROOT_MAJOR):
        raise ValueError(f"not a supported Thermador US product URL: {url}")
    return parts[-1], parts[3]


# keys folded into typed ProductRecord fields (kept out of the flat extra_specs labels)
_CONSUMED = {"COL_MAIN", "ENERGY_STAR_QUALIFIED", "DIM_US", "WEIGHT_NET_US", "WEIGHT_GROSS_US", "VOLTAGE", "CURRENT",
             "HOMECONNECTABLE", "HOMECONNECT_TYPE"}


def _num(pattern: str, text: str) -> float | None:
    return common.num(pattern, text, re.I)


def parse_product(model: str, url: str, root: str, flight: str) -> tuple[ProductRecord, list[RawSpec]]:
    """Product record (web data) and the full spec table from a product-page flight payload."""
    sections = json_after(flight, '"specifications":[{"name"')
    try:
        pricing = json_after(flight, '"pricing":', "{")
    except ValueError:
        pricing = {}
    try:
        highlights = json_after(flight, '"highlights":[')
    except ValueError:
        highlights = []
    title_class, headline = title_of(flight)
    rows = spec_rows(sections)
    spec: dict[str, str] = {}
    for r in rows:
        spec.setdefault(r["key"], r["value"])
    major = ROOT_MAJOR[root]
    name = clean_name([title_class, headline]) or model

    dims = dims_hwd(spec.get("DIM_US", ""))
    net, gross = _num(r"([\d.]+)", spec.get("WEIGHT_NET_US", "")), _num(r"([\d.]+)", spec.get("WEIGHT_GROSS_US", ""))
    hc, hc_type = spec.get("HOMECONNECTABLE"), spec.get("HOMECONNECT_TYPE")
    amount = pricing.get("amount") if isinstance(pricing, dict) else None
    price = float(amount) if isinstance(amount, (int, float)) and amount > 0 else None

    extra = {f"{r['sec']} > {r['label']}": r["value"] for r in rows}
    for r in rows:  # legacy flat labels (compare/POD lookups) except what typed fields already carry
        if r["key"] in _CONSUMED:
            continue
        extra.setdefault(r["label"], r["value"])
    if net is None and gross is not None:
        extra["Weight basis"] = "gross (net weight not listed on web page)"
    path = urlparse(url).path
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category=major, subcategory=classify(path, name),
        product_url=url, finish_color=spec.get("COL_MAIN"), region=REGION, country=COUNTRY, currency=CURRENCY,
        price_usd=price, price_local=price,
        height_in=dims[0] if dims else None, width_in=dims[1] if dims else None, depth_in=dims[2] if dims else None,
        weight_lb=net if net is not None else gross,
        voltage_v=spec["VOLTAGE"].removesuffix(" V") if "VOLTAGE" in spec else None,
        amps=_num(r"([\d.]+)\s*A\b", spec.get("CURRENT", "")),
        energy_star=boolean(spec.get("ENERGY_STAR_QUALIFIED")),
        wifi_supported=boolean(hc),
        wifi_evidence=(f"Home Connect: {hc}" + (f"; features: {hc_type}" if hc_type else "")) if hc else None,
        pod_features=[h["headline"]["text"] for h in highlights
                      if isinstance(h, dict) and isinstance(h.get("headline"), dict) and h["headline"].get("text")],
        extra_specs=extra, image_url=main_image_url(SITE, flight), **page_signals(flight),
    )
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=r["sec"], key=r["label"], value=r["value"])
           for r in rows]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model, root = model_from_url(url)
    with session(SITE, url) as s:
        flight = flight_text(s.html(url))
    record, raw = parse_product(model, url, root, flight)
    return record, download_docs(SITE, BRAND, model, flight), raw
