"""Miele Germany adapter: cooking appliances (https://www.miele.de, the Miele online shop).

Scope (coordinator decision 2026-10-06): cooking sub keys only. Refrigerators / washers exist on miele.de (categories
1014603 ..., 1015696 ...) but are NOT implemented or exposed here.

Platform facts (found by inspection, 2026-10; miele.de is a Nuxt 3 app that server-renders its data):
  Bot protection: Akamai answers plain HTTP (curl/requests) and Playwright's classic headless Chromium with
  "The requested URL was rejected" (HTTP 403). The NEW headless Chromium (`channel="chromium"`) passes, so no visible
  window is needed; the browser order is new-headless -> classic headless -> visible (FRIDGE_BROWSER_MODE = auto |
  headless | visible). Nothing is bypassed: normal page loads of public pages, 1 request per second at most.
  robots.txt (2026-10-06) disallows /cart, checkout, compare, account, wishlist, '*&query=*', /pmedia/, content strings,
  '*/products/price-and-stock*' and '*/support/using-your-appliance*'. This adapter reads only category and product pages
  and NEVER calls the price-and-stock API (prices come from the server-rendered page data).
  discover(): /category/<id>/<slug>[?page=N] (N from 1; 24 products per page). `<script id="__NUXT_DATA__">` holds a
      devalue-encoded payload; `data["category-data-<id>-<hash>"].products` = {products: [...], currentPage,
      totalPageCount, totalProductCount}. A row has name (model, e.g. 'H 2465 B ACTIVE'), designTypeName ('Backofen',
      'Gaskochfeld', 'Einbau-Mikrowellengeraet' ...), dataLayer.category ('APPLIANCE/MDA/Hobs/Gas hobs'), salesPrice,
      pdpUrl. One model exists once per colour (separate material numbers, same name) -> deduped by model name.
  scrape(): /product/<material number>/<slug> (redirects to the colour slug); `data["product-data-<code>"]` has
      techspecs (sections of name/values, 'TRUE' = feature present), documentGroups (PDF links on media.miele.com),
      primaryProductImage, price/salesPrice, connectedAppliance.
Prices: salesPrice.value = the shop price in EUR incl. VAT shown to every visitor (no member/card prices); stored as
  price_local, price_usd None, no conversion. If a product shows no price, price_local stays None.
Classification (classify(), shared by discover and scrape): 'Mikrowelle' in the type name -> microwave (Einbau-/Stand-
  Mikrowellengeraet) or sco (oven / steamer WITH microwave); '...backofen' (incl. Dampfbackofen) -> electric_oven; hobs
  (category 'Hobs') -> gas_cooktop ('Gaskochfeld'), induction ('Induktion...'), radiant ('Elektrokochfeld'). Left out:
  pure steamers (Dampfgarer), cookers ('Herd', electric range, hob type not stated), combi-set modules
  (ProLine/SmartLine-Element). Not offered by Miele: otr, gas_oven (no gas ovens/ranges).
German -> English comes from i18n.get('de') (glossary, cache, optional local LLM); the original German is kept as RawSpec.
"""
import json
import os
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

import catalog
import common
import i18n
import units
from catalog import Candidate
from common import UA, download_pdf, looks_blocked
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Miele"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
BASE = "https://www.miele.de"
MAX_DOCS = 3
DELAY_S = 1.0              # minimum gap between any two page loads
MAX_LIST_PAGES = 8         # safety bound per category (24 products per page; the biggest has 7 pages)

PAGE_HOSTS = ("miele.de",)                 # shop pages; media.miele.com (images / PDFs) is checked by common's allow-list
FILE_HOSTS = ("miele.com", "miele.de")

SUPPORTED_SUBCATEGORIES = {"microwave", "sco", "electric_oven", "induction", "radiant", "gas_cooktop"}
# sub key -> category pages ('<id>/<slug>') that can contain it; classify() decides what really belongs.
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("1013130/mikrowellengerate",),
    "sco": ("1022125/backofen",),
    "electric_oven": ("1022125/backofen",),
    "induction": ("1013778/kochfelder",),
    "radiant": ("1013778/kochfelder",),
    "gas_cooktop": ("1013778/kochfelder",),
}
_SUB_FUEL = {"gas_cooktop": "gas", "induction": "induction", "radiant": "electric", "electric_oven": "electric",
             "microwave": "electric", "sco": "electric"}
_FUEL_EN = {"gas": "Gas", "induction": "Induction", "electric": "Electric"}


class MielePageError(RuntimeError):
    """Miele page did not have the expected structure (site changed) or was refused."""


class _Blocked(MielePageError):
    """Bot protection answered (403, 'requested URL was rejected', challenge text)."""


# ---------------------------------------------------------------- hosts, names, politeness
def _host_in(url: str, suffixes: tuple[str, ...]) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme == "https" and not (u.username or u.password) and any(h == s or h.endswith("." + s) for s in suffixes)


def _check_url(url: str) -> None:
    """https + miele.de (exact suffix) + public IP; anything else is refused before any request."""
    if not _host_in(url, PAGE_HOSTS):
        raise MielePageError(f"refusing url outside miele.de over https: {url[:120]}")
    problem = common._url_problem(url, PAGE_HOSTS)
    if problem:
        raise MielePageError(f"refusing url ({problem}): {url[:120]}")


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


# ---------------------------------------------------------------- fetching (browser only: Akamai rejects plain HTTP)
def _browser_modes() -> list[tuple[bool, bool]]:
    """(headless, new-headless channel) attempts. auto = new headless, classic headless, visible window."""
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return [(True, True), (True, False)]
    if mode == "visible":
        return [(False, False)]
    return [(True, True), (True, False), (False, False)]


_REJECT = ("Alle ablehnen", "Ablehnen", "Nur notwendige", "Nur erforderliche", "Reject all", "Decline")
_ACCEPT = ("Alle akzeptieren", "Alle zulassen", "Akzeptieren", "Accept all", "Accept")
_REJECTED_PAGE = re.compile(r"requested url was rejected|access denied|pardon our interruption", re.I)


def _consent(pg, accept: bool = False) -> bool:
    """Best-effort cookie banner click: decline non-essential by default, accept only when the page is blocked."""
    for label in (_ACCEPT if accept else _REJECT):
        try:
            loc = pg.get_by_role("button", name=label, exact=True)
            if loc.count():
                loc.first.click(timeout=1500)
                return True
        except Exception:  # noqa: BLE001 - a banner click must never break scraping
            continue
    return False


class _Fetcher:
    """One browser per discover()/scrape() call (reused for all its pages); close() in a finally. A mode that gets
    rejected is closed and the next mode is tried; a mode that worked is kept."""

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._modes = _browser_modes()

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

    def html(self, url: str) -> tuple[str, str]:
        """(final_url, html) of a page that carries __NUXT_DATA__. The final url is host-checked."""
        last: Exception | None = None
        while self._modes:
            headless, channel = self._modes[0]
            try:
                if self._page is None:
                    self._open(headless, channel)
                _check_url(url)
                _polite()
                pg = self._page
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                _check_url(pg.url)
                body = pg.inner_text("body")
                if _REJECTED_PAGE.search(body[:400]) or (resp is not None and resp.status in (403, 429)):
                    raise _Blocked(f"HTTP {resp.status if resp else '?'} (rejected) at {url[:100]}")
                _consent(pg)
                try:
                    pg.wait_for_function("!!document.getElementById('__NUXT_DATA__')", timeout=15000)
                except PlaywrightTimeoutError:
                    _consent(pg, accept=True)   # a banner may be blocking content; accept only now
                    pg.wait_for_timeout(3000)
                _check_url(pg.url)
                html = pg.content()
                if looks_blocked(resp.status if resp else None, body):
                    raise _Blocked(f"blocked in browser at {url[:100]}")
                return pg.url, html
            except Exception as e:  # noqa: BLE001 - blocked / navigation failure -> next mode
                last = e
                print(f"miele_de: browser (headless={headless}, channel={channel}) failed: {e}", file=sys.stderr)
                self.close()
                self._modes = self._modes[1:]
        raise MielePageError(f"browser fetch failed: {last}")


# ---------------------------------------------------------------- Nuxt payload (devalue)
_NUXT_RE = re.compile(r'<script type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_SPECIAL = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}


def _resolve(arr: list, idx: Any, cache: dict, depth: int = 0) -> Any:
    """One value of a devalue array: ints are references; lists/dicts are rebuilt (shared nodes memoised)."""
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        return None if isinstance(idx, int) and not isinstance(idx, bool) else idx
    if idx in cache:
        return cache[idx]
    if depth > 400 or idx >= len(arr):
        raise MielePageError("Nuxt payload too deep / out of range")
    v = arr[idx]
    if isinstance(v, list):
        if v and isinstance(v[0], str) and v[0] in _SPECIAL:
            out = _resolve(arr, v[1], cache, depth + 1)
        elif v and isinstance(v[0], str) and v[0] in ("Set", "Map", "Date", "NuxtError", "EmptyRef", "EmptyShallowRef",
                                                      "undefined", "null"):
            out = None
        else:
            out = []
            cache[idx] = out
            out.extend(_resolve(arr, x, cache, depth + 1) for x in v)
    elif isinstance(v, dict):
        out = {}
        cache[idx] = out
        for k, x in v.items():
            out[k] = _resolve(arr, x, cache, depth + 1)
    else:
        out = v
    cache[idx] = out
    return out


def nuxt_entries(html: str, prefix: str) -> dict[str, Any]:
    """Decoded `data` entries of the page whose key starts with `prefix` ('category-data-', 'product-data-')."""
    m = _NUXT_RE.search(html or "")
    if not m:
        raise MielePageError("page has no __NUXT_DATA__ (site changed, blocked or not a Nuxt page)")
    try:
        arr = json.loads(m.group(1))
        root = arr[0]
        while isinstance(root, list) and len(root) == 2 and root[0] in _SPECIAL:   # ["ShallowReactive", <index>]
            root = arr[root[1]]
        data_idx = root["data"]
        keys = arr[data_idx]
        while isinstance(keys, list) and len(keys) == 2 and keys[0] in _SPECIAL:
            keys = arr[keys[1]]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise MielePageError("unexpected __NUXT_DATA__ shape") from e
    if not isinstance(keys, dict):
        raise MielePageError("unexpected __NUXT_DATA__ data shape")
    cache: dict = {}
    return {k: _resolve(arr, i, cache) for k, i in keys.items() if k.startswith(prefix)}


def listing_page(html: str) -> dict:
    """{'products': [rows], 'currentPage': 0-based, 'totalPageCount': n, 'totalProductCount': n} of a category page."""
    for entry in nuxt_entries(html, "category-data-").values():
        pr = entry.get("products") if isinstance(entry, dict) else None
        if isinstance(pr, dict) and isinstance(pr.get("products"), list):
            return pr
    raise MielePageError("category page has no product list (site changed or not a category page)")


def pdp_product(html: str) -> dict:
    for entry in nuxt_entries(html, "product-data-").values():
        if isinstance(entry, dict) and entry.get("name") and entry.get("techspecs") is not None:
            return entry
    raise MielePageError("product page has no product data (site changed or not a product page)")


# ---------------------------------------------------------------- classification (single source of truth)
def _norm(s: Any) -> str:
    return " ".join(str(s or "").replace(" ", " ").split()).lower()


def classify(design: Any, category: Any = "") -> Optional[str]:
    """Catalog sub key of one Miele product from its type name (designTypeName) and category path
    (dataLayer.category), or None (steamers, cookers, combi-set modules, everything that is not a supported cooking
    appliance). discover() feeds it listing rows, scrape() the product page's, so a model never lands under two subs."""
    d, c = _norm(design), _norm(category)
    if "mikrowelle" in d:
        return "microwave" if re.match(r"(?:einbau|stand)-mikrowellenger", d) else "sco"
    if "combiset" in c or "element" in d:
        return None
    if "kochfeld" in d and ("hobs" in c or not c):
        if "gas" in d:
            return "gas_cooktop"
        if "induktion" in d:
            return "induction"
        if "elektro" in d:
            return "radiant"
        return None
    if "backofen" in d:
        return "electric_oven"
    return None


def _classify_row(p: dict) -> Optional[str]:
    return classify(p.get("designTypeName"), (p.get("dataLayer") or {}).get("category"))


# ---------------------------------------------------------------- discover
def _price(p: dict) -> Optional[float]:
    for key in ("salesPrice", "price"):
        v = (p.get(key) or {}).get("value") if isinstance(p.get(key), dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    return None


def _clean_name(s: Any) -> str:
    return " ".join(str(s or "").replace(" ", " ").split())


def candidate_from(p: dict, sub: str) -> Optional[Candidate]:
    url = p.get("pdpUrl")
    model = _clean_name(p.get("name"))
    if not model or not isinstance(url, str) or not _host_in(url, PAGE_HOSTS):
        return None
    attrs: dict[str, Any] = {"fuel": _SUB_FUEL[sub]}
    if "dampf" in _norm(p.get("designTypeName")):
        attrs["steam"] = True
    design = _clean_name(p.get("designTypeName"))
    return Candidate(brand=BRAND, model_number=model, name=f"{model} {design}".strip(), url=url,
                     category=catalog.major_of(sub), subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY,
                     price_usd=None, price_local=_price(p), attrs=attrs, attrs_src={k: "listing" for k in attrs})


def parse_listing(rows: list[dict], sub: str, limit: int = 30) -> list[Candidate]:
    """Listing rows -> Candidates that classify() files under `sub` (deduped by model name = one per colour variant,
    off-domain urls dropped)."""
    out, seen = [], set()
    for p in rows:
        if not isinstance(p, dict) or _classify_row(p) != sub:
            continue
        c = candidate_from(p, sub)
        if c is None or c.model_number in seen:
            continue
        seen.add(c.model_number)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"Miele DE adapter does not support sub category {subcategory!r}")
    f = _Fetcher()
    found: dict[str, Candidate] = {}
    try:
        for source in SUB_SOURCES[subcategory]:
            for page in range(1, MAX_LIST_PAGES + 1):
                final, html = f.html(f"{BASE}/category/{source}" + ("" if page == 1 else f"?page={page}"))
                _check_url(final)
                lp = listing_page(html)
                for c in parse_listing(lp["products"], subcategory, limit):
                    found.setdefault(c.model_number, c)
                if len(found) >= limit or not lp["products"] or page >= int(lp.get("totalPageCount") or 1):
                    break
            if len(found) >= limit:
                break
    finally:
        f.close()
    print(f"miele_de: discover({subcategory}): kept {min(len(found), limit)}", file=sys.stderr)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- product page
def spec_rows(product: dict) -> list[tuple[str, str, str]]:
    """[(section_de, label_de, value_de)] from techspecs in page order; 'TRUE'/'FALSE' feature flags stay as they are
    (translated to Yes/No later); several values are joined with ', '."""
    rows = []
    for sec in product.get("techspecs") or []:
        title = _clean_name(sec.get("name"))
        for a in sec.get("attributes") or []:
            label = _clean_name(a.get("name"))
            vals = [_clean_name(v) for v in (a.get("values") or []) if _clean_name(v)]
            if label and vals:
                rows.append((title, label, ", ".join(vals)))
    return rows


_UNIT_LABEL = re.compile(r"^(.*?)\s+in\s+(kWh\s*/\s*24\s*h|kWh|kW|W|mm|cm|kg|l|V|Hz|A|mA|h|min|dB\(A\)[^\s]*)\s*$")


def _label_for_translation(label: str) -> str:
    """'Garraumvolumen in l' -> 'Garraumvolumen (l)' so the glossary's 'Label (unit)' rule applies."""
    m = _UNIT_LABEL.match(label)
    return f"{m.group(1)} ({m.group(2)})" if m else label


def _get(rows, *labels: str) -> Optional[str]:
    for want in (i18n.fold(x) for x in labels):
        for _s, lab, val in rows:
            if i18n.fold(lab) == want:
                return val
    return None


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 2))


def _num_dot(s: Any) -> Optional[float]:
    """Miele techspec numbers use a dot as decimal separator ('3.500' kW = 3.5, '0.3' W): plain float, not EU."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or "").replace(",", "."))
    return float(m.group(0)) if m else None


def facts(rows: list, sub: str) -> dict:
    """Typed facts in standard units from the spec rows; missing = unknown (key absent)."""
    out: dict[str, Any] = {"fuel": _SUB_FUEL[sub]}
    cavity = _num_dot(_get(rows, "Garraumvolumen in l", "Nutzinhalt in l"))
    if cavity:
        out["cavity_l"] = cavity
        out["oven_capacity_cuft"] = round(units.l_to_cuft(cavity), 2)
    for key, labels in (("width", ("Abmessungen in mm (Breite)", "Gerätebreite in mm", "Gehäusebreite in mm")),
                        ("height", ("Abmessungen in mm (Höhe)", "Gerätehöhe in mm", "Gehäusehöhe in mm")),
                        ("depth", ("Abmessungen in mm (Tiefe)", "Gerätetiefe in mm", "Gehäusetiefe in mm"))):
        v = _num_dot(_get(rows, *labels))
        if v:
            out[f"{key}_mm"] = v
            out[f"{key}_in"] = round(units.mm_to_in(v), 2)
    kg = _num_dot(_get(rows, "Gewicht in kg"))
    if kg:
        out["weight_lb"] = round(units.kg_to_lb(kg), 1)
    volt = _get(rows, "Spannung in V")
    if volt and (m := re.search(r"(\d{3})(?:\s*[-–]\s*(\d{3}))?", volt)):
        out["voltage_v"] = f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)
    hz = _get(rows, "Frequenz in Hz")
    if hz and (m := re.search(r"\d{2}", hz)):
        out["frequency_hz"] = float(m.group(0))
    zones = _num_dot(_get(rows, "Anzahl der Kochzonen"))
    if zones:
        out["burners"] = int(zones)
    mw = _num_dot(_get(rows, "Mikrowellenleistung maximal in W"))
    if mw is None:
        levels = [int(x) for x in re.findall(r"\d+", _get(rows, "Mikrowellenleistungsstufen in W") or "")]
        mw = float(max(levels)) if levels else None
    if mw:
        out["microwave_watts"] = int(mw)
    load = _num_dot(_get(rows, "Gesamtanschlusswert in kW"))
    if load:
        out["connected_load_kw"] = load
    for _s, lab, val in rows:
        if i18n.fold(lab).startswith("energieeffizienzklasse") and (cls := units.eu_energy_class(val)):
            out["energy_class"] = cls
            break
    return out


def _price_text(p: dict) -> Optional[str]:
    v = _price(p)
    return None if v is None else _fmt(v)


_DOC_RULES = (("Manual", re.compile(r"gebrauchsanweisung|montageanweisung|bedienungsanleitung", re.I)),
              ("SpecSheet", re.compile(r"produktblatt|produktdatenblatt", re.I)),
              ("EnergyGuide", re.compile(r"energielabel|datenblatt", re.I)))


def parse_documents(product: dict) -> list[tuple[str, str]]:
    """[(doc_type, https PDF url)]: operating/installation instructions, product sheet, energy label / EU data sheet;
    PDFs on miele.com only, first of each type, at most MAX_DOCS."""
    found: dict[str, str] = {}
    for g in product.get("documentGroups") or []:
        for d in (g.get("documents") or []) if isinstance(g, dict) else []:
            media = d.get("media") or {}
            url = media.get("url")
            if media.get("mimeType") != "application/pdf" or not isinstance(url, str) or not _host_in(url, FILE_HOSTS):
                continue
            text = f"{d.get('description') or ''} {media.get('altText') or ''}"
            if re.search(r"konformität|garantie|zeichnung", text, re.I):
                continue
            for dtype, pat in _DOC_RULES:
                if pat.search(text) and dtype not in found:
                    found[dtype] = url
                    break
    return [(dt, found[dt]) for dt, _ in _DOC_RULES if dt in found][:MAX_DOCS]


def _image(product: dict) -> Optional[str]:
    cands = [(product.get("primaryProductImage") or {}).get("url")] + [i.get("url") for i in product.get("images") or []
                                                                      if isinstance(i, dict)]
    return next((u for u in cands if isinstance(u, str) and _host_in(u, FILE_HOSTS)), None)


def _unique(key: str, taken: dict) -> str:
    if key not in taken:
        return key
    n = 2
    while f"{key} ({n})" in taken:
        n += 1
    return f"{key} ({n})"


_FLAG = {"TRUE": "Yes", "FALSE": "No"}


def parse_product(product: dict, url: str) -> tuple[ProductRecord, list[RawSpec], dict]:
    """-> (ProductRecord, [RawSpec with the ORIGINAL German], info{'documents'}). ValueError for a product family this
    adapter does not cover (refrigerators, washers, steamers, cookers ...)."""
    model = _clean_name(product.get("name"))
    if not model:
        raise MielePageError("product page has no model name")
    sub = classify(product.get("designTypeName"), (product.get("dataLayer") or {}).get("category"))
    if not sub:
        raise ValueError(f"not a supported Miele cooking product ({product.get('designTypeName')!r})")
    rows = spec_rows(product)
    if not rows:
        raise MielePageError("product spec table is empty")
    f = facts(rows, sub)
    tr = _de()
    secs = sorted({r[0] for r in rows})
    sec_en = dict(zip(secs, tr.translate_many(secs, "label")))
    lab_en = tr.translate_many([_label_for_translation(r[1]) for r in rows], "label")
    vals = [r[2] for r in rows]
    val_en = tr.translate_many([v for v in vals if v.upper() not in _FLAG], "value")
    it = iter(val_en)
    val_en = [_FLAG.get(v.upper()) or next(it) for v in vals]

    extra: dict[str, str] = {}
    for (sec, lab, val), le, ve in zip(rows, lab_en, val_en):
        if i18n.fold(lab).startswith("energieeffizienzklasse") and (cls := units.eu_energy_class(val)):
            ve = cls   # 'EU class A+', never an ENERGY STAR equivalent
        extra[_unique(f"{sec_en[sec]} > {le}", extra)] = ve
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=k, value=v) for s, k, v in rows]

    cur: dict[str, str] = {"Fuel type": _FUEL_EN[f["fuel"]]}
    if "cavity_l" in f:
        cur["Oven capacity (L)"] = _fmt(f["cavity_l"])
        cur["Oven capacity (cu ft)"] = _fmt(f["oven_capacity_cuft"])
    for k, label in (("width_mm", "Width (mm)"), ("height_mm", "Height (mm)"), ("depth_mm", "Depth (mm)")):
        if k in f:
            cur[label] = _fmt(f[k])
    if "burners" in f:
        cur["Burners/elements"] = str(f["burners"])
    if "microwave_watts" in f:
        cur["Microwave cooking power (W)"] = str(f["microwave_watts"])
    if "connected_load_kw" in f:
        cur["Total connected load (kW)"] = _fmt(f["connected_load_kw"])
    if "energy_class" in f:
        cur["Energy efficiency class (EU)"] = f["energy_class"]
    if (p := _price_text(product)):
        cur["Price incl. VAT (EUR)"] = p
    design = _clean_name(product.get("designTypeName"))
    if design:
        cur["Product type"] = tr.translate_many([design], "value")[0]

    color_de = _get(rows, "Gerätefarbe", "Farbe der Glaskeramik", "Frontfarbe", "Gehäusefarbe")
    color = tr.translate_many([color_de], "value")[0] if color_de else None
    short = str(product.get("shortPositioningText") or "")
    feats = [x.strip() for x in short.split(" I ") if x.strip()] if " I " in short else []   # 'DailyFresh I NoFrost' lists
    pod = tr.translate_many(feats, "value") if feats else []
    connected = product.get("connectedAppliance")
    wifi = connected if isinstance(connected, bool) else None

    rec = ProductRecord(
        brand=BRAND, model_number=model, product_name=f"{model} {design}".strip(), product_url=url,
        category=catalog.major_of(sub), subcategory=sub, door_style=None, finish_color=color,
        price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=_price(product),
        capacity_total_cuft=f.get("oven_capacity_cuft"), width_in=f.get("width_in"), height_in=f.get("height_in"),
        depth_in=f.get("depth_in"), weight_lb=f.get("weight_lb"), voltage_v=f.get("voltage_v"),
        frequency_hz=f.get("frequency_hz"), energy_kwh_year=None, energy_star=None,
        wifi_supported=wifi, wifi_evidence=("Miele product data: connectedAppliance = true" if wifi else None),
        pod_features=pod, extra_specs={**extra, **cur}, image_url=_image(product),
    )
    return rec, raw, {"documents": parse_documents(product)}


# ---------------------------------------------------------------- scrape
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _check_url(url)
    f = _Fetcher()
    try:
        final_url, html = f.html(url)
        _check_url(final_url)
    finally:
        f.close()
    product = pdp_product(html)
    rec, raw, info = parse_product(product, url)
    docs: list[DocumentRecord] = []
    for dt, link in info["documents"]:
        if not _host_in(link, FILE_HOSTS):
            print(f"miele_de: skipped {dt}: PDF url not https on miele.com ({link[:100]})", file=sys.stderr)
            continue
        _polite()
        d = download_pdf(BRAND, _safe_name(rec.model_number), dt, link)
        if not d:
            continue
        if any(x.sha256 == d.sha256 for x in docs):
            (common.ROOT / d.local_path).unlink(missing_ok=True)
            continue
        docs.append(d)
    return rec, docs, raw
