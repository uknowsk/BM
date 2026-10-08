"""Shared core of the Brazilian cooking adapters brastemp_br, consul_br (Whirlpool Corp.) and electrolux_br.

All three brand shops run on VTEX, whose public catalog API needs no login and is not covered by their robots.txt:
  * listing: GET <base>/api/catalog_system/pub/products/search/eletrodomesticos/<category>?_from=0&_to=49 (50 per page)
  * product: GET <base>/api/catalog_system/pub/products/search/<linkText>/p
Every product carries `productReference` (model), `categories`, `items[].sellers[].commertialOffer` and the whole
spec table (`allSpecificationsGroups` -> field names -> list of values), so discover() and scrape() use the same JSON
and the same classify_product(). Prices: `Price` is the current sale price ('por'); `ListPrice` ('de') and the PIX
discount that only shows in the instalment table are never used. Reviews are not in the catalog API: Brastemp/Consul
(FastStore) embed `productRatings` in the product page HTML, Electrolux (VTEX IO) injects an aggregateRating JSON-LD
block only in the rendered DOM (browser page).

Requests go through plain HTTP first and fall back to a real browser page (headless, then visible window; same
helpers and FRIDGE_BROWSER_MODE as the Electrolux adapters) if the site answers 403/429. At least 1 s between requests.
Scraped text is untrusted: https + host checks, ids validated, nothing executed. Portuguese labels/values are
translated through i18n.get('pt'); RawSpec keeps the source text.
"""
from __future__ import annotations

import html as _html
import json
import re
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import _electrolux_common as ec
import common
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

COUNTRY, REGION, CURRENCY = "br", "sa", "BRL"
DELAY_S = 1.0
PAGE_SIZE = 50          # VTEX allows at most 50 items per _from/_to window
MAX_PAGES = 8
_MODEL = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
_LINK = re.compile(r"^[A-Za-z0-9._-]{3,200}$")
_PATH = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")


@dataclass(frozen=True)
class Site:
    brand: str
    base: str                      # https://www.brastemp.com.br
    domain: str                    # brastemp.com.br (host suffix of every page/API URL)
    sub_sources: dict              # sub key -> tuple of catalog category paths ('eletrodomesticos/fogao', ...)
    render_rating: bool = False    # True: the rating exists only in the rendered DOM (browser page)

    @property
    def hosts(self) -> tuple[str, ...]:
        return (self.domain,)


# ------------------------------------------------------------------ transport
_LAST_REQUEST = [0.0]


def _throttle() -> None:
    wait = DELAY_S - (time.monotonic() - _LAST_REQUEST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST[0] = time.monotonic()


_BROWSER_FETCH = "async u=>{const r=await fetch(u,{headers:{Accept:'*/*'}});return [r.status,await r.text()]}"


class Fetcher:
    """GET helper bound to one site: plain requests first, a real browser page when the site blocks (403/429) or in
    FRIDGE_BROWSER_MODE=visible/headless. close() shuts the browser down."""

    def __init__(self, site: Site):
        self.site = site
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": common.UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self._pw = None
        self._browser = None
        self._page = None

    def _allowed(self, url: str) -> None:
        if urlparse(url).scheme != "https" or not ec.host_in(url, self.site.hosts):
            raise ValueError(f"fetch outside the allowed hosts: {url[:120]}")

    def _open_browser(self):
        if self._page is None:
            self._pw = sync_playwright().start()
            try:
                self._browser, self._page = ec.connect(self._pw, self.site.base + "/", self.site.hosts)
            except BaseException:
                self._pw.stop()
                self._pw = None
                raise
        return self._page

    def get(self, url: str) -> tuple[int, str]:
        """(status, body). Browser fetch is used for the rest of the session once the site has refused plain HTTP."""
        self._allowed(url)
        _throttle()
        if self._page is None:
            try:
                cur = url
                for _hop in range(4):       # redirects are followed by hand so every hop stays on the allowed hosts
                    r = self._http.get(cur, timeout=40, allow_redirects=False)
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        cur = urljoin(cur, r.headers["Location"])
                        self._allowed(cur)
                        continue
                    break
                if r.status_code not in (403, 429):
                    return r.status_code, r.text
            except requests.RequestException:
                pass
        page = self._open_browser()
        status, body = page.evaluate(_BROWSER_FETCH, url)
        return int(status), body

    def json(self, url: str):
        status, body = self.get(url)
        if not 200 <= status < 300:
            raise RuntimeError(f"fetch {url[:100]} -> HTTP {status}")
        return json.loads(body)

    def html(self, url: str) -> str:
        status, body = self.get(url)
        if status != 200:
            raise RuntimeError(f"fetch {url[:100]} -> HTTP {status}")
        return body

    def rendered(self, url: str, needle: str, timeout_ms: int = 12000) -> str:
        """Page DOM (browser) once a JSON-LD block containing `needle` exists (the review widget injects the
        aggregateRating a few seconds after load); whatever is there after the timeout otherwise. Only the small
        ld+json scripts are polled: scanning the whole 6 MB DOM every frame starves the page's own scripts."""
        self._allowed(url)
        _throttle()
        page = self._open_browser()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        ec.check_final_url(page.url, self.site.hosts)
        try:
            page.wait_for_function(
                "n => [...document.querySelectorAll('script[type=\"application/ld+json\"]')].some(s => s.textContent.includes(n))",
                arg=needle, polling=500, timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass
        return page.content()

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except PlaywrightError as e:
            print(f"vtex-br: browser.close() failed: {e}", file=sys.stderr)
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._browser = self._page = self._pw = None
            self._http.close()


@contextmanager
def open_fetcher(site: Site):
    f = Fetcher(site)
    try:
        yield f
    finally:
        f.close()


# ------------------------------------------------------------------ small helpers
def fold(s) -> str:
    """Lower-case, accent-free, whitespace-collapsed text for matching ('À gás' -> 'a gas')."""
    t = unicodedata.normalize("NFKD", str(s if s is not None else ""))
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c)).lower()).strip()


_TAG = re.compile(r"<[^>]+>")


def text_of(value) -> str:
    return ec.clean(_TAG.sub(" ", str(value if value is not None else "")))


def spec_map(p: dict) -> dict[str, str]:
    """folded field name -> cleaned value (multiple values joined with ' | ') for every spec field of the product."""
    out: dict[str, str] = {}
    for name in p.get("allSpecifications") or []:
        vals = p.get(name)
        if isinstance(vals, list):
            joined = " | ".join(dict.fromkeys(v for v in (text_of(x) for x in vals) if v))   # a field can repeat per SKU
            if joined:
                out.setdefault(fold(name), joined)
    return out


def _pick(specs: dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        v = specs.get(fold(n))
        if v and fold(v) not in ("-", "n/a", "xx", "nao se aplica"):
            return v
    return None


def _num(text) -> Optional[float]:
    return units.parse_eu_number(text) if re.search(r"\d", str(text or "")) else None


def _single_number(text) -> Optional[float]:
    """The number of a text holding exactly one number ('77,8 cm', '60 Hz'); None for ranges / lists."""
    nums = re.findall(r"\d+(?:[.,]\d+)?", str(text or ""))
    return _num(nums[0]) if len(nums) == 1 else None


# ------------------------------------------------------------------ classification
_KIND = {"fogao": "fogao", "cooktop": "cooktop", "cooktops": "cooktop", "forno": "forno", "fornos": "forno",
         "micro-ondas": "micro"}
# bundles, accessories and appliances outside the cooking sub groups (hoods, warming drawers, benchtop/portable units)
_EXCLUDE = re.compile(r"^(combo|kit|torre)\b|\b(combo|kit)\b|torre de coccao|gaveta aquecida|\bportatil\b|"
                      r"fritadeira|acessorio")
_BENCHTOP = re.compile(r"\bde (mesa|bancada)\b|\bbancada\b")
# an oven with a microwave function (speed-cook style): 'Forno Multifuncional com Micro-ondas', 'Forno ... 2 em 1 com
# Micro-ondas'. A plain 'forno micro-ondas' / 'forno de micro-ondas' (= a microwave) does not match.
_COMBI = re.compile(r"\bforno\b(?: de embutir| multifuncional| eletrico| a gas)*(?: 2 em 1)? (?:com|e|\+) micro[- ]?ondas\b|"
                    r"\bmicro[- ]?ondas (?:e|com|\+) forno\b")
_GAS = re.compile(r"\bgas\b|\bglp\b")
_INDUCTION = re.compile(r"\binducao\b")
_ELECTRIC = re.compile(r"\beletric[oa]s?\b|\bvitroceramic")


def _kind(p: dict) -> Optional[str]:
    for c in p.get("categories") or []:
        parts = [x for x in fold(c).strip("/").split("/") if x]
        if len(parts) >= 2 and parts[0] == "eletrodomesticos" and parts[1] in _KIND:
            return _KIND[parts[1]]
    return None


def classify_product(p: dict) -> Optional[str]:
    """Cooking sub key of a VTEX catalog product (shared by discover and scrape); None for anything that is not one of
    the supported appliances (bundles/kits, hoods, warming drawers, portable or benchtop units, hybrid hobs,
    outlet/refurbished stock, spare parts).
    Fogao (floor/built-in range with an oven) -> gas_oven; cooktop by heat source (gas -> gas_cooktop, induction ->
    induction, electric/vitroceramic -> radiant; a gas+induction hybrid fits neither and is skipped); forno de embutir
    by fuel (gas -> gas_oven, electric -> electric_oven), with a microwave function -> sco; micro-ondas -> microwave
    (sco when it is an oven with microwave)."""
    name = fold(p.get("productName"))
    ref = str(p.get("productReference") or "")
    cats = [fold(c).strip("/") for c in p.get("categories") or []]
    if not name or any(c.startswith("outlet") for c in cats) or "+" in ref or _EXCLUDE.search(name):
        return None
    kind = _kind(p)
    if kind is None:
        return None
    specs = spec_map(p)
    kind_text = fold(" ".join(filter(None, (_pick(specs, k) for k in (
        "Tipo", "Tipo do Produto", "Tipo de alimentação", "Alimentação", "Tipo De Aquecimento",
        "Tipo de tecnologia de cozimento")))))
    text = f"{name} {kind_text}"
    if kind == "forno" and _BENCHTOP.search(name + " " + " ".join(cats)):
        return None
    if kind == "fogao":
        return "induction" if _INDUCTION.search(name) else "gas_oven"
    if kind == "cooktop":
        gas, ind = bool(_GAS.search(text)), bool(_INDUCTION.search(text))
        if gas and ind:
            return None
        return "induction" if ind else "gas_cooktop" if gas else "radiant" if _ELECTRIC.search(text) else None
    # name + meta description only: the 'Tipo do Produto' spec of BOX47AR says 'Forno com Micro-ondas' for a plain oven
    combi = _COMBI.search(f"{name} {fold(p.get('metaTagDescription'))}")
    if kind == "micro":
        return "sco" if combi else "microwave"
    if combi:
        return "sco"
    return "gas_oven" if _GAS.search(text) else "electric_oven" if _ELECTRIC.search(text) else None


# ------------------------------------------------------------------ attribute extraction
def sale_price(p: dict) -> Optional[float]:
    """Current sale price in BRL ('Price'; never ListPrice and never the PIX price)."""
    for item in p.get("items") or []:
        for seller in item.get("sellers") or []:
            v = (seller.get("commertialOffer") or {}).get("Price")
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    return None


def release_date(p: dict) -> Optional[str]:
    """'YYYY-MM-DD' from the catalog `releaseDate` field (the shop's own launch date for the product); a date in the
    future is ignored."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(p.get("releaseDate") or ""))
    return m.group(1) if m and m.group(1) <= date.today().isoformat() else None


_H = ("Altura do produto", "Altura sem Embalagem", "Altura (cm) [sem Embalagem]", "Altura do forno")
_W = ("Largura do produto", "Largura sem Embalagem", "Largura (cm) [Sem Embalagem]", "Largura do forno")
_D = ("Profundidade do produto", "Profundidade sem Embalagem", "Profundidade (cm) [sem Embalagem]", "Profundidade do forno")
_TRIPLE = ("Dimensões sem Embalagem (AxLxP) (cm)", "Dimensões (AxLxP)")
_NUM = r"(\d+(?:[.,]\d+)?)"


def _first_single(specs: dict[str, str], names: tuple[str, ...]) -> Optional[float]:
    return next((v for v in (_single_number(_pick(specs, n)) for n in names) if v), None)


def dims_cm(specs: dict[str, str]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(height, width, depth) in cm: separate fields first, then an 'A x L x P' triple."""
    h, w, d = (_first_single(specs, names) for names in (_H, _W, _D))
    if not (h and w and d):
        triple = _pick(specs, *_TRIPLE)
        m = re.match(rf"\s*{_NUM}\s*(?:cm)?\s*x\s*{_NUM}\s*(?:cm)?\s*x\s*{_NUM}", triple or "", re.I)
        if m:
            th, tw, td = (_num(x) for x in m.groups())
            h, w, d = h or th, w or tw, d or td
    return h, w, d


def _litres(text) -> list[float]:
    s = str(text or "")
    found = [_num(x) for x in re.findall(rf"{_NUM}\s*(?:l\b|litros?\b)", s, re.I)]
    if not found and len(re.findall(r"\d+(?:[.,]\d+)?", s)) == 1 and not re.search(r"[xX×]", s):
        found = [_single_number(s)]
    return [x for x in found if x]


_CAP_KEYS = ("Capacidade total", "Capacidade total (L)", "Capacidade total  (L)", "Capacidade forno (L)", "Capacidade do Forno",
             "Volume do forno", "Volume do Forno (L)", "Volume do forno (L)", "Volume")
_CAP_LOW, _CAP_UP = "Capacidade do forno inferior ou simples (L)", "Capacidade do forno superior (L)"
_NAME_L = re.compile(rf"(?<![\d.,]){_NUM}\s*(?:l|litros?)\b", re.I)


def capacity_litres(p: dict, specs: dict[str, str], sub: str) -> tuple[Optional[float], str]:
    """(litres, source) of the cavity (all ovens of a double-oven range added up); source 'listing' = spec table,
    'name' = a '34L' / '32 Litros' mention in the product name. (None, '') when unknown."""
    if sub in ("gas_cooktop", "induction", "radiant"):
        return None, ""
    low, up = _litres(_pick(specs, _CAP_LOW)), _litres(_pick(specs, _CAP_UP))
    total: Optional[float] = (low[0] + up[0]) if low and up else None
    if total is None:
        for key in _CAP_KEYS:
            lit = _litres(_pick(specs, key))
            if lit:
                total = sum(lit[:2])
                break
    src = "listing"
    if total is None:
        m = _NAME_L.search(str(p.get("productName") or ""))
        total, src = (_num(m.group(1)), "name") if m else (None, "")
    lo, hi = (10, 60) if sub == "microwave" else (10, 250)
    return (total, src) if total is not None and lo <= total <= hi else (None, "")


_BURNER_KEYS = ("Quantidade de bocas", "Quantidade de Bocas", "Quantidade de queimadores", "Meli - Quantidade de queimadores")


def burners(p: dict, specs: dict[str, str]) -> tuple[Optional[int], str]:
    for key in _BURNER_KEYS:
        v = _single_number(_pick(specs, key))
        if v and 1 <= v <= 8:
            return int(v), "listing"
    m = re.search(r"\b(\d)\s*bocas?\b", fold(p.get("productName")))
    return (int(m.group(1)), "name") if m else (None, "")


def wifi_flag(p: dict, specs: dict[str, str]) -> Optional[bool]:
    for key, val in specs.items():
        if re.search(r"wi-?fi", key):
            return {"sim": True, "nao": False}.get(fold(val).split(" ")[0], None)
    return True if re.search(r"wi-?fi", fold(p.get("productName"))) else None


def finish_of(specs: dict[str, str]) -> Optional[str]:
    cor = fold(_pick(specs, "Cor", "Cores") or "")
    if "inox" in cor:
        return "stainless"
    return "black" if "preto" in cor else "white" if "branco" in cor else None


_DUAL = re.compile(r"forno eletric|eletrico:|gas:|eletrico \+ gas|gas \+ eletrico|superior: eletric|inferior: eletric")


def fuel_of(sub: str, p: dict, specs: dict[str, str]) -> Optional[str]:
    if sub == "gas_cooktop":
        return "gas"
    if sub in ("induction", "radiant", "electric_oven", "sco"):
        return {"induction": "induction"}.get(sub, "electric")
    if sub == "gas_oven":
        blob = fold(" ".join(filter(None, (p.get("productName"), _pick(specs, "Tipo de forno", "Tipo"),
                                           _pick(specs, "Capacidade forno (L)"), _pick(specs, "Volume")))))
        return "dual_fuel" if "forno" in blob and _DUAL.search(blob) and _kind(p) == "fogao" else "gas"
    return None


def main_image(p: dict, site: Site) -> Optional[str]:
    for item in p.get("items") or []:
        for img in item.get("images") or []:
            u = str(img.get("imageUrl") or "")
            if urlparse(u).scheme == "https" and u.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
                return u
    return None


def candidate(site: Site, p: dict, sub: str) -> Optional[Candidate]:
    model = str(p.get("productReference") or "").strip().upper()
    name = ec.clean(p.get("productName"))
    link = str(p.get("link") or "")
    if (not _MODEL.match(model) or not name or classify_product(p) != sub or urlparse(link).scheme != "https"
            or not ec.host_in(link, site.hosts)):
        return None
    specs = spec_map(p)
    attrs: dict = {}
    src: dict[str, str] = {}

    def put(key, value, how="listing"):
        if value is not None:
            attrs[key], src[key] = value, how

    _, w_cm, _ = dims_cm(specs)
    put("width_in", round(units.mm_to_in(w_cm * 10), 1) if w_cm else None)
    litres, how = capacity_litres(p, specs, sub)
    put("capacity_total_cuft", round(units.l_to_cuft(litres), 2) if litres else None, how)
    put("fuel", fuel_of(sub, p, specs))
    n, how = burners(p, specs)
    put("burners", n if sub in ("gas_oven", "gas_cooktop", "induction", "radiant") else None, how)
    put("wifi", wifi_flag(p, specs))
    put("finish", finish_of(specs))
    rel = release_date(p)
    if rel:
        put("release_date", rel)
        put("release_src", "site")
    return Candidate(brand=site.brand, model_number=model, name=name, url=link, price_usd=None, category="cooking",
                     subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=sale_price(p),
                     attrs=attrs, attrs_src=src)


# ------------------------------------------------------------------ discover
def _search_url(site: Site, path: str, start: int) -> str:
    if not _PATH.match(path):
        raise ValueError(f"bad category path {path!r}")
    return f"{site.base}/api/catalog_system/pub/products/search/{path}?_from={start}&_to={start + PAGE_SIZE - 1}"


def discover(site: Site, sub: str, limit: int = 30) -> list[Candidate]:
    if sub not in site.sub_sources:
        raise ValueError(f"unsupported subcategory {sub!r} for {site.brand} BR")
    found: dict[str, Candidate] = {}
    with open_fetcher(site) as f:
        for path in site.sub_sources[sub]:
            for page in range(MAX_PAGES):
                try:
                    items = f.json(_search_url(site, path, page * PAGE_SIZE))
                except RuntimeError:
                    if page == 0:
                        raise
                    break   # a window past the end of the category
                for p in items if isinstance(items, list) else []:
                    c = candidate(site, p, sub)
                    if c:
                        found.setdefault(c.model_number, c)
                if len(found) >= limit or not isinstance(items, list) or len(items) < PAGE_SIZE:
                    break
            if len(found) >= limit:
                break
    return list(found.values())[:limit]


# ------------------------------------------------------------------ reviews
_RATING_FASTSTORE = re.compile(r'"productRatings"\s*:\s*\{\s*"averageRating"\s*:\s*([\d.]+)\s*,\s*"reviewCount"\s*:\s*(\d+)')
_RATING_LD = re.compile(r'"aggregateRating"\s*:\s*\{[^{}]*?"ratingValue"\s*:\s*"?([\d.]+)"?[^{}]*?"reviewCount"\s*:\s*"?(\d+)"?')


def parse_rating(page_html: str) -> dict:
    """{'rating', 'review_count'} from a product page (FastStore productRatings or an aggregateRating JSON-LD);
    {} when the page shows no review."""
    m = _RATING_FASTSTORE.search(page_html or "") or _RATING_LD.search(page_html or "")
    return ec.review_signal(m.group(1), m.group(2)) if m else {}


def page_rating(f, site: Site, url: str) -> dict:
    """Best-effort review summary of the product page; any failure just means 'unknown'."""
    try:
        return parse_rating(f.rendered(url, "aggregateRating") if site.render_rating else f.html(url))
    except (RuntimeError, ValueError, PlaywrightError, requests.RequestException) as e:
        print(f"vtex-br: no rating for {url[:90]}: {e}", file=sys.stderr)
        return {}


# ------------------------------------------------------------------ product
_SPEC_GROUPS = {"caracteristicas tecnicas": "Technical specifications", "especificacoes tecnicas": "Technical specifications",
                "especificaciones tecnicas": "Technical specifications", "especificacoes": "Specifications",
                "componente de dimensoes": "Dimensions"}
_SKIP_LABEL = re.compile(r"^(ean\b|ncm|codigo comercial|origem anymarket|meli\b|meli_title|incluir especificacoes|"
                         r"produto substituto|textos legais|servico$|marcas?$|portfolio$|nome modelo|modelo$|"
                         r"manual|guia |infografico|saiba mais|videos?$|id do video|classificacao energetica 1|"
                         r"classificacao energetica 2|informacoes para instalacao|mais informacoes|conteudo da embalagem)")
_BAD_VALUE = re.compile(r"^(https?://|<iframe)", re.I)


def spec_rows(p: dict) -> list[tuple[str, str, str]]:
    """(section, label, value) in source language for the technical spec fields of the product."""
    rows: list[tuple[str, str, str]] = []
    for group in p.get("allSpecificationsGroups") or []:
        if fold(group) not in _SPEC_GROUPS:
            continue
        for label in p.get(group) or []:
            vals = p.get(label)
            if not isinstance(vals, list) or _SKIP_LABEL.match(fold(label)):
                continue
            value = " | ".join(dict.fromkeys(v for v in (text_of(x) for x in vals) if v and not _BAD_VALUE.match(v)))[:400]
            if value and fold(value) not in ("-", "xx"):
                rows.append((group, ec.clean(label), value))
    return rows


_DOCS = (("Manual do Produto", "Manual"), ("Manual do produto", "Manual"), ("Guia Rápido", "QuickSpecs"),
         ("Guia rápido", "QuickSpecs"), ("Guia de Instalação", "Installation"))


def doc_links(p: dict) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for label, dtype in _DOCS:
        vals = p.get(label)
        url = str(vals[0]).strip() if isinstance(vals, list) and vals else ""
        if urlparse(url).scheme == "https" and dtype not in out:
            out[dtype] = url
    return list(out.items())


def _features(p: dict) -> list[str]:
    feats = [text_of(x) for x in p.get("Diferenciais") or []]
    feats += [text_of(x) for k in sorted(p, key=lambda s: (len(s), s)) if re.fullmatch(r"feature-title-\d+", k)
              for x in p.get(k) or []]
    return [f for f in dict.fromkeys(feats) if f and f != "."]


def parse_product(site: Site, p: dict, url: str, signals: Optional[dict] = None,
                  translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    """ProductRecord + RawSpec rows from a catalog product dict (extra_specs 'Section > Label' in English through the
    pt translator; RawSpec keeps the Portuguese source)."""
    model = str(p.get("productReference") or "").strip().upper()
    rows = spec_rows(p)
    if not model or not rows:
        raise ValueError(f"{site.brand} product data unrecognised for {model or url}")
    if translator is None:
        import i18n
        translator = i18n.get("pt")
    labels = list(dict.fromkeys(l for _, l, _ in rows))
    values = list(dict.fromkeys(v for _, _, v in rows))
    lab_en = dict(zip(labels, translator.translate_many(labels, "label")))
    val_en = dict(zip(values, translator.translate_many(values, "value")))
    table: dict[str, str] = {}
    for sec, label, value in rows:
        ec.put_spec(table, f"{_SPEC_GROUPS[fold(sec)]} > {lab_en.get(label, label)}", val_en.get(value, value))
    specs = spec_map(p)
    pbe = _pick(specs, "Classificação energética", "Eficiência Energética")
    if pbe and re.fullmatch(r"[A-E]", pbe.strip().upper()):
        ec.put_spec(table, "Energy > Inmetro PBE class", pbe.strip().upper())
    sub = classify_product(p)
    h, w, d = dims_cm(specs)
    to_in = lambda cm: round(units.mm_to_in(cm * 10), 1) if cm else None  # noqa: E731
    kg = _first_single(specs, ("Peso do produto", "Peso (kg) [sem Embalagem]"))
    color = _pick(specs, "Cor", "Cores")
    litres, _ = capacity_litres(p, specs, sub or "")
    sku_volts = [text_of(v.get("name")) for s in p.get("skuSpecifications") or []
                 if fold((s.get("field") or {}).get("name")) == "voltagem" for v in s.get("values") or []]
    volt = _pick(specs, "Tensão") or ("/".join(v for v in sku_volts if v) or None)
    wifi = wifi_flag(p, specs)
    wifi_key = next((k for k in specs if re.search(r"wi-?fi", k)), None)
    feats = translator.translate_many(_features(p), "value")
    record = ProductRecord(
        brand=site.brand, model_number=model, product_name=ec.clean(p.get("productName")) or model, category="cooking",
        subcategory=sub, finish_color=translator.translate_value(color) if color else None, product_url=url,
        price_usd=None, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=sale_price(p),
        capacity_total_cuft=round(units.l_to_cuft(litres), 2) if litres else None,
        height_in=to_in(h), width_in=to_in(w), depth_in=to_in(d), weight_lb=round(units.kg_to_lb(kg), 1) if kg else None,
        voltage_v=volt, frequency_hz=_single_number(_pick(specs, "Frequência")),
        wifi_supported=wifi, wifi_evidence=f"{wifi_key}: {specs[wifi_key]}" if wifi_key and wifi is not None else None,
        pod_features=[f for f in dict.fromkeys(feats) if f], extra_specs=table, image_url=main_image(p, site),
        release_date=release_date(p), release_src="site" if release_date(p) else None, **(signals or {}))
    raw = [RawSpec(brand=site.brand, model_number=model, source="web", section=sec, key=label, value=value)
           for sec, label, value in rows]
    return record, raw


def link_from_url(site: Site, url: str) -> str:
    """linkText of https://<host>/<linkText>/p"""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not ec.host_in(url, site.hosts) or len(parts) != 2 or parts[1] != "p"
            or not _LINK.match(parts[0])):
        raise ValueError(f"not a supported {site.brand} BR product URL: {url}")
    return parts[0]


def scrape(site: Site, url: str, translator=None) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    link = link_from_url(site, url)
    with open_fetcher(site) as f:
        data = f.json(f"{site.base}/api/catalog_system/pub/products/search/{quote(link, safe='')}/p")
        if not isinstance(data, list) or not data or data[0].get("linkText") != link:
            raise ValueError(f"{site.brand} BR has no product {link!r}")
        signals = page_rating(f, site, f"{site.base}/{link}/p")
    p = data[0]
    record, raw = parse_product(site, p, f"{site.base}/{link}/p", signals, translator)
    return record, ec.fetch_docs(site.brand, record.model_number, doc_links(p)), raw
