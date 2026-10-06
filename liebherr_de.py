"""Liebherr Germany adapter: household refrigerators (https://www.liebherr.com/de-de, Liebherr-Hausgeraete).

Platform facts (found by inspection, 2026-10; home.liebherr.com redirects to www.liebherr.com/<lang>-<cc>):
  The site is a Next.js app; every page embeds its data as `<script id="__NEXT_DATA__">` JSON (plain HTTP, no browser, no
  token). robots.txt (checked 2026-10-06) disallows only /WebResource.axd, /Data/..., partsshop, /wip/ -- nothing we read.
  discover(): the leaf product listing pages (PLP, e.g. /de-de/gefrier-kuehlschraenke/freistehende-side-by-side-
      kuehlschraenke-2451453) carry `pageProps.hydrationData.plpData.products` (full product objects incl. price and the
      spec tables) and `total`; further pages are `?p=N`. The tile/overview pages (side-by-side-kuehlschraenke-... etc.)
      have no product data and are not used.
  scrape(): the PDP (/de-de/p/<idUrl>-<pdpId>) holds `pageModel.components.additionalContent[0].product`: product-level
      highlights (combined size/volume), `components[]` = the appliances of the product (a Side-by-Side is a fridge unit
      plus a freezer unit) each with `attributeGroups` (German spec table), `assetGroups` (PDFs) and the same object is the
      listing row. The JSON-LD Product block is not needed.
Prices: `shopInformation.priceGross` = list price in EUR incl. VAT (shown to every visitor; no member/card prices exist);
  stored as price_local, `price_usd` stays None, no currency conversion.
Not offered by Liebherr / not supported here: top_freezer (no such Liebherr line), freezers and wine/beverage/outdoor
  coolers (not refrigerators of the catalog), washers and cooking (Liebherr makes none). A freestanding single-door fridge
  (Standkuehlschrank) fits no catalog sub and is left out (classify() returns None).
German -> English comes from i18n.get('de') (glossary, cache, optional local LLM); the original German is kept as RawSpec.
"""
import json
import os
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

import catalog
import common
import i18n
import units
from catalog import Candidate
from common import UA, download_pdf, looks_blocked
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Liebherr"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
BASE = "https://www.liebherr.com"
PLP_BASE = BASE + "/de-de/gefrier-kuehlschraenke/"
MAX_DOCS = 3
DELAY_S = 1.0              # minimum gap between any two requests
MAX_LIST_PAGES = 6         # safety bound per listing (10 or 50 products per page)
MAX_REDIRECTS = 5

PAGE_HOSTS = ("liebherr.com",)   # www.liebherr.com pages; assets-cdn.liebherr.com / www-assets.liebherr.com files
_HEADERS = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9", "Accept": "text/html,application/xhtml+xml"}

# Scope decision (coordinator, 2026-10-06): only cooking sub keys are in scope for now and Liebherr makes no cooking
# appliances, so nothing is exposed. The refrigerator code below works (live-verified) but is parked: re-enable by
# setting this to set(SUB_SOURCES) when refrigerators come into scope.
SUPPORTED_SUBCATEGORIES: set[str] = set()
# sub key -> leaf listing pages (slug under PLP_BASE) that can contain it; classify() decides what really belongs
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "french_door": ("french-door-kuehlschraenke-7028531",),
    "side_by_side": ("freistehende-side-by-side-kuehlschraenke-2451453",),
    "bottom_freezer": ("freistehende-kuehl-gefrierkombinationen-2451426",),
    "built_in": ("einbaukuehlschraenke-2451437", "einbau-kuehl-gefrierkombinationen-2451429",
                 "einbau-side-by-side-kuehlschraenke-2451457"),
    "compact": ("freistehende-kuehlschraenke-2451434", "einbaukuehlschraenke-2451437"),
}
_DOOR_STYLE = {"french_door": "French Door", "side_by_side": "Side-by-Side", "bottom_freezer": "Bottom Freezer",
               "built_in": "Built-in", "compact": "Compact"}


class LiebherrPageError(RuntimeError):
    """Liebherr page did not have the expected structure (site changed) or was refused."""


class _Blocked(LiebherrPageError):
    """Bot protection / rate limit answered (403, 429, challenge text)."""


# ---------------------------------------------------------------- hosts, names, politeness
def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme == "https" and not (u.username or u.password) and any(h == s or h.endswith("." + s) for s in suffixes)


def _check_url(url: str) -> None:
    """https + liebherr.com (exact suffix) + public IP; anything else is refused before any request."""
    if not _host_in(url, PAGE_HOSTS):
        raise LiebherrPageError(f"refusing url outside liebherr.com over https: {url[:120]}")
    problem = common._url_problem(url, PAGE_HOSTS)
    if problem:
        raise LiebherrPageError(f"refusing url ({problem}): {url[:120]}")


def _safe_name(m: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", m).strip(".") or "unknown"


_last_request = 0.0


def _polite() -> None:
    global _last_request
    wait = DELAY_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


_TR = None   # tests swap in a deterministic stand-in; None = the shared i18n 'de' translator


def _de():
    return _TR if _TR is not None else i18n.get("de")


# ---------------------------------------------------------------- fetching (HTTP first, Playwright fallback)
def _browser_modes() -> list[tuple[bool, bool]]:
    """(headless, new-headless channel) attempts: FRIDGE_BROWSER_MODE auto = new headless, headless, visible."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [(True, True), (True, False)]
    if mode == "visible":
        return [(False, False)]
    return [(True, True), (True, False), (False, False)]


def _browser_only() -> bool:
    return os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower() in ("headless", "visible")


_REJECT = ("Alle ablehnen", "Ablehnen", "Nur notwendige", "Nur erforderliche", "Reject all", "Decline")
_ACCEPT = ("Alle akzeptieren", "Alle zulassen", "Akzeptieren", "Accept all", "Accept")


def _consent(pg, accept: bool = False) -> bool:
    """Best-effort cookie banner click: decline non-essential by default, accept only when the page is blocked."""
    for label in (_ACCEPT if accept else _REJECT):
        try:
            pg.get_by_role("button", name=label, exact=True).first.click(timeout=1200)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:  # noqa: BLE001 - a banner click must never break scraping
            continue
    return False


class _Fetcher:
    """HTTP for everything; when blocked (or FRIDGE_BROWSER_MODE forces it) a real Chromium loads the page. One
    instance per discover()/scrape() call; close() in a finally."""

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._use_browser = _browser_only()

    def _http(self, url: str) -> tuple[str, str]:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(cur)
            _polite()
            r = requests.get(cur, headers=_HEADERS, timeout=60, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur = urljoin(cur, r.headers["Location"])
                continue
            if r.status_code in (403, 429):
                raise _Blocked(f"HTTP {r.status_code} from {cur[:100]}")
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return cur, r.text
        raise LiebherrPageError(f"more than {MAX_REDIRECTS} redirects from {url[:100]}")

    def _open(self, headless: bool, channel: bool) -> None:
        self.close()
        self._pw = sync_playwright().start()
        if channel:
            self._browser = self._pw.chromium.launch(channel="chromium", headless=True)
        else:
            self._browser = common.launch_browser(self._pw, headless=headless)
        self._page = self._browser.new_context(user_agent=UA, locale="de-DE").new_page()

    def close(self) -> None:
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._page = None

    def _browser_html(self, url: str) -> tuple[str, str]:
        last: Exception | None = None
        for headless, channel in _browser_modes():
            try:
                if self._page is None:
                    self._open(headless, channel)
                _check_url(url)
                _polite()
                pg = self._page
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                _check_url(pg.url)
                _consent(pg)
                try:
                    pg.wait_for_function("!!document.getElementById('__NEXT_DATA__')", timeout=15000)
                except PlaywrightTimeoutError:
                    _consent(pg, accept=True)   # a banner may be blocking content; accept only now
                    pg.wait_for_timeout(3000)
                _check_url(pg.url)
                html = pg.content()
                if looks_blocked(resp.status if resp else None, pg.inner_text("body")):
                    raise _Blocked(f"blocked in browser at {url[:100]}")
                return pg.url, html
            except Exception as e:  # noqa: BLE001 - blocked / navigation failure -> next mode
                last = e
                print(f"liebherr_de: browser (headless={headless}, channel={channel}) failed: {e}", file=sys.stderr)
                self.close()
        raise LiebherrPageError(f"browser fetch failed: {last}")

    def html(self, url: str) -> tuple[str, str]:
        """(final_url, html). Final url is host-checked on every hop."""
        if not self._use_browser:
            try:
                final, text = self._http(url)
                if looks_blocked(200, text):
                    raise _Blocked("page looks like a bot challenge")
                return final, text
            except _Blocked as e:
                print(f"liebherr_de: {e}; falling back to browser", file=sys.stderr)
                self._use_browser = True
        return self._browser_html(url)


# ---------------------------------------------------------------- page data
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> dict:
    m = _NEXT_RE.search(html or "")
    if not m:
        raise LiebherrPageError("page has no __NEXT_DATA__ (site changed or blocked?)")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        raise LiebherrPageError("__NEXT_DATA__ is not JSON") from e
    if not isinstance(data, dict):
        raise LiebherrPageError("unexpected __NEXT_DATA__ shape")
    return data


def plp_data(html: str) -> dict:
    """hydrationData.plpData of a leaf listing page: {'products': [...], 'total': n, 'pageSize': n, ...}."""
    pp = (next_data(html).get("props") or {}).get("pageProps") or {}
    plp = (pp.get("hydrationData") or {}).get("plpData")
    if not isinstance(plp, dict) or not isinstance(plp.get("products"), list):
        raise LiebherrPageError("listing page has no plpData.products (tile page or site changed)")
    return plp


def pdp_product(html: str) -> dict:
    """The product object of a PDP (components[], attributeGroups, assetGroups, shopInformation ...)."""
    pm = ((next_data(html).get("props") or {}).get("pageProps") or {}).get("pageModel") or {}
    for item in (pm.get("components") or {}).get("additionalContent") or []:
        if isinstance(item, dict) and isinstance(item.get("product"), dict):
            return item["product"]
    raise LiebherrPageError("PDP has no product object (site changed or not a product page)")


# ---------------------------------------------------------------- spec rows
def _attr_iter(groups, path=()):
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "")
        for a in g.get("attributes") or []:
            if isinstance(a, dict) and a.get("name"):
                yield path + (name,), g.get("groupTypeId") or "", a
        yield from _attr_iter(g.get("children"), path + (name,))


def _value_text(a: dict) -> str:
    v = str(a.get("formattedValue") if a.get("formattedValue") is not None else "").strip()
    unit = str(a.get("unitOfMeasurement") or "").strip()
    if v and unit and unit.lower() not in v.lower():
        v = f"{v} {unit}"
    return v


def _comps(product: dict) -> list[dict]:
    return [c for c in product.get("components") or [] if isinstance(c, dict)]


def spec_rows(product: dict) -> list[tuple[str, str, str, str, str]]:
    """[(section_de, label_de, value_de, unit_tag, kind)] in page order. kind 'hl' = product-level highlights (combined
    size/volume of the whole product), 'comp' = a component (appliance) table, 'prod' = other product-level rows
    (only when the product has no component). unit_tag = the component's model name when there are several."""
    rows: list[tuple[str, str, str, str, str]] = []
    comps = _comps(product)
    multi = len(comps) > 1
    for path, gtype, a in _attr_iter(product.get("attributeGroups")):
        if gtype in ("product_highlights", "product_teasers"):   # PDP / listing flavour of the combined size + volume
            kind = "hl"
        elif comps:
            continue        # product-level 'Technische Daten' only repeats article number / manufacturer of the components
        else:
            kind = "prod"
        rows.append((path[-1] or path[0], str(a["name"]).strip(), _value_text(a), "", kind))
    for c in comps:
        tag = str(c.get("displayName") or "").strip() if multi else ""
        for path, gtype, a in _attr_iter(c.get("attributeGroups")):
            if gtype.startswith(("component_product_teasers", "component_product_highlights")):
                continue   # repeats size / volume / noise of the technical data
            rows.append((path[-1] or path[0], str(a["name"]).strip(), _value_text(a), tag, "comp"))
    return [r for r in rows if r[2] and r[2] not in ("—", "-")]


def _first(rows, label: str, kinds: tuple[str, ...] = ("comp", "prod", "hl")) -> Optional[str]:
    want = i18n.fold(label)
    for kind in kinds:
        for _sec, lab, val, _tag, k in rows:
            if k == kind and i18n.fold(lab) == want:
                return val
    return None


def _all(rows, label: str, kinds: tuple[str, ...] = ("comp", "prod")) -> list[str]:
    want = i18n.fold(label)
    return [val for _s, lab, val, _t, k in rows if k in kinds and i18n.fold(lab) == want]


def _n(s: Any) -> Optional[float]:
    return units.parse_eu_number(s) if s not in (None, "") else None


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 2))


def _dims_mm(text: Optional[str]) -> Optional[tuple[float, float, float]]:
    """'1.855 / 1.226 / 675 mm' | '185,5 / 59,7 / 67,5 cm' (Hoehe / Breite / Tiefe) -> (h, w, d) in mm."""
    if not text:
        return None
    parts = [p for p in str(text).split("/")]
    if len(parts) != 3:
        return None
    nums = [units.parse_eu_number(re.sub(r"[a-zA-Z]+", "", p)) for p in parts]
    if any(x is None for x in nums):
        return None
    k = 10.0 if re.search(r"\bcm\b", str(text), re.I) else 1.0
    return tuple(round(x * k, 1) for x in nums)  # type: ignore[return-value]


_FINISH = (("edelstahl|steel|inox|stainless", "stainless"), (r"wei[sß]|white|blanc", "white"),
           (r"schwarz|black|noir", "black"))


def _finish(text: Optional[str]) -> Optional[str]:
    for pat, key in _FINISH:
        if text and re.search(pat, text, re.I):
            return key
    return None


def facts(product: dict) -> dict:
    """Typed facts in standard units from one product object (listing row or PDP): used for Candidate.attrs and the
    ProductRecord, so both agree. Missing = unknown (key absent)."""
    rows = spec_rows(product)
    comps = _comps(product)
    out: dict[str, Any] = {}
    whole = ("hl", "comp", "prod") if len(comps) <= 1 else ("hl",)   # a component alone is not the whole product
    dims = _dims_mm(_first(rows, "Außenmaße: Höhe / Breite / Tiefe", whole))
    if dims:
        h, w, d = dims
        out.update(height_in=round(units.mm_to_in(h), 2), width_in=round(units.mm_to_in(w), 2),
                   depth_in=round(units.mm_to_in(d), 2), height_mm=h, width_mm=w, depth_mm=d)
    total = _n(_first(rows, "Gesamtvolumen", whole))
    if total:
        out["capacity_total_l"] = total
        out["capacity_total_cuft"] = round(units.l_to_cuft(total), 2)
    fresh = [x for x in map(_n, _all(rows, "Volumen Kühlfächer")) if x]
    frozen = [x for x in map(_n, _all(rows, "Volumen Gefrierfächer")) if x]
    if fresh:
        out["capacity_fridge_cuft"] = round(units.l_to_cuft(sum(fresh)), 2)
    if frozen:
        out["capacity_freezer_cuft"] = round(units.l_to_cuft(sum(frozen)), 2)
    kg = [x for x in map(_n, _all(rows, "Gewicht (ohne Verpackung)")) if x]
    if kg:
        out["weight_lb"] = round(units.kg_to_lb(sum(kg)), 1)
    classes = [c for c in map(units.eu_energy_class, _all(rows, "Energieeffizienzklasse")) if c]
    if len(classes) == 1 or (classes and len(set(classes)) == 1):
        out["energy_class"] = classes[0]
    kwh = [x for x in map(_n, _all(rows, "Energieverbrauch pro Jahr")) if x]
    if len(comps) <= 1 and len(kwh) == 1:
        out["energy_kwh_year"] = kwh[0]
    fin = _finish(_first(rows, "Farbe Tür") or _first(rows, "Material Tür"))
    if fin:
        out["finish"] = fin
    volt = _first(rows, "Spannung")
    if volt and (m := re.search(r"(\d{3})(?:\s*[-–]\s*(\d{3}))?", volt)):
        out["voltage_v"] = f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)
    hz = _first(rows, "Frequenz")
    if hz and (m := re.search(r"\d{2}", hz)):
        out["frequency_hz"] = float(m.group(0))
    smart = _first(rows, "SmartDeviceBox")
    if smart:
        out["smartdevice"] = smart
        if re.search(r"integriert|^ja\b|vorhanden", smart, re.I):
            out["wifi"] = True
    shop = product.get("shopInformation") or {}
    price = shop.get("priceGross")
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        out["price_eur"] = float(price)
    return out


# ---------------------------------------------------------------- classification (single source of truth)
_NOT_A_FRIDGE = re.compile(r"wein|getränke|outdoor|humidor|gefrierschrank|gefriertruhe|gefrierschränke|gefrierger", re.I)


def classify(levels: Any, type_de: Any) -> Optional[str]:
    """Catalog sub key of one Liebherr household product, or None (freezers, wine/beverage/outdoor coolers, a plain
    freestanding single-door fridge, anything outside Liebherr's refrigerator tree). `levels` = hierarchyLevels
    (list of {'name':...}) or their names, `type_de` = the product's `type` text ('Side-by-Side-Kombination',
    'Integrierbarer Kuehlschrank mit BioFresh' ...). discover() and scrape() both call this."""
    names = [str(x.get("name") if isinstance(x, dict) else x) for x in (levels or [])]
    joined = " | ".join(names).lower()
    t = str(type_de or "").lower()
    if "k-einbau" in joined:
        built = True
    elif "k-freistehend" in joined:
        built = False
    else:
        return None
    if _NOT_A_FRIDGE.search(t) and "kühl-gefrier" not in t and "side-by-side" not in t and "french" not in t:
        return None
    if built:
        if "unterbau" in t or "unterbaufähig" in joined:
            return None if re.search(r"getränke|outdoor", t) else "compact"
        return "built_in"
    if "french" in t:
        return "french_door"
    if "side-by-side" in t:
        return "side_by_side"
    if "tisch" in t or "tisch-ks" in joined:
        return "compact"
    if "kühl-gefrierkombination" in t:
        return "bottom_freezer"
    return None


def _classify_product(product: dict) -> Optional[str]:
    return classify(product.get("hierarchyLevels"), product.get("type"))


# ---------------------------------------------------------------- discover
def _abs(path: Any) -> Optional[str]:
    if not isinstance(path, str) or not path:
        return None
    url = urljoin(BASE + "/", path)
    return url if _host_in(url, PAGE_HOSTS) else None


def candidate_from(product: dict, sub: str) -> Optional[Candidate]:
    url = _abs(product.get("pdpUrl"))
    model = str(product.get("productSetName") or product.get("displayName") or "").strip()
    if not url or not model:
        return None
    f = facts(product)
    attrs = {k: f[k] for k in ("capacity_total_cuft", "width_in", "height_in", "depth_in", "energy_kwh_year",
                               "energy_class", "finish", "wifi") if k in f}
    name = " ".join(x for x in (str(product.get("displayName") or model), str(product.get("type") or "")) if x)
    return Candidate(brand=BRAND, model_number=model, name=name, url=url, category=catalog.major_of(sub),
                     subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None,
                     price_local=f.get("price_eur"), attrs=attrs, attrs_src={k: "listing" for k in attrs})


def parse_listing(products: list[dict], sub: str, limit: int = 30) -> list[Candidate]:
    """plpData.products -> Candidates that classify() files under `sub` (deduped by PDP url, off-domain urls dropped)."""
    out, seen = [], set()
    for p in products:
        if not isinstance(p, dict) or _classify_product(p) != sub:
            continue
        c = candidate_from(p, sub)
        if c is None or c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"Liebherr DE adapter does not support sub category {subcategory!r}")
    f = _Fetcher()
    found: dict[str, Candidate] = {}
    try:
        for slug in SUB_SOURCES[subcategory]:
            got = 0
            for page in range(1, MAX_LIST_PAGES + 1):
                url = PLP_BASE + slug + ("" if page == 1 else f"?p={page}")
                final, html = f.html(url)
                _check_url(final)
                plp = plp_data(html)
                products = plp["products"]
                got += len(products)
                for c in parse_listing(products, subcategory, limit):
                    found.setdefault(c.url, c)
                if len(found) >= limit or not products or got >= int(plp.get("total") or 0):
                    break
            if len(found) >= limit:
                break
    finally:
        f.close()
    print(f"liebherr_de: discover({subcategory}): kept {min(len(found), limit)}", file=sys.stderr)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- PDP parsing
def _unique(key: str, taken: dict) -> str:
    if key not in taken:
        return key
    n = 2
    while f"{key} ({n})" in taken:
        n += 1
    return f"{key} ({n})"


def _tr_pairs(rows) -> tuple[list[str], list[str], list[str]]:
    """English section, label and value for every row (one batched call per kind)."""
    tr = _de()
    secs = sorted({r[0] for r in rows})
    sec_en = dict(zip(secs, tr.translate_many(secs, "label")))
    labels = tr.translate_many([r[1] for r in rows], "label")
    values = tr.translate_many([r[2] for r in rows], "value")
    return [sec_en[r[0]] for r in rows], labels, values


_LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _jsonld_description(html: str) -> str:
    """description of the schema.org Product block ('Typ, Außenmaße ..., Gesamtvolumen ..., EasyFresh, NoFrost')."""
    for m in _LD_RE.finditer(html or ""):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product" and isinstance(d.get("description"), str):
            return d["description"]
    return ""


def _features(text: str) -> list[str]:
    """'Typ, Maße ..., EasyFresh, NoFrost, easyfresh' -> feature words (size/volume parts dropped, case-insensitive dedupe)."""
    out: list[str] = []
    for part in (p.strip() for p in str(text or "").split(",")):
        if part and not re.match(r"(außenmaße|gesamtvolumen)", part, re.I) and part.lower() not in (o.lower() for o in out):
            out.append(part)
    return out


def _image(product: dict) -> Optional[str]:
    for g in product.get("assetGroups") or []:
        for a in (g.get("assets") or []) if isinstance(g, dict) else []:
            if a.get("assetType") == "product_image" and isinstance(a.get("url"), str) and _host_in(a["url"], PAGE_HOSTS):
                return a["url"]
    return None


_DOC_TYPES = (("operating_instructions", "Manual"), ("assembly_and_installation_instruction", "Installation"),
              ("spec_sheet", "SpecSheet"))


def parse_documents(product: dict) -> list[tuple[str, str]]:
    """[(doc_type, https PDF url)] from the product downloads: operating instructions, installation instructions,
    data sheet; PDFs only, unique urls, at most MAX_DOCS."""
    found: dict[str, str] = {}
    for g in product.get("assetGroups") or []:
        for a in (g.get("assets") or []) if isinstance(g, dict) else []:
            url = a.get("url")
            if a.get("mimeTypeDisplayName") != "PDF" or not isinstance(url, str) or not _host_in(url, PAGE_HOSTS):
                continue
            for atype, dtype in _DOC_TYPES:
                if a.get("assetType") == atype and dtype not in found:
                    found[dtype] = url
    out = [(dt, found[dt]) for _, dt in _DOC_TYPES if dt in found]
    return out[:MAX_DOCS]


def parse_pdp(html: str, url: str) -> tuple[ProductRecord, list[RawSpec], dict]:
    """-> (ProductRecord, [RawSpec with the ORIGINAL German], info{'documents'}). Raises LiebherrPageError for a page
    that is not a product page and ValueError for a product family this adapter does not cover."""
    product = pdp_product(html)
    model = str(product.get("productSetName") or product.get("displayName") or "").strip()
    if not model:
        raise LiebherrPageError("PDP has no model name")
    sub = _classify_product(product)
    if not sub:
        raise ValueError(f"not a supported Liebherr refrigerator ({product.get('type')!r})")
    rows = spec_rows(product)
    if not rows:
        raise LiebherrPageError("PDP spec table is empty")
    f = facts(product)
    tr = _de()
    sec_en, lab_en, val_en = _tr_pairs(rows)
    shown = [i for i, r in enumerate(rows) if r[4] != "hl" or len(_comps(product)) > 1]
    extra: dict[str, str] = {}
    for i in shown:
        sec, _lab, _val, tag, _k = rows[i]
        section = f"{sec_en[i]} [{tag}]" if tag else sec_en[i]
        value = val_en[i]
        if lab_en[i].lower().startswith("energy efficiency class") and (cls := units.eu_energy_class(rows[i][2])):
            value = cls   # 'EU class D', never an ENERGY STAR equivalent
        extra[_unique(f"{section} > {lab_en[i]}", extra)] = value
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=r[0] + (f" [{r[3]}]" if r[3] else ""),
                   key=r[1], value=r[2]) for r in rows]

    cur: dict[str, str] = {}
    if "capacity_total_l" in f:
        cur["Total capacity (L)"] = _fmt(f["capacity_total_l"])
    if "width_mm" in f:
        cur.update({"Width (mm)": _fmt(f["width_mm"]), "Height (mm)": _fmt(f["height_mm"]),
                    "Depth (mm)": _fmt(f["depth_mm"])})
    if "energy_class" in f:
        cur["Energy efficiency class (EU)"] = f["energy_class"]
    shop = product.get("shopInformation") or {}
    if "price_eur" in f:
        cur["Price incl. VAT (EUR)"] = _fmt(f["price_eur"])
    special = shop.get("specialPriceGross")
    if isinstance(special, (int, float)) and special > 0 and shop.get("specialPriceFromTo"):
        cur["Special price incl. VAT (EUR)"] = _fmt(float(special))
    type_de = str(product.get("type") or "")
    type_en = tr.translate_many([type_de], "value")[0] if type_de else ""
    if type_en:
        cur["Product type"] = type_en
    feats_de = [x for x in _features(_jsonld_description(html)) if x.lower() != type_de.lower()]
    pod = tr.translate_many(feats_de, "value") if feats_de else []

    smart = f.get("smartdevice")
    name = str(product.get("displayName") or model)
    fridge_like = str(product.get("type") or "")
    product_rec = ProductRecord(
        brand=BRAND, model_number=model, product_name=f"{name} {fridge_like}".strip(), product_url=url,
        category=catalog.major_of(sub), subcategory=sub, door_style=_DOOR_STYLE.get(sub),
        finish_color=None, price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY,
        price_local=f.get("price_eur"),
        capacity_total_cuft=f.get("capacity_total_cuft"), capacity_fridge_cuft=f.get("capacity_fridge_cuft"),
        capacity_freezer_cuft=f.get("capacity_freezer_cuft"),
        width_in=f.get("width_in"), height_in=f.get("height_in"), depth_in=f.get("depth_in"),
        weight_lb=f.get("weight_lb"), voltage_v=f.get("voltage_v"), frequency_hz=f.get("frequency_hz"),
        energy_kwh_year=f.get("energy_kwh_year"), energy_star=None,
        ice_maker=True if re.search(r"icemaker|eiswürfel", fridge_like, re.I) else None,
        water_dispenser=True if re.search(r"wasser", fridge_like, re.I) else None,
        wifi_supported=f.get("wifi"), wifi_evidence=(f"Liebherr spec table: SmartDeviceBox = {smart}" if smart else None),
        pod_features=pod, extra_specs={**extra, **cur}, image_url=_image(product),
    )
    fin_de = _first(rows, "Farbe Tür")
    if fin_de:
        product_rec.finish_color = tr.translate_many([fin_de], "value")[0]
    return product_rec, raw, {"documents": parse_documents(product)}


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_url(url)
    f = _Fetcher()
    try:
        final_url, html = f.html(url)
        _check_url(final_url)
    finally:
        f.close()
    product, raw, info = parse_pdp(html, url)
    docs: list[DocumentRecord] = []
    for dt, link in info["documents"]:
        if not _host_in(link, PAGE_HOSTS):
            print(f"liebherr_de: skipped {dt}: PDF url not https on liebherr.com ({link[:100]})", file=sys.stderr)
            continue
        _polite()
        d = download_pdf(BRAND, _safe_name(product.model_number), dt, link)
        if not d:
            continue
        if any(x.sha256 == d.sha256 for x in docs):
            (common.ROOT / d.local_path).unlink(missing_ok=True)
            continue
        docs.append(d)
    return product, docs, raw
