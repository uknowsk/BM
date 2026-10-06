"""Shared helpers of the Electrolux-group adapters (frigidaire_us, electrolux_us, electrolux_de, aeg_de, aeg_uk).

Two platforms, both reached through a real browser page (same-origin fetch(), cookie banner declined first):

US (frigidaire.com, electrolux.com): SAP Commerce OCC JSON at apolloapi.electrolux.com/occ/v2/<site>/...
  * listing:  products/search?query=:relevance:allCategories:<M_FoodPreparation_...>&pageSize=N&currentPage=P
  * product:  products/<variant code>?fields=FULL  (classifications = the complete spec table, images, ownerManuals)
  Akamai answers old/new headless Chromium with ERR_HTTP2_PROTOCOL_ERROR, so auto mode falls back to a visible window.

EU (electrolux.de, aeg.de, aeg.co.uk): one Next.js front-end.
  * electrolux.de has no web shop: the category page embeds `productList` in __NEXT_DATA__ (?page=N shows 15*N items).
  * aeg.de / aeg.co.uk list through /external/commerce/ccv2/occ/<DEU-AEG|GBR-AEG>/products/search/ (same OCC shape).
  * every product page renders the full spec table client side (`.full-specifications__wrapper` blocks).
Scraped text is untrusted: URLs are https + host checked, ids are validated, nothing is executed.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

DELAY_S = 1.0
MAX_PAGES = 6
API_HOST = "apolloapi.electrolux.com"
CDN_HOST = "electrolux.bynder.com"  # EU product images / manuals
_CODE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_SAFE_PATH = re.compile(r"^(/[A-Za-z0-9._-]+)+$")


# ------------------------------------------------------------------ browser session
def browser_modes() -> list[bool]:
    """Headless flags to try, in order. FRIDGE_BROWSER_MODE (auto | headless | visible) is read on every call."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [True]
    if mode == "visible":
        return [False]
    return [True, False]


def host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def check_final_url(url: str, hosts: tuple[str, ...]) -> None:
    if urlparse(url).scheme != "https" or not host_in(url, hosts):
        raise ValueError(f"unexpected host after navigation: {url[:120]}")


_DECLINE = ("#onetrust-reject-all-handler", "button:has-text('Reject All')", "button:has-text('Reject all')",
            "button:has-text('Alle ablehnen')", "button:has-text('Nur notwendige')")


def dismiss_consent(page) -> None:
    """Decline non-essential cookies first; accept only when the banner is still blocking afterwards."""
    for sel in _DECLINE:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=600):
                loc.click(timeout=2500)
                page.wait_for_timeout(300)
                break
        except PlaywrightError:
            continue
    try:
        if page.locator("#onetrust-banner-sdk").first.is_visible(timeout=400):
            page.locator("#onetrust-accept-btn-handler").first.click(timeout=2500)
            page.wait_for_timeout(300)
    except PlaywrightError:
        pass


def _close_quietly(browser) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except PlaywrightError as e:
        print(f"electrolux: browser.close() failed: {e}", file=sys.stderr)


_HEADLESS_FAILED: set[str] = set()  # hosts whose headless launch was refused (Akamai): go straight to visible next time


def connect(p, url: str, hosts: tuple[str, ...]):
    """(browser, page) opened on url; headless first, visible window as the fallback (auto mode)."""
    check_final_url(url, hosts)
    host = urlparse(url).hostname or ""
    modes = browser_modes()
    if len(modes) > 1 and host in _HEADLESS_FAILED:
        modes = [False]
    last_err: Exception | None = None
    for headless in modes:
        browser = None
        try:
            browser = common.launch_browser(p, headless=headless)
            page = browser.new_context(user_agent=common.UA if headless else None).new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            check_final_url(page.url, hosts)
            try:
                page.wait_for_function("document.body && document.body.innerText.length >= 200", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            dismiss_consent(page)
            if common.looks_blocked(resp.status if resp else None, page.inner_text("body")):
                raise RuntimeError(f"blocked (status {resp.status if resp else None})")
            return browser, page
        except (PlaywrightTimeoutError, PlaywrightError, RuntimeError, ValueError) as e:
            last_err = e
            if headless:
                _HEADLESS_FAILED.add(host)
            _close_quietly(browser)
        except BaseException:
            _close_quietly(browser)
            raise
    raise RuntimeError(f"{host} unreachable (modes tried: {modes}): {last_err}")


class Session:
    """A browser page on the brand site; same-origin / API fetches use the page's cookies."""

    def __init__(self, page, hosts: tuple[str, ...], api_hosts: tuple[str, ...] = ()):
        self.page, self.hosts, self.api_hosts = page, hosts, api_hosts

    def _allowed(self, url: str) -> None:
        if urlparse(url).scheme != "https" or not host_in(url, self.hosts + self.api_hosts):
            raise ValueError(f"fetch outside the allowed hosts: {url[:120]}")

    def text(self, url: str) -> str:
        self._allowed(url)
        status, body = self.page.evaluate(
            "async u=>{const r=await fetch(u,{headers:{Accept:'*/*'}});return [r.status,await r.text()]}", url)
        if status != 200:
            raise RuntimeError(f"fetch {url[:100]} -> HTTP {status}")
        return body

    def json(self, url: str):
        return json.loads(self.text(url))

    def rendered(self, url: str, ready: str, timeout_ms: int = 25000) -> str:
        """Navigate to a product page and return the DOM once `ready` (CSS selector) is attached."""
        self._allowed(url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        check_final_url(self.page.url, self.hosts)
        try:
            self.page.wait_for_selector(ready, state="attached", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            print(f"electrolux: {ready} not found on {url[:100]}", file=sys.stderr)
        self.page.wait_for_timeout(800)
        return self.page.content()


@contextmanager
def session(url: str, hosts: tuple[str, ...], api_hosts: tuple[str, ...] = ()):
    with sync_playwright() as p:
        browser, page = connect(p, url, hosts)
        try:
            yield Session(page, hosts, api_hosts)
        finally:
            browser.close()


# ------------------------------------------------------------------ small parsers
_FRAC = re.compile(r"(\d+(?:\.\d+)?)(?:\s+(\d+)/(\d+))?")


def frac_in(text) -> Optional[float]:
    """'35 5/8"' -> 35.625, '30"' -> 30.0, '67 9/10"' -> 67.9; None when there is no number."""
    m = _FRAC.search(str(text or ""))
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) and int(m.group(3)):
        v += int(m.group(2)) / int(m.group(3))
    return round(v, 3)


def first_num(text) -> Optional[float]:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(text or ""))
    return float(m.group(0).replace(",", "")) if m else None


_NOISE = re.compile(r"[﻿​‌‍­]")


def clean(text) -> str:
    s = _NOISE.sub("", str(text if text is not None else ""))
    s = _html.unescape(s).replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", s).strip()


def yes_no(v) -> Optional[bool]:
    s = clean(v).lower()
    if re.match(r"(yes|true|ja)\b", s):
        return True
    if re.match(r"(no|none|false|nein|n/a|keine?)\b", s):
        return False
    return None


def review_signal(rating, count, best=5) -> dict:
    """{'rating': 0-5 float, 'review_count': int} from the site's own review summary (rating on a 0..best scale, rescaled to
    5); {} when either value is missing, malformed or there is no review."""
    try:
        r, n, b = float(rating), int(float(count)), float(best)
    except (TypeError, ValueError):
        return {}
    return {"rating": round(r * 5 / b, 2), "review_count": n} if b > 0 and 0 < r <= b and n > 0 else {}


_NEW_FLAG = re.compile(r"new( arrival| product)?!?", re.I)


def is_new_flag(*labels) -> dict:
    """{'is_new': True} when the site's own flag/label reads 'New' (never False: no flag = unknown)."""
    return {"is_new": True} if any(_NEW_FLAG.fullmatch(clean(x)) for x in labels if isinstance(x, str)) else {}


def put_spec(table: dict[str, str], key: str, value: str) -> None:
    """Add key -> value; a repeated key keeps every distinct value joined with ' | '."""
    if key in table:
        if value not in table[key].split(" | "):
            table[key] = f"{table[key]} | {value}"
    else:
        table[key] = value


def _doc_name(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", model).strip(".") or "x"


def fetch_docs(brand: str, model: str, wanted: list[tuple[str, str]]) -> list[DocumentRecord]:
    """Download (doc_type, https url) pairs through common.download_pdf (allowlist + public-IP checks); failures are
    logged and skipped, they never fail the product."""
    docs: list[DocumentRecord] = []
    for dtype, url in wanted:
        time.sleep(DELAY_S)
        rec = common.download_pdf(brand, model, dtype, url)
        if rec:
            docs.append(rec)
    return docs


# ================================================================== US (OCC JSON)
@dataclass(frozen=True)
class UsSite:
    brand: str
    site: str                      # OCC base site: 'frigidaire' | 'electrolux'
    base: str                      # https://www.frigidaire.com
    domain: str                    # frigidaire.com
    sub_categories: dict           # sub key -> OCC category codes queried by discover()

    @property
    def occ(self) -> str:
        return f"https://{API_HOST}/occ/v2/{self.site}"

    @property
    def hosts(self) -> tuple[str, ...]:
        return (self.domain,)


US_MICRO = "M_FoodPreparation_Microwaves"
US_WALL = "M_FoodPreparation_WallOvens"
US_RANGE = "M_FoodPreparation_Ranges"
US_TOP = "M_FoodPreparation_Cooktops"


def classify_us(codes, name: str) -> Optional[str]:
    """Cooking sub key of an Electrolux US product from its OCC category codes + name (shared by discover/scrape).
    Precedence: otr, sco (combination wall oven / name with microwave|speed), microwave, wall oven (gas wall oven ->
    gas_oven, else electric_oven), ranges (gas|dual fuel -> gas_oven, induction, electric -> radiant), cooktops."""
    cs = {str(c) for c in codes or ()}
    n = (name or "").lower()

    def has(*suffix: str) -> bool:
        return any(c.endswith(s) for c in cs for s in suffix)

    wall = has("_WallOvens_Single", "_WallOvens_Double", "_WallOvens_MicrowaveCombination", "_WallOvens")
    if has("Microwaves_OverTheRange"):
        return "otr"
    if has("WallOvens_MicrowaveCombination") or (wall and re.search(r"microwave|speed oven", n)):
        return "sco"
    if has("Microwaves_BuiltIn", "Microwaves_Countertop") or (has("_Microwaves") and not wall):
        return "microwave"
    if wall:
        return "gas_oven" if re.search(r"\bgas\b", n) else "electric_oven"
    if has("Ranges_Gas", "Ranges_DualFuel"):
        return "gas_oven"
    if has("Ranges_Induction"):
        return "induction"
    if has("Ranges_Electric"):
        return "radiant"
    if has("Cooktops_Gas"):
        return "gas_cooktop"
    if has("Cooktops_Induction"):
        return "induction"
    if has("Cooktops_Electric"):
        return "radiant"
    if has("_Ranges", "_Cooktops"):  # parent category only: decide from the name
        if "induction" in n:
            return "induction"
        if re.search(r"\bgas\b|dual[- ]fuel", n):
            return "gas_oven" if re.search(r"range", n) and "rangetop" not in n else "gas_cooktop"
        return "radiant"
    return None


_US_FUEL = {"gas_oven": "gas", "gas_cooktop": "gas", "induction": "induction", "radiant": "electric",
            "electric_oven": "electric"}


def us_model(item: dict) -> Optional[str]:
    """Orderable model code of a listing item: the last segment of its `url` ('/c//p/GCFG3060BF-A1')."""
    code = str(item.get("url") or "").rstrip("/").rsplit("/", 1)[-1] or str(item.get("code") or "")
    return code if _CODE.match(code) else None


def us_candidate(cfg: UsSite, item: dict, sub: str, category_code: str) -> Optional[Candidate]:
    model = us_model(item)
    name = clean(item.get("name"))
    if not model or not name or classify_us([category_code], name) != sub:
        return None
    cat = str(item.get("categoryUrl") or "")
    path = cat if _SAFE_PATH.match(cat) else ""
    url = f"{cfg.base}/en/p{path}/{model}"
    price = (item.get("price") or {}).get("value")
    variants = [v for v in item.get("colorVariants") or [] if isinstance(v, dict)]
    first = next((v for v in variants if v.get("code") == model), variants[0] if variants else {})
    attrs: dict = {}
    w = frac_in(first.get("width"))
    if w:
        attrs["width_in"] = w
    if sub in _US_FUEL:
        attrs["fuel"] = "dual_fuel" if re.search(r"dual[- ]fuel", name, re.I) else _US_FUEL[sub]
    try:
        import filters
        fin = filters.finish_word(str(first.get("color") or ""))
        if fin:
            attrs["finish"] = fin
    except ImportError:
        pass
    attrs.update(is_new_flag(first.get("primaryFlag")))   # PLP corner flag ('Best Seller', 'Top Rated', 'Best Deal', ...)
    return Candidate(brand=cfg.brand, model_number=model, name=name, url=url,
                     price_usd=float(price) if price else None, category="cooking", subcategory=sub,
                     region="na", country="us", currency="USD",
                     attrs=attrs, attrs_src={k: "listing" for k in attrs})


def us_discover(cfg: UsSite, sub: str, limit: int = 30) -> list[Candidate]:
    if sub not in cfg.sub_categories:
        raise ValueError(f"unsupported subcategory {sub!r} for {cfg.brand} US")
    found: dict[str, Candidate] = {}
    size = min(50, max(limit, 12))
    with session(cfg.base + "/en/", cfg.hosts, (API_HOST,)) as s:
        for code in cfg.sub_categories[sub]:
            page = 0
            while len(found) < limit and page < MAX_PAGES:
                q = quote(f":relevance:allCategories:{code}", safe="")
                d = s.json(f"{cfg.occ}/products/search?query={q}&pageSize={size}&currentPage={page}&lang=en&curr=USD")
                items = d.get("products") or []
                for it in items:
                    c = us_candidate(cfg, it, sub, code)
                    if c:
                        found.setdefault(c.model_number, c)
                page += 1
                if not items or page >= int((d.get("pagination") or {}).get("totalPages") or 0):
                    break
                time.sleep(DELAY_S)
            if len(found) >= limit:
                break
            time.sleep(DELAY_S)
    return list(found.values())[:limit]


def us_model_from_url(cfg: UsSite, url: str) -> str:
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not host_in(url, cfg.hosts) or len(parts) < 3 or parts[:2] != ["en", "p"]
            or not _CODE.match(parts[-1])):
        raise ValueError(f"not a supported {cfg.brand} US product URL: {url}")
    return parts[-1]


def us_spec_rows(classifications) -> list[tuple[str, str, str]]:
    """(section, label, value) of every classification feature; multi values joined with ' | '."""
    rows = []
    for c in classifications or []:
        sec = clean(c.get("name"))
        for f in c.get("features") or []:
            label = clean(f.get("name"))
            vals = [clean(v.get("value")) for v in f.get("featureValues") or [] if clean(v.get("value"))]
            unit = clean((f.get("featureUnit") or {}).get("symbol"))
            val = " | ".join(vals)
            if unit and val and unit.lower() not in val.lower():
                val = f"{val} {unit}"
            if label and val:
                rows.append((sec, label, val))
    return rows


def us_main_image(p: dict) -> Optional[str]:
    imgs = [i for i in p.get("images") or [] if isinstance(i, dict) and i.get("url")]
    cand = [i["url"] for i in imgs if i.get("imageType") == "PRIMARY"] + [p.get("plpImage")] + [i["url"] for i in imgs]
    for u in cand:
        if isinstance(u, str) and urlparse(u).scheme == "https" and host_in(u, ("bynder.com", "electrolux.com",
                                                                                  "frigidaire.com")):
            return u
    return None


_US_DOC_TYPES = (("energy guide", "EnergyGuide"), ("installation", "Installation"), ("specification", "SpecSheet"),
                 ("quick", "QuickSpecs"), ("owner", "Manual"), ("use & care", "Manual"), ("use and care", "Manual"),
                 ("manual", "Manual"))


def us_docs(p: dict) -> list[tuple[str, str]]:
    """(doc_type, https pdf url): one English PDF per type from `ownerManuals` (Right-to-Repair/warranty sheets skipped)."""
    out: dict[str, str] = {}
    for d in p.get("ownerManuals") or []:
        url, lang = str(d.get("downloadUrl") or d.get("url") or ""), str(d.get("language") or "")
        title = str(d.get("altText") or "").lower()
        if (urlparse(url).scheme != "https" or not url.lower().endswith(".pdf") or not lang.lower().startswith("en")
                or "repair" in title):
            continue
        dtype = next((t for needle, t in _US_DOC_TYPES if needle in title), None)
        if dtype and dtype not in out:
            out[dtype] = url
    return list(out.items())


def us_parse_product(cfg: UsSite, model: str, url: str, p: dict) -> tuple[ProductRecord, list[RawSpec]]:
    if not isinstance(p, dict) or not p.get("name") or not p.get("classifications"):
        raise ValueError(f"{cfg.brand} US product data unrecognised for {model}")
    rows = us_spec_rows(p["classifications"])
    first: dict[str, str] = {}
    table: dict[str, str] = {}
    for sec, label, val in rows:
        first.setdefault(label, val)
        put_spec(table, f"{sec} > {label}", val)
    codes = [c.get("code") for c in p.get("categories") or []]
    sub = classify_us(codes, p["name"])
    price = (p.get("price") or {}).get("value")
    volt = first.get("Voltage Rating") or next((v for k, v in first.items() if k.startswith("Connected Load @")), None)
    volt_n = re.search(r"(\d{3})\s*V", (volt or "") + " " + next((k for k in first if k.startswith("Connected Load @")), ""))
    wifi_key = next((l for s, l, _ in rows if re.search(r"wi-?fi", l, re.I) or re.fullmatch(r"connectivity", s, re.I)), None)
    star = first.get("ENERGY STAR Certified")
    weight = first_num(first.get("Product Weight"))
    feats = list(dict.fromkeys(clean(f.get("title")) for f in p.get("productFeatures") or [] if clean(f.get("title"))))
    variant = next((v for v in p.get("colorVariants") or [] if isinstance(v, dict) and v.get("code") == model), {})
    signals = {**review_signal(p.get("averageRating"), p.get("numberOfReviews")), **is_new_flag(variant.get("primaryFlag"))}
    record = ProductRecord(
        brand=cfg.brand, model_number=model, product_name=clean(p["name"]), category="cooking", subcategory=sub,
        finish_color=clean(p.get("color")) or None, product_url=url,
        price_usd=float(price) if price else None, region="na", country="us", currency="USD",
        height_in=frac_in(first.get("Height")), width_in=frac_in(first.get("Width")), depth_in=frac_in(first.get("Depth")),
        weight_lb=weight, voltage_v=volt_n.group(1) if volt_n else None,
        amps=first_num(first.get("Minimum Circuit Required")),
        energy_star=None if star is None else bool(yes_no(star)),
        wifi_supported=None if wifi_key is None else yes_no(first[wifi_key]),
        wifi_evidence=None if wifi_key is None else f"{wifi_key}: {first[wifi_key]}",
        pod_features=feats, extra_specs=table, image_url=us_main_image(p), **signals)
    raw = [RawSpec(brand=cfg.brand, model_number=model, source="web", section=sec, key=label, value=val)
           for sec, label, val in rows]
    return record, raw


def us_scrape(cfg: UsSite, url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = us_model_from_url(cfg, url)
    with session(cfg.base + "/en/", cfg.hosts, (API_HOST,)) as s:
        p = s.json(f"{cfg.occ}/products/{quote(model, safe='')}?fields=FULL&lang=en&curr=USD")
    if str(p.get("code") or "").upper() != model.upper():
        raise ValueError(f"{cfg.brand} US returned {p.get('code')!r} for {model}")
    record, raw = us_parse_product(cfg, model, url, p)
    return record, fetch_docs(cfg.brand, model, us_docs(p)), raw


# ================================================================== EU (Next.js front-end)
@dataclass(frozen=True)
class EuSite:
    brand: str
    country: str                   # 'de' | 'uk'
    base: str                      # https://www.aeg.de
    domain: str                    # aeg.de
    currency: str                  # EUR | GBP
    lang: str                      # 'de' | 'en'
    sub_sources: dict              # sub key -> tuple of category paths ('kitchen/cooking/ovens', ...)
    api_site: Optional[str] = None  # OCC base site of the shop API (DEU-AEG | GBR-AEG); None = SSR category pages only

    @property
    def hosts(self) -> tuple[str, ...]:
        return (self.domain,)


def _cat_tail(category: str) -> str:
    c = (category or "").strip("/")
    return c.split("kitchen/cooking/", 1)[-1] if "kitchen/cooking/" in c else c


_MICRO_RE = re.compile(r"mikrowell|microwave", re.I)
_INDUCTION_RE = re.compile(r"induktion|induction", re.I)
# a microwave combined with an oven in one name ('Microwave and Built-in Oven', 'Kompaktbackofen / Mikrowelle')
_COMBI_RE = re.compile(r"(?:mikrowell|microwave)\w*.{0,40}?(?:oven|backofen|herd)|(?:oven|backofen).{0,40}?(?:mikrowell|microwave)",
                       re.I)


def classify_eu(category: str, name: str = "", description: str = "") -> Optional[str]:
    """Cooking sub key of an Electrolux/AEG EU product from its category path ('kitchen/cooking/hobs/induction-hob')
    and name/description (shared by discover/scrape). Ovens with a microwave function are sco, a plain microwave is
    microwave; hobs/cookers split by heat source (no gas ovens/OTR exist in these markets: gas_oven/otr are never
    returned)."""
    tail = _cat_tail(category)
    text = f"{name} {description}"
    if tail.startswith("microwaves") or tail.startswith("compact-built-in-range/built-in-microwaves"):
        return "sco" if _COMBI_RE.search(text) else "microwave"
    if tail.startswith("ovens") or tail.startswith("compact-built-in-range/compact-oven"):
        return "sco" if _MICRO_RE.search(text) else "electric_oven"
    if tail.startswith("hobs/gas-hob"):
        return "gas_cooktop"
    if tail.startswith(("hobs/induction-hob", "hobs/electric-hob", "hobs/combohob", "cookers/electric-cooker")):
        if tail.startswith("hobs/induction-hob") or _INDUCTION_RE.search(text):
            return "induction"
        return "radiant"
    return None


# 'NN cm' that is a width, not the second/third number of 'H x W cm' (so '59 x 56 cm' and '37.1 x 59.4 cm' do not match)
_CM = re.compile(r"(?<![x×\d.,])(?<![x×]\s)(\d{2,3}(?:[.,]\d)?)\s*cm\b", re.I)
_CLASS_TAIL = re.compile(r"/\s*([A-G])(\+{0,3})\s*$")


def eu_price(item: dict) -> Optional[float]:
    """Selling price shown for a listing item: offerPrice (final price), else price (electrolux.de: recommended price)."""
    for key in ("offerPrice", "price"):
        v = (item.get(key) or {}).get("value")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def eu_item_signals(item: dict) -> dict:
    """rating / review_count (listing fields reviewRating, reviewCount) and is_new (b2BAttributes.isNewProduct, only when
    True; the shop API does not return it) of a listing item."""
    b2b = item.get("b2BAttributes")
    return {**review_signal(item.get("reviewRating"), item.get("reviewCount")),
            **({"is_new": True} if isinstance(b2b, dict) and b2b.get("isNewProduct") is True else {})}


def eu_candidate(cfg: EuSite, item: dict, sub: str) -> Optional[Candidate]:
    model = clean(item.get("modelId")).upper()
    name = clean(item.get("name"))
    desc = clean(item.get("description"))
    cat = str((item.get("categoryFallBack") or {}).get("categoryFallBackCode") or "")
    url = str(item.get("productURL") or "")
    url = urljoin(cfg.base + "/", url) if url.startswith("/") else url
    if (not model or not _CODE.match(model) or not name or classify_eu(cat, name, desc) != sub
            or urlparse(url).scheme != "https" or not host_in(url, cfg.hosts)):
        return None
    attrs: dict = {}
    m = _CM.search(name) or _CM.search(desc)
    if m:
        attrs["width_in"] = round(float(m.group(1).replace(",", ".")) / 2.54, 1)
    if sub in ("induction", "gas_cooktop", "radiant", "electric_oven", "sco"):
        attrs["fuel"] = {"induction": "induction", "gas_cooktop": "gas"}.get(sub, "electric")
    m = _CLASS_TAIL.search(desc)
    if m and not m.group(2):
        attrs["eu_class"] = m.group(1)
    attrs.update(eu_item_signals(item))
    price = eu_price(item)
    return Candidate(brand=cfg.brand, model_number=model, name=name, url=url, price_usd=None, category="cooking",
                     subcategory=sub, region="eu", country=cfg.country, currency=cfg.currency, price_local=price,
                     attrs=attrs, attrs_src={k: "listing" for k in attrs})


_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_API_FIELDS = ("products(code,modelId,name,description,price,offerPrice,rrp,productURL,categoryFallBack,d2cSellable,"
               "reviewRating,reviewCount),"
               "pagination")


def next_data(html_text: str) -> dict:
    m = _NEXT_DATA.search(html_text)
    if not m:
        raise ValueError("page has no __NEXT_DATA__ payload")
    return json.loads(m.group(1))


def _walk_find(o, key: str):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = _walk_find(v, key)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _walk_find(v, key)
            if r is not None:
                return r
    return None


def parse_ssr_listing(html_text: str) -> tuple[list[dict], int]:
    """(products, total) from the `productList` embedded in a category page's __NEXT_DATA__."""
    pl = _walk_find(next_data(html_text)["props"]["pageProps"]["pageData"].get("components"), "productList")
    if not isinstance(pl, dict) or not isinstance(pl.get("products"), list):
        raise ValueError("category page has no productList")
    return pl["products"], int((pl.get("pagination") or {}).get("totalResults") or len(pl["products"]))


def _eu_listing(cfg: EuSite, s: Session, path: str, want: int) -> list[dict]:
    """All raw items of one category path (API for the shop sites, SSR category pages for electrolux.de)."""
    items: list[dict] = []
    if cfg.api_site:
        q = quote(f":relevance:category:{path}:d2cSellable:true", safe=":")
        page = 0
        while len(items) < want and page < MAX_PAGES:
            d = s.json(f"{cfg.base}/external/commerce/ccv2/occ/{cfg.api_site}/products/search/?query={q}"
                       f"&currentPage={page}&pageSize=50&sort=relevance&fields={quote(_API_FIELDS, safe='')}")
            got = d.get("products") or []
            items += got
            page += 1
            if not got or page >= int((d.get("pagination") or {}).get("totalPages") or 0):
                break
            time.sleep(DELAY_S)
        return items
    for n in range(1, MAX_PAGES + 1):
        got, total = parse_ssr_listing(s.text(f"{cfg.base}/{path}/" + (f"?page={n}" if n > 1 else "")))
        items = got
        if len(got) >= min(total, want) or n * 15 >= total:
            break
        time.sleep(DELAY_S)
    return items


def eu_discover(cfg: EuSite, sub: str, limit: int = 30) -> list[Candidate]:
    if sub not in cfg.sub_sources:
        raise ValueError(f"unsupported subcategory {sub!r} for {cfg.brand} {cfg.country.upper()}")
    found: dict[str, Candidate] = {}
    with session(cfg.base + "/", cfg.hosts) as s:
        for path in cfg.sub_sources[sub]:
            for it in _eu_listing(cfg, s, path, 300 if sub == "sco" else max(limit * 3, 30)):
                c = eu_candidate(cfg, it, sub)
                if c:
                    found.setdefault(c.model_number, c)
            if len(found) >= limit:
                break
            time.sleep(DELAY_S)
    return list(found.values())[:limit]


# ------------------------------------------------------------------ EU product page
_SPEC_BLOCK = re.compile(r'<div class="full-specifications__wrapper"><div class="full-specifications__title">(.*?)</div>'
                         r'<ul class="full-specifications__items">(.*?)</ul>', re.S)
_SPEC_ITEM = re.compile(r'<li class="full-specifications__item">(.*?)</li>', re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return clean(_TAG.sub("", _COMMENT.sub("", fragment)))


def parse_spec_sections(html_text: str) -> list[tuple[str, list[str]]]:
    """[(section title, [item text])] in page order. The page renders the table twice (desktop + mobile): the first
    block of each title wins."""
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for m in _SPEC_BLOCK.finditer(html_text):
        title = _text(m.group(1))
        if not title or title in seen:
            continue
        seen.add(title)
        out.append((title, [t for t in (_text(i) for i in _SPEC_ITEM.findall(m.group(2))) if t]))
    return out


def parse_pdp(html_text: str) -> dict:
    """Structured product-page data: model, pnc, heading, brand, price (schema.org price), image urls, doc urls,
    spec sections. Raises ValueError when the page is not a product page."""
    comps = next_data(html_text)["props"]["pageProps"]["pageData"].get("components") or []
    pdp = next((c for c in comps if c.get("name") == "PDPContainer"), None)
    if not pdp:
        raise ValueError("not a product page (no PDPContainer)")
    props = pdp.get("props") or {}
    top = next((c.get("props") or {} for c in pdp.get("components") or [] if c.get("name") == "TopArea"), {})
    seo = props.get("productSEOSchemaDataModel") or {}
    images = [i.get("previewImageUrl") for i in seo.get("images") or [] if isinstance(i, dict)]
    label = top.get("productEnergyLabel") or {}
    price = seo.get("price")
    try:
        price = float(price) if price not in (None, "") else None
    except ValueError:
        price = None
    ld = re.search(r'"aggregateRating":\{[^{}]*\}', html_text)
    try:
        agg = json.loads("{" + ld.group(0) + "}")["aggregateRating"] if ld else {}
    except ValueError:
        agg = {}
    signals = review_signal(agg.get("ratingValue"), agg.get("ratingCount") or agg.get("reviewCount"),
                            agg.get("bestRating") or 5)
    if top.get("isNewProduct") is True or (props.get("b2BProperties") or {}).get("isNewProduct") is True:
        signals["is_new"] = True
    return {
        "signals": signals,
        "model": clean(top.get("modelId") or seo.get("name")).upper(), "pnc": clean(props.get("productCode")),
        "heading": clean(top.get("pageHeading") or seo.get("pageHeading")), "brand": clean(top.get("brand") or seo.get("brand")),
        "category": clean(seo.get("category")), "ean": clean(top.get("ean")), "schema_price": price,
        "images": [i for i in images if isinstance(i, str)] + ([props["productImage"]] if props.get("productImage") else []),
        "sheet_url": label.get("productInformationSheetUrl") or "",
        "manual_url": (top.get("productSafetyBanner") or {}).get("userManualUrl") or "",
        "sections": parse_spec_sections(html_text)}


_SECTION_EN = {"ausstattungsmerkmale": "Features", "leistung": "Performance", "gerätedaten": "Appliance data",
               "geraetedaten": "Appliance data", "technische daten": "Technical data", "energiewerte": "Energy values",
               "installation": "Installation", "sonstiges": "Other", "specification": "Features"}
_FEATURE_SECTIONS = {"ausstattungsmerkmale", "specification", "features", "key features"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _split_item(item: str) -> tuple[str, str]:
    label, sep, value = item.partition(": ")
    return (label.strip(), value.strip()) if sep and label and value else ("", item)


def _mm_triplet(value: str) -> Optional[tuple[float, float, float]]:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)", value)
    return tuple(float(x.replace(",", ".")) for x in m.groups()) if m else None  # type: ignore[return-value]


def _mm(value: str) -> Optional[float]:
    v = first_num(str(value).replace(",", "."))
    return v


def eu_parse_product(cfg: EuSite, model: str, url: str, pdp: dict, price: Optional[float],
                     translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    """ProductRecord + RawSpec rows from parse_pdp() data. Labels/values of German pages are normalised to English
    through the i18n translator (extra_specs 'Section > Label'); RawSpec keeps the source text. `price` is the shop price
    (None -> the schema.org price of the page)."""
    sections = pdp["sections"]
    if not sections:
        raise ValueError(f"no specification table on the {cfg.brand} page of {model}")
    if translator is None and cfg.lang != "en":
        import i18n
        translator = i18n.get(cfg.lang)
    spec_rows: list[tuple[str, str, str]] = []   # (section, label, value) in source language
    features: list[str] = []
    for title, items in sections:
        if _norm(title) in _FEATURE_SECTIONS:
            features += items
            continue
        for it in items:
            label, value = _split_item(it)
            if label:
                spec_rows.append((title, label, value))
    secs = list(dict.fromkeys(t for t, _, _ in spec_rows))
    if translator is not None:
        sec_en = dict(zip(secs, [_SECTION_EN.get(_norm(t)) or translator.translate_key(t) for t in secs]))
        labels = list(dict.fromkeys(l for _, l, _ in spec_rows))
        lab_en = dict(zip(labels, translator.translate_many(labels, "label")))
        vals = list(dict.fromkeys(v for _, _, v in spec_rows))
        val_en = dict(zip(vals, translator.translate_many(vals, "value")))
        pod = translator.translate_many(features, "value")
    else:
        sec_en, lab_en, val_en, pod = {t: t for t in secs}, {}, {}, list(features)
    table: dict[str, str] = {}
    for sec, label, value in spec_rows:
        put_spec(table, f"{sec_en[sec]} > {lab_en.get(label, label)}", val_en.get(value, value))
    first: dict[str, str] = {}
    for _sec, label, value in spec_rows:
        first.setdefault(label, value)

    def pick(pattern: str) -> Optional[str]:
        return next((v for k, v in first.items() if re.search(pattern, k, re.I)), None)

    h = w = d = None
    dims = next((_mm_triplet(v) for k, v in first.items()
                 if re.match(r"(Gerätemaße|Dimensions)\b", k, re.I) and re.search(r"HxBxT|HxWxD", k, re.I)), None)
    if dims:
        h, w, d = dims
    else:
        h, w, d = (_mm(pick(p) or "") for p in (r"^Gerätehöhe", r"^Gerätebreite", r"^Gerätetiefe"))
        wd = next((v for k, v in first.items() if re.match(r"Dimensions\b", k, re.I) and re.search(r"WxD", k, re.I)), None)
        m = re.match(r"\s*(\d+)\s*x\s*(\d+)", wd or "")
        if m and not w:
            w, d = float(m.group(1)), float(m.group(2))
    mm_in = lambda v: round(units.mm_to_in(v), 1) if v else None  # noqa: E731
    kg = _mm(pick(r"^(Nettogewicht|Net Weight)") or "")
    klass = units.eu_energy_class(pick(r"^(Energieeffizienzklasse|Energy Rating|Energy class)"))
    if klass:
        put_spec(table, "Energy > EU energy class", klass)
    volt = pick(r"^(Volt|Spannung|Voltage)")
    freq = pick(r"^(Frequenz|Frequency)")
    conn_key = next((k for k in first if re.search(r"connectivity|wlan|wi-?fi|konnektivität", k, re.I)), None)
    wifi = yes_no(first[conn_key]) if conn_key else None
    color = pick(r"^(Farbe|Main Colour|Colour)$")
    record = ProductRecord(
        brand=cfg.brand, model_number=model, product_name=pdp["heading"] or model, category="cooking",
        subcategory=classify_eu(pdp.get("category_path") or "", pdp["heading"],
                                f"{pdp.get('description') or ''} {pick(r'^(Bauart|Product Type)$') or ''}"),
        finish_color=(val_en.get(color, color) if color else None), product_url=url, price_usd=None, region="eu",
        country=cfg.country, currency=cfg.currency, price_local=price if price else pdp.get("schema_price"),
        height_in=mm_in(h), width_in=mm_in(w), depth_in=mm_in(d), weight_lb=round(units.kg_to_lb(kg), 1) if kg else None,
        voltage_v=(clean(volt) or None) if volt else None, frequency_hz=_mm(freq or ""),
        wifi_supported=wifi, wifi_evidence=f"{conn_key}: {first[conn_key]}" if conn_key else None,
        pod_features=list(dict.fromkeys(f for f in pod if f)), extra_specs=table,
        image_url=eu_main_image(cfg, pdp), **(pdp.get("signals") or {}))
    raw = [RawSpec(brand=cfg.brand, model_number=model, source="web", section=sec, key=label, value=value)
           for sec, label, value in spec_rows]
    return record, raw


def eu_main_image(cfg: EuSite, pdp: dict) -> Optional[str]:
    """Hero image: first schema.org image of the page. The site host (Akamai) refuses plain HTTP clients, so the
    '/services/eml/transform/<format>/<asset>' path is mapped to the same asset on the brand CDN
    (electrolux.bynder.com/transform/<format>/<asset>, the host the shop API itself lists in `mediaFiles`)."""
    for u in pdp.get("images") or []:
        url = urljoin(cfg.base + "/", u) if str(u).startswith("/") else str(u)
        if urlparse(url).scheme != "https" or not host_in(url, cfg.hosts + (CDN_HOST,)):
            continue
        m = re.fullmatch(r"/services/eml/transform/([A-Za-z0-9_]+)/([A-Za-z0-9._/-]+)", urlparse(url).path)
        if m and host_in(url, cfg.hosts):
            return f"https://{CDN_HOST}/transform/{m.group(1)}/{m.group(2)}"
        return url
    return None


def eu_model_from_url(cfg: EuSite, url: str) -> tuple[str, str]:
    """(MODEL, category path) from https://<host>/kitchen/cooking/<...>/<model>/"""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not host_in(url, cfg.hosts) or len(parts) < 4 or parts[:2] != ["kitchen", "cooking"]
            or not _CODE.match(parts[-1])):
        raise ValueError(f"not a supported {cfg.brand} {cfg.country.upper()} product URL: {url}")
    return parts[-1].upper(), "/".join(parts[:-1])


def _eu_shop_item(cfg: EuSite, s: Session, pnc: str) -> Optional[dict]:
    """The listing item of one product (price, category, description exactly as discover() sees them) via the shop
    API; None when there is no shop API or the product is not listed."""
    if not cfg.api_site or not re.fullmatch(r"\d{6,12}", pnc or ""):
        return None
    q = quote(f":relevance:code:{pnc}", safe=":")
    try:
        d = s.json(f"{cfg.base}/external/commerce/ccv2/occ/{cfg.api_site}/products/search/?query={q}&currentPage=0"
                   f"&pageSize=1&fields={quote(_API_FIELDS, safe='')}")
    except (RuntimeError, ValueError, PlaywrightError):
        return None
    items = d.get("products") or []
    return items[0] if items else None


def eu_scrape(cfg: EuSite, url: str, translator=None) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model, path = eu_model_from_url(cfg, url)
    with session(cfg.base + "/", cfg.hosts) as s:
        page_html = s.rendered(url, ".full-specifications__item")
        pdp = parse_pdp(page_html)
        item = _eu_shop_item(cfg, s, pdp["pnc"])
    if pdp["model"] != model:
        raise ValueError(f"{cfg.brand} page shows {pdp['model']!r}, expected {model!r}")
    # same inputs as discover(): the listing's category + name/description when the shop API knows the product
    pdp["category_path"] = str(((item or {}).get("categoryFallBack") or {}).get("categoryFallBackCode") or path)
    pdp["description"] = clean((item or {}).get("description"))
    price = eu_price(item) if item else None
    pdp["signals"] = {**(pdp.get("signals") or {}), **(eu_item_signals(item) if item else {})}   # listing values are unrounded
    record, raw = eu_parse_product(cfg, model, url, pdp, price, translator)
    wanted = [(t, u) for t, u in (("Manual", pdp["manual_url"]), ("EnergyGuide", pdp["sheet_url"]))
              if urlparse(u).scheme == "https"]
    return record, fetch_docs(cfg.brand, model, wanted), raw
