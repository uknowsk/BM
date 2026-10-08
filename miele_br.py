"""Miele BR adapter (catalog.py contract): cooking only (hobs, ranges, ovens), Portuguese site, BRL prices.

Data source: https://shop.mielebrasil.com.br, Miele Brasil's own online shop (Spree). The brand's info site (www.mielebrasil.com.br,
and miele.com.br) is behind a WAF that rejects non-browser requests and was not used; the shop serves plain requests.
robots.txt (2026-10-08): `Crawl-delay: 2`, and `Disallow: /*?*` (every URL with a query string, incl. `?page=` pagination and the
`?taxon_id=` suffix of the listing's product links), /checkout, /cart, /orders, /user, /account, /api, /password. So this adapter
requests only query-free `/t/...` category pages and `/products/<slug>` pages, >= 2 s apart (requests -> headless Playwright ->
visible window, FRIDGE_BROWSER_MODE = auto | headless | visible). The shop lists about 30 cooking products, all on one page per category.
  * category `/t/cozinha/gastronomia/<group>/<taxon>`: `<article class="product-card">` with name, price ('R$32.899,00' = the shop price)
    and the product link. Colour variants are separate products (model suffix CLST / GRGR / OBSW).
  * product page: schema.org microdata (`itemprop` name / sku (Miele TNR) / price / image; its `priceCurrency` says CLP, a shop bug -
    BRL is used) and the description HTML (one summary line, an optional '(L x A x P) em mm' size line, feature bullets). Miele BR
    publishes no spec table, ratings, release dates or manuals here, so those stay unset. Some items are outlet stock (stated in the
    description; flagged 'Notes > Outlet item') and ovens may show 'Solicitar Cotação' because Miele installs them.
TLS: the shop does not send its intermediate certificate, so python-requests fails certificate verification (browsers fetch it
themselves); on an SSLError the adapter switches to the headless browser for the rest of the process and never turns verification off.
Sub keys: gas_cooktop (cooktop a gás, rangetop, ProLine gas module), induction, gas_oven (Range Cooker = gas range with oven),
electric_oven (forno elétrico, forno combinado a vapor), sco (forno combinado com micro-ondas). Not sold: microwave-only, radiant.
Left out: ProLine griddle / barbecue / teppanyaki modules, warming drawers, hoods.
Allowed hosts to add to common.DOWNLOAD_HOST_ALLOW / IMAGE_HOST_ALLOW / server: shop.mielebrasil.com.br (pages) and
d21v6iwzex1yc.cloudfront.net (product images; exact host only).
"""
import html as _html
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

BRAND = "Miele"
COUNTRY, REGION, CURRENCY = "br", "sa", "BRL"
BASE = "https://shop.mielebrasil.com.br"
PAGE_HOSTS = ("shop.mielebrasil.com.br",)
IMAGE_HOSTS = ("d21v6iwzex1yc.cloudfront.net",)
DELAY_S = 2.0  # robots.txt Crawl-delay
MAX_REDIRECTS = 5
ROOT = "/t/cozinha/gastronomia/"
SUB_TAXONS: dict[str, tuple[str, ...]] = {
    "gas_cooktop": ("fogoes/fogoes-a-gas", "fogoes/range-tops", "fogoes/proline-combisets"),
    "induction": ("fogoes/fogoes-de-inducao", "fogoes/proline-combisets"),
    "gas_oven": ("fogoes/range-cookers",),
    "electric_oven": ("fornos/fornos-eletricos", "fornos/fornos-combinados-a-vapor"),
    "sco": ("fornos/fornos-combinados-com-micro-ondas",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_TAXONS)
_SLUG = re.compile(r"^/products/[a-z0-9][a-z0-9-]{4,200}$")
_MODEL = re.compile(r"^[A-Z0-9][A-Za-z0-9 ./()-]{2,40}$")


# ---------------------------------------------------------------- fetching
class MieleBrError(RuntimeError):
    pass


class MieleBrNotFound(MieleBrError):
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
        raise MieleBrError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise MieleBrNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise MieleBrError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise MieleBrError(f"expected marker {probe!r} missing (structure changed?)")
    return body


def _via_browser(url: str, probe: str, headless: bool) -> str:
    check = "(p) => document.documentElement.outerHTML.includes(p)"
    with sync_playwright() as p:
        browser = common.launch_browser(p, headless=headless)
        try:
            page = browser.new_context(user_agent=common.UA, locale="pt-BR").new_page()
            _throttle()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _check_final_url(page.url)
            page.wait_for_function(check, arg=probe, timeout=20000)
            status = resp.status if resp else None
            if status in (404, 410):
                raise MieleBrNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise MieleBrError(f"blocked (status {status})")
            return page.content()  # the shop shows no cookie banner
        finally:
            browser.close()


_requests_broken = [False]  # the shop's TLS chain lacks its intermediate certificate: python-requests cannot verify it, browsers can


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        if strategy == "requests" and _requests_broken[0]:
            continue
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except MieleBrNotFound:
            raise
        except (MieleBrError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            if isinstance(e, requests.exceptions.SSLError):
                _requests_broken[0] = True  # never disable verification; use the browser instead
            print(f"miele_br: load via {strategy} failed: {e}"[:300], file=sys.stderr)
    raise MieleBrError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def fold(s) -> str:
    s = unicodedata.normalize("NFKD", _html.unescape(str(s or "")).casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def classify(name: str) -> str | None:
    """Sub key from the product name (the listing may cut it with '...', the keywords come first); one rule for discover() and scrape()."""
    n = fold(name)
    if re.search(r"churrasqueira|tepan|fritadeira|grelhar|gaveta|coifa|exaustor|lava|refriger|adega|cafe", n):
        return None
    if "micro" in n:
        return "sco"  # every micro-wave product here is a combination oven (convection / steam / grill)
    if re.search(r"range ?cooker", n):
        return "gas_oven"
    if "induc" in n:
        return "induction"
    if re.search(r"rangetop|\bgas\b", n) and re.search(r"cooktop|placa|rangetop|fogao", n):
        return "gas_cooktop"
    return "electric_oven" if "forno" in n else None


_MODEL_RE = re.compile(r"\b[A-Z]{1,4} \d{3,4}(?:-\d)?(?: (?:[A-Z]{1,5}|\d)(?!\w))*(?:/[A-Z]+)?(?: \([A-Z]+\))?")
_SLUG_MODEL = re.compile(r"((?:^|-)[a-z]{1,4})-(\d{3,4}(?:-[a-z0-9]{1,4}){0,3})$")


def model_of(name: str, slug: str = "") -> str | None:
    """Model code from the name tail ('Cooktop a gás 4 bocas KM 3465 LP' -> 'KM 3465 LP'); a name cut short by the listing falls
    back to the URL slug ('...-cs-1222-i' -> 'CS 1222 I')."""
    m = _MODEL_RE.search(" ".join(_html.unescape(str(name)).split()))
    if m:
        return m.group(0)
    s = _SLUG_MODEL.search(slug)
    return (s.group(1).lstrip("-") + " " + s.group(2).replace("-", " ")).upper() if s else None


def name_attrs(name: str, sub: str) -> dict:
    """Standard-unit facts the product name states (width, burners, fuel); unknown = key absent."""
    attrs: dict = {}
    n = fold(" ".join(name.split()))
    m = re.search(r"(?:range ?cooker|rangetop|dual fuel)\D{0,20}?\b(30|36|48)\b", n)  # inches
    if m:
        attrs["width_in"] = float(m.group(1))
    else:
        cm = re.search(r"(\d{2,3})\s?cm\b", n)
        mm = re.search(r"\b(\d{3})\s?mm\b", n)
        if cm:
            attrs["width_in"] = round(units.mm_to_in(int(cm.group(1)) * 10), 1)
        elif mm:
            attrs["width_in"] = round(units.mm_to_in(int(mm.group(1))), 1)
    b = re.search(r"\b(\d)\s*bocas\b", n)
    if b and sub in ("gas_cooktop", "gas_oven"):
        attrs["burners"] = int(b.group(1))
    fuel = {"gas_cooktop": "gas", "induction": "induction", "electric_oven": "electric"}.get(sub)
    if sub == "gas_oven":
        fuel = "dual_fuel" if "dual fuel" in n else "gas"
    if fuel and sub != "electric_oven":
        attrs["fuel"] = fuel
    return attrs


# ---------------------------------------------------------------- discover
_ARTICLE = re.compile(r'<article[^>]*class="product-card"[^>]*>(.*?)</article>', re.S)


def parse_listing(html: str) -> list[dict]:
    """[{name, slug, price}] of the category page's product cards, in page order (the `?taxon_id=` link suffix is dropped)."""
    out = []
    for m in _ARTICLE.finditer(html):
        a = m.group(1)
        href = re.search(r'href="(/products/[^"?#]*)', a)
        name = re.search(r'itemprop="name">\s*(.*?)\s*</div>', a, re.S)
        price = re.search(r'product-price"[^>]*>\s*(.*?)\s*</div>', a, re.S)
        if href and name:
            out.append({"slug": href.group(1), "name": " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", name.group(1))).split()),
                        "price": price.group(1).strip() if price else None})
    return out


def parse_price(text) -> float | None:
    """'R$32.899,00' -> 32899.0 (the shop price; no instalment or payment-method prices exist on these pages)."""
    if text is None:
        return None
    v = units.parse_eu_number(str(text).replace("R$", ""))
    return v if v and v > 0 else None


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    slug, name = item["slug"], item["name"]
    model = model_of(name, slug)
    if not _SLUG.match(slug) or not model or not _MODEL.match(model) or classify(name) != sub:
        return None
    attrs = name_attrs(name, sub)
    return Candidate(brand=BRAND, model_number=model, name=name, url=BASE + slug, price_usd=None, category="cooking", subcategory=sub,
                     region=REGION, country=COUNTRY, currency=CURRENCY, price_local=parse_price(item.get("price")), attrs=attrs,
                     attrs_src={k: "name" for k in attrs})


_TAXON_CACHE: dict[str, tuple[float, list[dict]]] = {}
TAXON_TTL_S = 300.0


def taxon_items(taxon: str) -> list[dict]:
    hit = _TAXON_CACHE.get(taxon)
    if hit and time.monotonic() - hit[0] < TAXON_TTL_S:
        return hit[1]
    items = parse_listing(fetch_html(f"{BASE}{ROOT}{taxon}", "product-card"))
    _TAXON_CACHE[taxon] = (time.monotonic(), items)
    return items


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Miele BR")
    found: dict[str, Candidate] = {}
    for taxon in SUB_TAXONS[subcategory]:
        try:
            items = taxon_items(taxon)
        except MieleBrNotFound:
            continue
        for item in items:
            c = item_to_candidate(item, subcategory)
            if c:
                found.setdefault(c.model_number, c)
        print(f"miele_br: discover({subcategory}) {taxon}: {len(items)} listed, {len(found)} kept", file=sys.stderr)
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def url_check(url: str) -> str:
    """The product slug of an https shop.mielebrasil.com.br `/products/<slug>` URL (no query: robots.txt); ValueError otherwise."""
    u = urlparse(url)
    if u.scheme != "https" or not host_in(url) or u.query or u.fragment or u.username or not _SLUG.match(u.path):
        raise ValueError(f"not a supported Miele BR product URL: {url}")
    return u.path


def _meta(html: str, prop: str) -> str | None:
    m = re.search(r'<meta itemprop="%s" content="([^"]*)"' % prop, html)
    return _html.unescape(m.group(1)) if m else None


def description_lines(html: str) -> tuple[list[str], list[str], list[str]]:
    """(paragraph lines, bold headings, bullet items) of the description HTML (the meta attribute is HTML-escaped twice)."""
    m = re.search(r'<meta itemprop="description" content="(.*?)">', html, re.S)
    raw = _html.unescape(_html.unescape(m.group(1))) if m else ""
    heads = [" ".join(_html.unescape(re.sub(r"<[^>]+>", " ", h)).split()) for h in re.findall(r"(?is)<strong>(.*?)</strong>", raw)]
    bullets = [" ".join(_html.unescape(re.sub(r"<[^>]+>", " ", b)).split()) for b in re.findall(r"(?is)<li[^>]*>(.*?)</li>", raw)]
    text = re.sub(r"(?is)<li[^>]*>.*?</li>", "\n", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", text)
    lines = [" ".join(_html.unescape(re.sub(r"<[^>]+>", " ", ln)).split()) for ln in text.splitlines()]
    notice = lambda t: fold(t).startswith("aviso importante")  # the outlet notice is bold text, not a feature
    return [ln for ln in lines if ln], [h for h in heads if h and not notice(h)], [b for b in bullets if b and not notice(b)]


def _width_from_text(text: str) -> float | None:
    """Width in inches from a summary / bullet ('91cm', 'Largura de 620mm', 'de 914mm de largura', '380 mm de largura')."""
    t = fold(text)
    m = re.search(r"(\d{3})\s?mm\s+de\s+largura|largura\s+de\s+(\d{3})\s?mm", t)
    if m:
        return round(units.mm_to_in(int(m.group(1) or m.group(2))), 1)
    m = re.search(r"\b(\d{2,3})\s?cm\b", t)
    return round(units.mm_to_in(int(m.group(1)) * 10), 1) if m else None


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec]]:
    name = " ".join((_meta(html, "name") or "").split())
    tnr = _meta(html, "sku")
    slug = url_check(url)
    model = model_of(name, slug)
    if not name or not tnr or not model:
        raise MieleBrError("product microdata incomplete (structure changed?)")
    lines, heads, bullets = description_lines(html)
    summary = next((ln for ln in lines if not fold(ln).startswith(("aviso importante", "dimensoes do produto"))), "")
    dims = next((re.findall(r"\d+", ln.split(":", 1)[1]) for ln in lines if fold(ln).startswith("dimensoes do produto") and ":" in ln), [])
    w_h_d = [float(x) for x in dims] if len(dims) == 3 else None
    outlet = any("outlet" in fold(ln) for ln in lines)
    tr = i18n.get("pt")
    rows: list[tuple[str, str, str]] = [("Descrição", "Resumo", summary)] if summary else []
    if w_h_d:
        rows.append(("Dados técnicos", "Dimensões do produto (L x A x P) em mm", " x ".join(dims)))
    rows += [("Destaques", h, "Sim") for h in heads]
    for b in bullets:
        label, sep, value = b.partition(":")
        rows.append(("Destaques", label.strip(), value.strip()) if sep and value.strip() and len(label) <= 70 else ("Destaques", b, "Sim"))
    names = list(dict.fromkeys(l for _, l, _ in rows if l != "Dimensões do produto (L x A x P) em mm"))
    en_l = dict(zip(names, tr.translate_many(names, "label"))) if names else {}
    vals = list(dict.fromkeys(v for _, l, v in rows if l != "Resumo"))
    en_v = dict(zip(vals, tr.translate_many(vals, "value"))) if vals else {}
    extra: dict[str, str] = {}
    for sec, lab, val in rows:
        if lab == "Resumo":
            extra["General > Description"] = tr.translate_value(val) if len(val) <= 200 else val
        elif lab.startswith("Dimensões do produto"):
            extra["Size > Product dimensions (W x H x D) (mm)"] = val
        else:
            extra[f"Features > {en_l.get(lab, lab)}"] = en_v.get(val, val)
    extra["General > Miele material number (TNR)"] = tnr
    if outlet:
        extra["Notes > Outlet item"] = "Yes (the shop states the product is outlet stock)"
    zones = next((m.group(1) for b in [summary] + bullets if (m := re.search(r"\b(\d) (?:zonas|queimadores)\b", fold(b)))), None)
    if zones:
        extra["General > Cooking zones / burners"] = zones
    price = parse_price(_meta(html, "price"))
    image = _meta(html, "image")
    if not (image and urlparse(image).scheme == "https" and host_in(image, IMAGE_HOSTS)):
        image = None
    width = round(units.mm_to_in(w_h_d[0]), 1) if w_h_d else _width_from_text(" ".join([summary] + bullets[:3]))
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=classify(name), product_url=url,
        region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None, price_local=price,
        width_in=width, height_in=round(units.mm_to_in(w_h_d[1]), 1) if w_h_d else None,
        depth_in=round(units.mm_to_in(w_h_d[2]), 1) if w_h_d else None,
        pod_features=tr.translate_many((heads + bullets)[:8], "value"), extra_specs=extra, image_url=image)
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=l, value=v) for s, l, v in rows]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    url_check(url)
    html = fetch_html(url, 'itemprop="sku"')
    record, raw = parse_product(url, html)
    return record, [], raw
