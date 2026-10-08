"""Panasonic BR adapter (catalog.py contract): cooking only (microwave, sco), Portuguese site, BRL prices.

Data source: https://loja.panasonic.com.br, Panasonic do Brasil's official online store (VTEX IO). www.panasonic.com/br (the info
site) answers every non-browser and headless request with an Akamai "Access Denied" page and, in a visible browser, redirects to
this store; nothing is bypassed - the store itself serves plain requests. robots.txt (2026-10-08) disallows only /img/, /account/,
/login/, /checkout/, /busca/, /quick-view/ and /espiar/; this adapter reads the category and product pages only, >= 1 s apart
(requests -> headless Playwright -> visible window, FRIDGE_BROWSER_MODE = auto | headless | visible).
Panasonic sells microwave ovens only as home cooking appliances in Brazil (countertop; no built-in ovens, hobs or ranges).
  * listing `/cozinha/microondas[?page=N]`: schema.org `ItemList` JSON-LD with per product the name (ends with ' - NN-XXXX'), the
    product URL (`/<slug>/p`), image and `offers.lowPrice` (selling price in BRL; list price, PIX/boleto/card prices are not used).
  * product page: the VTEX `__STATE__` JSON (Apollo cache): Product (productReference = model, releaseDate, priceRange,
    specificationGroups with name/values, properties incl. 'Manual do Usuário' PDF and 'Lançamentos: Sim' = the store's NEW flag).
    The page carries no ratings or reviews. Weights: kg; dimensions L x A x P in mm.
Sub keys: 'sco' for microwaves that also have an oven function (convection / Airfryer / combination), otherwise 'microwave'.
Labels/values are English via a few built-in terms then i18n.get('pt'); RawSpec keeps the Portuguese original.
Allowed hosts to add to common.DOWNLOAD_HOST_ALLOW / IMAGE_HOST_ALLOW / server: loja.panasonic.com.br (pages), panasonic.vtexassets.com
(images; vteximg.com.br for old assets) and panasonic-br.zendesk.com (user-manual PDFs).
"""
import html as _html
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

import common
import i18n
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Panasonic"
COUNTRY, REGION, CURRENCY = "br", "sa", "BRL"
BASE = "https://loja.panasonic.com.br"
PAGE_HOSTS = ("loja.panasonic.com.br",)
IMAGE_HOSTS = ("panasonic.vtexassets.com", "panasonic.vteximg.com.br")
DOC_HOSTS = ("panasonic-br.zendesk.com",)
LISTING = "/cozinha/microondas"
DELAY_S = 1.0
MAX_PAGES = 3
MAX_REDIRECTS = 5
SUPPORTED_SUBCATEGORIES = {"microwave", "sco"}
_SLUG_URL = re.compile(r"^/[a-z0-9][a-z0-9-]{4,200}/p$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")


# ---------------------------------------------------------------- fetching
class PanasonicBrError(RuntimeError):
    pass


class PanasonicBrNotFound(PanasonicBrError):
    pass


def _strategies() -> list[str]:
    mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
    if mode == "headless":
        return ["headless"]
    if mode == "visible":
        return ["visible"]
    return ["requests", "headless", "visible"]


def host_in(url: str, suffixes: tuple[str, ...] = PAGE_HOSTS) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _check_final_url(url: str) -> None:
    if urlparse(url).scheme != "https" or not host_in(url):
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
        r = requests.get(cur, headers={"User-Agent": common.UA, "Accept-Language": "pt-BR,pt;q=0.9"}, timeout=40,
                         allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise PanasonicBrError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise PanasonicBrNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise PanasonicBrError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise PanasonicBrError(f"expected marker {probe!r} missing (structure changed?)")
    return body


def _consent(page, accept: bool = False) -> None:
    """Cookie banner: decline non-essential first (this store only offers 'Entendi!', so accepting is the fallback)."""
    names = ("Entendi!", "Aceitar") if accept else ("Recusar", "Rejeitar", "Somente necessários")
    for label in names:
        try:
            page.get_by_role("button", name=label).first.click(timeout=1500)
            return
        except (PlaywrightTimeoutError, PlaywrightError):
            continue


def _via_browser(url: str, probe: str, headless: bool) -> str:
    check = "(p) => document.documentElement.outerHTML.includes(p)"
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA, locale="pt-BR").new_page()
            _throttle()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_final_url(page.url)
            _consent(page)
            try:
                page.wait_for_function(check, arg=probe, timeout=20000)
            except PlaywrightTimeoutError:
                _consent(page, accept=True)
                page.wait_for_function(check, arg=probe, timeout=20000)
            status = resp.status if resp else None
            if status in (404, 410):
                raise PanasonicBrNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise PanasonicBrError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except PanasonicBrNotFound:
            raise
        except (PanasonicBrError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"panasonic_br: load via {strategy} failed: {e}", file=sys.stderr)
    raise PanasonicBrError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def fold(s) -> str:
    s = unicodedata.normalize("NFKD", _html.unescape(str(s or "")).casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def classify(name: str) -> str | None:
    """Sub key from the product name; the single rule for discover() and scrape(). Microwaves with an oven function
    (convection, Airfryer, 'forno', combination) -> sco; plain / grill microwaves -> microwave; anything else -> None."""
    n = fold(name)
    if not re.search(r"micro-?ondas", n):
        return None
    return "sco" if re.search(r"air\s*-?\s*fr|convec|combinad|\bforno\b|4 em 1", n) else "microwave"


_FINISH = (("black_stainless", r"black stainless"), ("white", r"branc"), ("black", r"black|pret"),
           ("stainless", r"inox|espelhad|prata"))
_LITRES = re.compile(r"\b(\d{2,3})\s*l\b", re.I)


def name_attrs(name: str) -> dict:
    attrs: dict = {}
    m = _LITRES.search(name)
    if m:
        attrs["capacity_total_cuft"] = round(units.l_to_cuft(float(m.group(1))), 2)  # 'Volume Total' (the 30 L in the title)
    t = fold(name.split(" - ")[0])
    fin = next((k for k, pat in _FINISH if re.search(pat, t)), None)
    if fin:
        attrs["finish"] = fin
    return attrs


def model_of(name: str) -> str | None:
    """The model code after the last ' - ' of the product name ('... Black Glass - NN-GT68LBRU')."""
    tail = " ".join(str(name).split()).rsplit(" - ", 1)[-1].strip()
    return tail if _MODEL.match(tail) and re.search(r"\d", tail) else None


# ---------------------------------------------------------------- discover
def parse_listing(html: str) -> list[dict]:
    """Products of the page's ItemList JSON-LD: [{name, url, image, price, sku}], in page order."""
    out = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("@type") != "ItemList":
            continue
        for el in d.get("itemListElement", []):
            p = el.get("item") if isinstance(el, dict) else None
            if not isinstance(p, dict) or not p.get("name") or not p.get("@id"):
                continue
            offers = p.get("offers") if isinstance(p.get("offers"), dict) else {}
            out.append({"name": " ".join(_html.unescape(str(p["name"])).split()), "url": str(p["@id"]), "image": p.get("image"),
                        "price": offers.get("lowPrice", offers.get("price")), "sku": p.get("sku")})
    return out


def _price(value) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def product_url_ok(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and host_in(url) and not u.query and not u.fragment and bool(_SLUG_URL.match(u.path))


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    name, url = item["name"], item["url"]
    model = model_of(name)
    if not model or not product_url_ok(url) or classify(name) != sub:
        return None
    attrs = name_attrs(name)
    return Candidate(brand=BRAND, model_number=model, name=name, url=url, price_usd=None, category="cooking", subcategory=sub,
                     region=REGION, country=COUNTRY, currency=CURRENCY, price_local=_price(item.get("price")), attrs=attrs,
                     attrs_src={k: "name" for k in attrs})


_LISTING_CACHE: dict[int, tuple[float, list[dict]]] = {}
LISTING_TTL_S = 300.0


def listing_page(page: int) -> list[dict]:
    hit = _LISTING_CACHE.get(page)
    if hit and time.monotonic() - hit[0] < LISTING_TTL_S:
        return hit[1]
    url = BASE + LISTING + (f"?page={page}" if page > 1 else "")
    items = parse_listing(fetch_html(url, "ItemList"))
    _LISTING_CACHE[page] = (time.monotonic(), items)
    return items


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Panasonic BR")
    found: dict[str, Candidate] = {}
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        try:
            items = listing_page(page)
        except PanasonicBrNotFound:
            break
        fresh = [i for i in items if i["url"] not in seen]
        seen.update(i["url"] for i in fresh)
        for item in fresh:
            c = item_to_candidate(item, subcategory)
            if c:
                found.setdefault(c.model_number, c)
        print(f"panasonic_br: discover({subcategory}) p{page}: {len(items)} listed, {len(found)} kept", file=sys.stderr)
        if not fresh or len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def url_check(url: str) -> str:
    """The product slug of an https loja.panasonic.com.br `/<slug>/p` URL; ValueError otherwise."""
    if not product_url_ok(url) or urlparse(url).username:
        raise ValueError(f"not a supported Panasonic BR product URL: {url}")
    return urlparse(url).path.strip("/").rsplit("/", 1)[0]


LABELS_PT = {
    "volume total": "Total volume", "volume util": "Usable volume", "potencia": "Microwave power", "potencia grill": "Grill power",
    "potencia convecção": "Convection power", "potencia convecao": "Convection power", "niveis de potencia": "Power levels",
    "eficiencia energetica": "Energy efficiency class", "cor": "Colour", "voltagem": "Voltage", "peso": "Net weight",
    "peso com embalagem": "Weight with packaging", "garantia": "Warranty", "tipo": "Type", "acessorios": "Included accessories",
    "dimensoes do produto (l x a x p )": "Product dimensions (W x H x D) (mm)", "dimensoes da embalagem (l x a x p )": "Packaged dimensions (W x H x D) (mm)",
    "diametro do prato": "Turntable diameter", "capacidade interna (litros)": "Internal capacity", "beneficios": "Benefits",
    "cor do display": "Display colour", "cor interna": "Interior colour", "acabamento da porta": "Door finish", "porta": "Door",
}
_COLOURS = {"preta": "Black", "preto": "Black", "branca": "White", "branco": "White", "cinza": "Grey", "prata": "Silver",
            "inox": "Stainless steel", "espelhado": "Mirror"}


def _resolve(st: dict, v):
    """Follow Apollo-cache references ({'type':'id','id':...}) and json wrappers to plain Python data."""
    if isinstance(v, dict):
        if v.get("type") == "id" and v.get("id") in st:
            return _resolve(st, st[v["id"]])
        if v.get("type") == "json":
            return v.get("json")
        return {k: _resolve(st, x) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(st, x) for x in v]
    return v


def product_state(html: str) -> dict:
    """The resolved Product object of the page's __STATE__ cache."""
    m = re.search(r'<template[^>]*data-varname="__STATE__"[^>]*>\s*<script>(.*?)</script>', html, re.S)
    if not m:
        raise PanasonicBrError("__STATE__ not found (structure changed?)")
    try:
        st = json.loads(m.group(1))
    except ValueError as e:
        raise PanasonicBrError(f"__STATE__ is not valid JSON ({e})") from e
    roots = [k for k in st if k.startswith("Product:") and "." not in k]
    if len(roots) != 1:
        raise PanasonicBrError(f"expected one Product in __STATE__, found {len(roots)}")
    p = _resolve(st, st[roots[0]])
    if not isinstance(p, dict) or not p.get("productReference"):
        raise PanasonicBrError("Product has no productReference")
    return p


def spec_rows(p: dict) -> list[tuple[str, str, str]]:
    """[(group, name, value)] of the product's specification groups (a multi-value spec is joined with ', ')."""
    rows = []
    for g in p.get("specificationGroups") or []:
        for s in g.get("specifications") or []:
            vals = [str(v).strip() for v in (s.get("values") or []) if str(v).strip()]
            if s.get("name") and vals:
                rows.append((str(g.get("name") or "Specifications"), str(s["name"]).strip(), ", ".join(vals)))
    return rows


def english_table(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    tr = i18n.get("pt")
    names = list(dict.fromkeys(l for _, l, _ in rows if fold(l) not in LABELS_PT))
    en = dict(zip(names, tr.translate_many(names, "label"))) if names else {}
    vals_in = list(dict.fromkeys(v for _, _, v in rows))
    vals = dict(zip(vals_in, tr.translate_many(vals_in, "value"))) if vals_in else {}
    table: dict[str, str] = {}
    for _, lab, val in rows:
        key = "Specifications > " + (LABELS_PT.get(fold(lab)) or en.get(lab) or lab)
        value = vals.get(val, val)
        table[key] = f"{table[key]} | {value}" if key in table and value not in table[key] else table.get(key, value)
    return table


def _get(rows, label: str) -> str | None:
    return next((v for _, l, v in rows if fold(l) == fold(label)), None)


def _num(pattern: str, text: str | None) -> float | None:
    m = re.search(pattern, text or "")
    return float(m.group(1).replace(".", "").replace(",", ".") if "," in m.group(1) else m.group(1)) if m else None


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    p = product_state(html)
    model = str(p["productReference"]).strip()
    if not _MODEL.match(model):
        raise PanasonicBrError(f"unexpected model code {model!r}")
    name = " ".join(_html.unescape(str(p.get("productName") or model)).split())
    rows = spec_rows(p)
    props = {str(x.get("name")): x.get("values") for x in p.get("properties") or [] if isinstance(x, dict)}
    tr = i18n.get("pt")
    extra = english_table(rows)
    cls = units.br_energy_class(_get(rows, "Eficiência Energética") or "")
    if cls:
        extra["Specifications > Energy efficiency class"] = cls
    wxhxd = re.findall(r"\d+(?:[.,]\d+)?", _get(rows, "Dimensões do Produto (L x A x P )") or "")
    wxhxd = [float(x.replace(",", ".")) for x in wxhxd] if len(wxhxd) == 3 else None
    kg = _num(r"([\d.,]+)\s*kg", _get(rows, "Peso"))
    litres = _num(r"([\d.,]+)\s*l\b", _get(rows, "Volume Total") or _get(rows, "Capacidade Interna  (Litros)")) or \
        _num(r"(\d+)\s*l\b", name.lower())
    colour = _get(rows, "Cor")
    colour_en = (_COLOURS.get(fold(colour)) or tr.translate_value(colour)) if colour else None
    price_range = (p.get("priceRange") or {}).get("sellingPrice") or {}
    price = _price(price_range.get("lowPrice"))
    if price is None:  # no sellable SKU in the range: the cheapest listed offer
        offers = [s.get("commertialOffer", {}).get("Price") for it in p.get("items") or [] for s in it.get("sellers") or []]
        price = min((x for x in map(_price, offers) if x), default=None)
    volts = (_get(rows, "Voltagem") or "").upper().replace("V", "").replace(" ", "") or None
    image = None
    for it in p.get("items") or []:
        for im in it.get("images") or []:
            cand = im.get("imageUrl") if isinstance(im, dict) else None
            if isinstance(cand, str) and urlparse(cand).scheme == "https" and host_in(cand, IMAGE_HOSTS):
                image = cand
                break
        if image:
            break
    benefits = props.get("Benefícios") or []
    release = str(p.get("releaseDate") or "")[:10]
    flags = {"is_new": True} if (props.get("Lançamentos") or [""])[0].strip().lower() == "sim" else {}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release):
        flags.update(release_date=release, release_src="distribution")  # catalog launch date of the store, not a manufacturer date
    manual = next((str(u) for u in (props.get("Manual do Usuário") or []) if str(u).lower().endswith(".pdf")), None)
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=classify(name), product_url=url,
        finish_color=colour_en, region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None, price_local=price,
        capacity_total_cuft=round(units.l_to_cuft(litres), 2) if litres else None,
        width_in=round(units.mm_to_in(wxhxd[0]), 1) if wxhxd else None, height_in=round(units.mm_to_in(wxhxd[1]), 1) if wxhxd else None,
        depth_in=round(units.mm_to_in(wxhxd[2]), 1) if wxhxd else None,
        weight_lb=round(units.kg_to_lb(kg), 1) if kg else None, voltage_v=volts,
        pod_features=tr.translate_many([str(b) for b in benefits], "value") if isinstance(benefits, list) else [],
        extra_specs=extra, image_url=image, **flags)
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=g, key=l, value=v) for g, l, v in rows]
    docs = [("Manual", manual)] if manual and urlparse(manual).scheme == "https" and host_in(manual, DOC_HOSTS) else []
    return record, raw, docs


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    url_check(url)
    html = fetch_html(url, "__STATE__")
    record, raw, links = parse_product(url, html)
    docs: list[DocumentRecord] = []
    for dtype, durl in links:
        rec = common.download_pdf(BRAND, record.model_number, dtype, durl)
        if rec:
            docs.append(rec)
    return record, docs, raw
