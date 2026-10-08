"""Smeg BR adapter (catalog.py contract): cooking only (ovens, microwaves, floor cookers, cooktops), Portuguese site, BRL prices.

Data source: https://www.smegbrasil.com.br (the official Smeg Brasil store; www.smeg.com.br redirects there). The shop runs on
the Magazord platform; robots.txt allows everything except login/cart/checkout and filter/sort/search query strings
(`?caracteristica=`, `?ordem=`, `?q=` ...), none of which are used here. Plain requests work (no bot wall seen on 2026-10-08);
the fetch order is requests -> headless Playwright -> visible window (FRIDGE_BROWSER_MODE = auto | headless | visible), >= 1 s apart.
  * listing `/coccao?p=N` (all cooking, ~25 items/page): a `dataVitrine... = {itens: [...]}` JS literal with, per product,
    `codigo` (model), `nome`, `categoria` ('Cocção,Cooktop,Cooktop Gás'), `valor` (selling price; `valor_de` is the struck-through
    old price, `valor_pix`/`valor_cartao` are payment-method prices and are ignored), `nota`/`avaliacoes` (rating, 0 reviews today) and `link`.
  * product page `/<slug>`: schema.org Product JSON-LD (name, image, price) plus page JSON with `caracteristicas` (colour, width,
    burners, spec-PDF path), `data_lancamento` (the shop's launch date -> release_date, release_src 'distribution') and the HTML
    `descricao`: `<p><strong>Section</strong></p><p>Label: value<br />Label: value</p>` blocks (the full spec sheet).
Labels/values are translated to English by `i18n.get('pt')` (glossary, cache, local LLM) with a few built-in terms; RawSpec keeps the
Portuguese original. Long descriptive values (marketing text per cooking mode / feature) become 'Yes' in extra_specs.
Allowed hosts to add to common.DOWNLOAD_HOST_ALLOW / IMAGE_HOST_ALLOW / server: smegbrasil.com.br (pages) and
smegbrasil.cdn.magazord.com.br (images and spec PDFs).
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

BRAND = "Smeg"
COUNTRY, REGION, CURRENCY = "br", "sa", "BRL"
BASE = "https://www.smegbrasil.com.br"
PAGE_HOSTS = ("smegbrasil.com.br",)
IMAGE_HOSTS = ("smegbrasil.cdn.magazord.com.br",)
DELAY_S = 1.0
MAX_PAGES = 8
MAX_REDIRECTS = 5
# Smeg BR sells no glass-ceramic (radiant) hob today; classify() still maps one if it appears.
SUPPORTED_SUBCATEGORIES = {"microwave", "sco", "electric_oven", "gas_oven", "gas_cooktop", "induction"}
LISTING = "coccao"
_SLUG = re.compile(r"^/[a-z0-9][a-z0-9-]{4,200}$")
_NOT_PRODUCT = {"coccao", "busca", "login", "cliente", "checkout", "cadastro-de-clientes", "politica-de-privacidade", "acessorios"}


# ---------------------------------------------------------------- fetching
class SmegBrError(RuntimeError):
    pass


class SmegBrNotFound(SmegBrError):
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
        r = requests.get(cur, headers={"User-Agent": common.UA, "Accept-Language": "pt-BR,pt;q=0.9"}, timeout=30,
                         allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            cur = urljoin(cur, r.headers["Location"])
            continue
        break
    else:
        raise SmegBrError(f"more than {MAX_REDIRECTS} redirects for {url}")
    if r.status_code in (404, 410):
        raise SmegBrNotFound(f"HTTP {r.status_code} for {url}")
    body = r.content.decode("utf-8", "replace")
    if r.status_code != 200 or common.looks_blocked(r.status_code, body):
        raise SmegBrError(f"blocked or bad status ({r.status_code})")
    if probe not in body:
        raise SmegBrError(f"expected marker {probe!r} missing (structure changed?)")
    return body


def _consent(page, accept: bool = False) -> None:
    """Cookie banner: decline non-essential first; accept only when the page is stuck behind it."""
    names = ("Aceitar todos", "Aceitar", "Concordo") if accept else ("Recusar", "Rejeitar", "Somente necessários", "Apenas necessários")
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
                page.wait_for_function(check, arg=probe, timeout=15000)
            except PlaywrightTimeoutError:
                _consent(page, accept=True)  # the banner may be blocking rendering; retry once
                page.wait_for_function(check, arg=probe, timeout=15000)
            status = resp.status if resp else None
            if status in (404, 410):
                raise SmegBrNotFound(f"HTTP {status} for {url}")
            if common.looks_blocked(status, page.inner_text("body")):
                raise SmegBrError(f"blocked (status {status})")
            return page.content()
        finally:
            browser.close()


def fetch_html(url: str, probe: str) -> str:
    _check_final_url(url)
    last: Exception | None = None
    for strategy in _strategies():
        try:
            return _via_requests(url, probe) if strategy == "requests" else _via_browser(url, probe, strategy == "headless")
        except SmegBrNotFound:
            raise
        except (SmegBrError, ValueError, requests.RequestException, PlaywrightError, PlaywrightTimeoutError) as e:
            last = e
            print(f"smeg_br: load via {strategy} failed: {e}", file=sys.stderr)
    raise SmegBrError(f"could not load {url}: {last}")


# ---------------------------------------------------------------- classification
def fold(s) -> str:
    """Lower-case, accent-free text for rule matching ('Cocção,Fogão de Piso' -> 'coccao,fogao de piso')."""
    s = unicodedata.normalize("NFKD", _html.unescape(str(s or "")).casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def classify(categoria: str, name: str = "") -> str | None:
    """Sub key from the shop category path + product name; the single rule for discover() and scrape().

    Floor cookers (fogão de piso) are gas ranges with oven -> gas_oven (induction/electric-hob ones map to induction/radiant);
    cooktop a gás and gas domino modules -> gas_cooktop; cooktop elétrico: indução -> induction, vitrocerâmico -> radiant;
    forno elétrico de embutir (incl. steam/pyrolytic) -> electric_oven; micro-ondas combinado (with oven) -> sco, micro-ondas
    com grill -> microwave. Hoods, warming drawers, accessories, electric domino modules, benchtop (bancada) ovens -> None."""
    c, n = fold(categoria), fold(name)
    if any(w in c or w in n for w in ("coifa", "gaveta", "acessor", "exaustor", "depurador", "bancada")):
        return None
    if "domino" in c or "domino" in n:
        return "gas_cooktop" if re.search(r"\bgas\b", c + " " + n) and "eletric" not in c else None
    if "micro" in c or re.search(r"micro-?ondas", n):
        return "sco" if "combinado" in n else "microwave"
    if "fogao de piso" in c or "fogao de piso" in n:
        if "inducao" in n:
            return "induction"
        if "gas" in n:
            return "gas_oven"
        return "radiant" if re.search(r"eletric|vitroceram", n) else "gas_oven"
    if "cooktop" in c or n.startswith("cooktop"):
        if "inducao" in c + " " + n:
            return "induction"
        if re.search(r"\bgas\b", c + " " + n):
            return "gas_cooktop"
        return "radiant" if re.search(r"eletric|vitroceram", c + " " + n) else None
    if n.startswith("forno") or "forno" in c:
        return "gas_oven" if re.search(r"\bgas\b", n) else "electric_oven"
    return None


_FINISH = (("black_stainless", r"preto.*inox|black stainless"), ("stainless", r"inox|\baco\b"), ("white", r"branco"),
           ("black", r"\b(?:preto|nero)\b"), ("slate", r"ardosia|antracite"))
_WIDTH = re.compile(r"(\d{2,3})(?:[/,]\d{2,3})*\s*cm\b", re.I)
_LITRES = re.compile(r"\b(\d{2,3})\s*(?:l|litros)\b", re.I)
_BURNERS = re.compile(r"\b(\d)\s*queimadores\b", re.I)


def finish_pt(text: str) -> str | None:
    t = fold(text)
    return next((k for k, pat in _FINISH if re.search(pat, t)), None)


def name_attrs(name: str, sub: str) -> dict:
    """Standard-unit facts the product name states (width, volume, burners, finish); unknown = key absent."""
    attrs: dict = {}
    nums = [int(x) for x in re.findall(r"\d{2,3}", " ".join(m.group(0) for m in _WIDTH.finditer(name)))]
    if nums:
        width = nums[-1]  # '70/75 cm' -> the larger size
        compact = width == 45 and sub in ("microwave", "sco", "electric_oven")  # '45 cm' on compact ovens is the height
        if 20 <= width <= 130 and not compact:
            attrs["width_in"] = round(units.mm_to_in(width * 10), 1)
    m = _LITRES.search(name)
    if m and sub in ("microwave", "sco", "electric_oven", "gas_oven"):
        attrs["capacity_total_cuft"] = round(units.l_to_cuft(float(m.group(1))), 2)
    m = _BURNERS.search(name)
    if m and sub in ("gas_cooktop", "gas_oven"):
        attrs["burners"] = int(m.group(1))
    fin = finish_pt(name)
    if fin:
        attrs["finish"] = fin
    fuel = {"gas_cooktop": "gas", "gas_oven": "gas", "induction": "induction", "radiant": "electric"}.get(sub)
    if fuel and not (sub == "gas_oven" and re.search(r"eletric", fold(name))):
        attrs["fuel"] = fuel
    return attrs


# ---------------------------------------------------------------- discover
def parse_listing(html: str) -> list[dict]:
    """All product dicts of the page's `itens: [...]` JS literals (a page may carry several), in page order."""
    items, dec = [], json.JSONDecoder()
    for m in re.finditer(r"itens:\s*\[", html):
        try:
            arr, _ = dec.raw_decode(html[m.end() - 1:])
        except ValueError:
            continue
        items.extend(i for i in arr if isinstance(i, dict) and i.get("codigo") and i.get("nome") and i.get("link"))
    return items


def _price(value) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def _site_signals(nota, reviews) -> dict:
    """rating (0-5) and review_count only when the shop shows reviews (nota/avaliacoes are 0.00/0 for unreviewed items)."""
    try:
        r, n = float(nota), int(reviews)
    except (TypeError, ValueError):
        return {}
    return {"rating": round(r, 2), "review_count": n} if n > 0 and 0 < r <= 5 else {}


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    link = item.get("link") or ""
    name = " ".join(_html.unescape(str(item["nome"])).split())
    if not _SLUG.match(link) or link.lstrip("/") in _NOT_PRODUCT or classify(item.get("categoria", ""), name) != sub:
        return None
    model = str(item["codigo"]).strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$", model):
        return None
    attrs = name_attrs(name, sub)
    attrs.update(_site_signals(item.get("nota"), item.get("avaliacoes")))
    price = None if item.get("ocultar_preco_site") else _price(item.get("valor"))
    return Candidate(brand=BRAND, model_number=model, name=name, url=BASE + link, price_usd=None, category="cooking",
                     subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY, price_local=price, attrs=attrs,
                     attrs_src={k: "name" if k in ("width_in", "capacity_total_cuft", "burners", "finish", "fuel") else "listing"
                                for k in attrs})


_LISTING_CACHE: dict[int, tuple[float, list[dict]]] = {}  # page -> (fetched at, items): one search asks every sub in a row
LISTING_TTL_S = 300.0


def listing_page(page: int) -> list[dict]:
    hit = _LISTING_CACHE.get(page)
    if hit and time.monotonic() - hit[0] < LISTING_TTL_S:
        return hit[1]
    items = parse_listing(fetch_html(f"{BASE}/{LISTING}?p={page}", "itens:"))
    _LISTING_CACHE[page] = (time.monotonic(), items)
    return items


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Smeg BR")
    found: dict[str, Candidate] = {}
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        try:
            items = listing_page(page)
        except SmegBrNotFound:
            break
        fresh = [i for i in items if i["codigo"] not in seen]
        seen.update(i["codigo"] for i in fresh)
        for item in fresh:
            c = item_to_candidate(item, subcategory)
            if c:
                found.setdefault(c.model_number, c)
        print(f"smeg_br: discover({subcategory}) p{page}: {len(items)} listed, {len(found)} kept", file=sys.stderr)
        if not fresh or len(found) >= limit:
            break
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape
def url_check(url: str) -> str:
    """The product slug of an https smegbrasil.com.br product URL; ValueError otherwise."""
    u = urlparse(url)
    if (u.scheme != "https" or not host_in(url) or u.query or u.fragment or u.username or not _SLUG.match(u.path)
            or u.path.lstrip("/") in _NOT_PRODUCT):
        raise ValueError(f"not a supported Smeg BR product URL: {url}")
    return u.path.lstrip("/")


# Portuguese label -> English for the fields this adapter reads or that the site repeats on every page (folded keys).
LABELS_PT = {
    "cores": "Colour", "colecoes especiais": "Special collection", "voltagem": "Voltage", "largura": "Width",
    "queimadores": "Burners", "tipo de instalacao": "Installation type", "especificacoes pdf": "Specification PDF",
    "dimensoes do produto (mm)": "Product dimensions (H x W x D) (mm)", "dimensoes do produto (axlxp)": "Product dimensions (H x W x D) (mm)",
    "peso liquido (kg)": "Net weight", "peso bruto (kg)": "Gross weight", "voltagem (v)": "Voltage", "tensao (v)": "Voltage",
    "tensao": "Voltage", "corrente": "Current", "frequencia (hz)": "Frequency", "frequencia": "Frequency",
    "classe de eficiencia energetica": "Energy efficiency class", "volume util da cavidade": "Usable cavity volume",
    "volume liquido da cavidade": "Net cavity volume", "potencia de micro-ondas": "Microwave power",
    "numero total de zonas de coccao": "Total number of cooking zones", "numero de prateleiras": "Number of shelves",
    "comprimento do cabo de alimentacao": "Power cord length", "tipo de gas": "Gas type",
}
SECTIONS_PT = {
    "tipo": "Type", "estetica": "Design", "informacoes do produto": "Product information", "familia de produtos": "Product information",
    "informacoes logisticas": "Logistics", "conexao eletrica": "Electrical connection", "caracteristicas tecnicas": "Technical features",
    "zonas de cozimento": "Cooking zones", "desempenho / etiqueta energetica": "Performance / Energy label",
    "desempenho / rotulo energetico": "Performance / Energy label", "especificacoes de desempenho e etiqueta energetica": "Performance / Energy label",
    "caracteristicas gerais": "General", "conexao a gas": "Gas connection", "controles": "Controls", "opcoes": "Options",
    "programas / funcoes": "Programs / Functions", "programas / funcoes do forno principal": "Programs / Functions",
    "programa / funcoes": "Programs / Functions", "caracteristicas": "Features", "caracteristicas tecnicas do forno principal": "Technical features",
    "caracteristicas tecnicas do fogao": "Technical features",
}
_COLOURS = {"preto": "Black", "nero": "Black", "branco": "White", "inox": "Stainless steel", "creme": "Cream", "antracite": "Anthracite",
            "cinza": "Grey", "azul": "Blue", "prata": "Silver"}
_LONG = 80  # a value longer than this is marketing prose about a feature: extra_specs gets 'Yes'


def _html_text(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def spec_rows(descricao: str) -> list[tuple[str, str, str]]:
    """[(section, label, value)] of the description HTML: `<strong>Section</strong>` headings (a colon-free bold text) followed by
    blocks whose `<br />`-separated lines are 'Label: value' (prose, questions and over-long labels are skipped; <style> dropped)."""
    def heading(m):
        text = _html_text(m.group(1))
        return f"\n##H## {text}\n" if text and ":" not in text and len(text) <= 80 else text

    marked = re.sub(r"(?is)<(style|script)\b.*?</\1\s*>", " ", descricao)
    marked = re.sub(r"(?is)<strong>(.*?)</strong>", heading, marked)
    marked = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", marked)
    rows, section = [], ""
    for line in marked.splitlines():
        line = line.strip()
        if line.startswith("##H##"):
            section = line[5:].strip()
            continue
        text = _html_text(line)
        label, sep, value = text.partition(":")
        label, value = label.strip(), value.strip()
        if sep and value and 2 <= len(label) <= 70 and len(label.split()) <= 9 and not re.search(r"[.!?]|\d+\s*%", label):
            rows.append((section, label, value))
    return rows


def _json_after(html: str, marker: str, opener: str):
    i = html.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(html[i + len(marker) - len(opener):])[0]
    except ValueError:
        return None


def product_json(html: str) -> dict:
    """The schema.org Product block of the page (valid JSON on this platform)."""
    m = re.search(r'<script type="application/ld\+json" id="product-schema">(.*?)</script>', html, re.S)
    if not m:
        raise SmegBrError("product-schema block not found (structure changed?)")
    try:
        d = json.loads(m.group(1))
    except ValueError as e:
        raise SmegBrError(f"product-schema is not valid JSON ({e})") from e
    if not isinstance(d, dict) or not d.get("sku"):
        raise SmegBrError("product-schema has no sku")
    return d


def page_data(html: str, sku: str) -> dict:
    """categoria, release date, rating, description rows and characteristics from the page's JS product JSON."""
    out: dict = {"descricao": _json_after(html, '"descricao":"', '"') or "",
                 "caracteristicas": _json_after(html, '"caracteristicas":[', "[") or []}
    m = re.search(r'"codigo":"%s","categoria":"((?:[^"\\]|\\.)*)"(.{0,700})' % re.escape(sku), html, re.S)
    if m:
        out["categoria"] = json.loads('"' + m.group(1) + '"')
        d = re.search(r'"data_lancamento":"(\d{4}-\d{2}-\d{2})"', m.group(2))
        out["release"] = d.group(1) if d else None
        v = re.search(r'"valor":"([\d.]+)"', m.group(2))
        out["valor"] = v.group(1) if v else None
    r = re.search(r'"nota":"([\d.]+)","avaliacoes":(\d+)', html)
    out["signals"] = _site_signals(r.group(1), r.group(2)) if r else {}
    return out


def _get(rows, pattern: str, section: str | None = None) -> str | None:
    for sec, lab, val in rows:
        if re.search(pattern, fold(lab)) and (section is None or re.search(section, fold(sec))):
            return val
    return None


def _kg(text: str | None) -> float | None:
    """'39,4 kg' / '19,300 kg' / '91.000 kg' -> kg (a dot or comma followed by 1-3 digits is always the decimal mark here)."""
    m = re.search(r"(\d[\d.,]*)\s*kg", text or "", re.I)
    if not m:
        return None
    s = m.group(1)
    s = s.replace(".", "").replace(",", ".") if "," in s else s
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 < v < 1000 else None


def _num(pattern: str, text: str | None) -> float | None:
    m = re.search(pattern, text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def english_table(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """{'Section > Label': value} in English: built-in terms, then i18n 'pt'; over-long prose values collapse to 'Yes'."""
    tr = i18n.get("pt")
    names = list(dict.fromkeys([l for _, l, _ in rows if fold(l) not in LABELS_PT] + [s for s, _, _ in rows if s and fold(s) not in SECTIONS_PT]))
    en = dict(zip(names, tr.translate_many(names, "label"))) if names else {}
    shorts = list(dict.fromkeys(v for _, _, v in rows if len(v) <= _LONG))
    vals = dict(zip(shorts, tr.translate_many(shorts, "value"))) if shorts else {}
    table: dict[str, str] = {}
    for sec, lab, val in rows:
        label = LABELS_PT.get(fold(lab)) or en.get(lab) or lab
        section = SECTIONS_PT.get(fold(sec)) or en.get(sec) or sec or "General"
        key = f"{section} > {label}"
        value = vals.get(val, val) if len(val) <= _LONG else "Yes"
        table[key] = f"{table[key]} | {value}" if key in table and value not in table[key] else table.get(key, value)
    return table


def doc_links(caracteristicas: list) -> list[tuple[str, str]]:
    """[(doc_type, url)] from the 'Especificações PDF' characteristic (a path on the shop's CDN)."""
    out = []
    for c in caracteristicas:
        path = str(c.get("valor") or "") if isinstance(c, dict) else ""
        if fold(c.get("nome") if isinstance(c, dict) else "").startswith("especifica") and re.match(r"^img/[\w./-]+\.pdf$", path):
            url = f"https://{IMAGE_HOSTS[0]}/{path}"
            out.append(("Manual" if "manual" in path.lower() else "SpecSheet", url))
    return out


def parse_product(url: str, html: str) -> tuple[ProductRecord, list[RawSpec], list[tuple[str, str]]]:
    ld = product_json(html)
    model = str(ld["sku"]).strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$", model):
        raise SmegBrError(f"unexpected model code {model!r}")
    name = " ".join(_html.unescape(str(ld.get("name") or model)).split())
    data = page_data(html, model)
    chars = [c for c in data["caracteristicas"] if isinstance(c, dict) and c.get("nome")
             and not str(c.get("valor") or "").lower().endswith(".pdf")]
    rows = [(str(c.get("grupo") or "General"), str(c["nome"]), str(c["valor"])) for c in chars if c.get("valor")]
    desc_rows = spec_rows(_html.unescape(data["descricao"])) if data["descricao"] else []
    allrows = rows + desc_rows
    sub = classify(data.get("categoria", ""), name)
    tr = i18n.get("pt")

    hwd = re.findall(r"\d+(?:[.,]\d+)?", _get(desc_rows, r"^dimensoes do produto \((?:mm|axlxp)\)$") or "")
    hwd = [float(x.replace(",", ".")) for x in hwd] if len(hwd) == 3 else None
    kg = _kg(_get(desc_rows, r"^peso liquido"))
    litres = _num(r"([\d.,]+)\s*(?:l\b|litros)", _get(desc_rows, r"^volume (?:util|liquido) da cavidade"))
    cls = _get(desc_rows, r"^classe de eficiencia energetica")
    extra = english_table(allrows)
    eu_class = units.eu_energy_class(cls) if cls else None
    if eu_class:  # 'EU class A+' replaces the bare letter wherever the page put it
        keys = [k for k in extra if k.endswith("> Energy efficiency class")] or ["General > Energy efficiency class"]
        for k in keys:
            extra[k] = eu_class
    colour = next((c["valor"] for c in chars if fold(c["nome"]) == "cores"), None)
    colour_en = None
    if colour:
        colour_en = _COLOURS.get(fold(colour)) or tr.translate_value(str(colour))
    price = _price(ld.get("offers", {}).get("price") if isinstance(ld.get("offers"), dict) else None) or _price(data.get("valor"))
    volts = (_get(desc_rows, r"^(?:tensao|voltagem)") or "").replace(" V", "").strip() or None
    intro = [_html_text(x) for x in re.findall(r"(?is)<li[^>]*>(.*?)</li>", _html.unescape(data["descricao"]))]
    images = ld.get("image")
    image = images[0] if isinstance(images, list) and images else images if isinstance(images, str) else None
    if not (isinstance(image, str) and urlparse(image).scheme == "https" and host_in(image, IMAGE_HOSTS)):
        image = None
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name, category="cooking", subcategory=sub, product_url=url,
        finish_color=colour_en, region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None, price_local=price,
        capacity_total_cuft=round(units.l_to_cuft(litres), 2) if litres else None,
        height_in=round(units.mm_to_in(hwd[0]), 1) if hwd else None, width_in=round(units.mm_to_in(hwd[1]), 1) if hwd else None,
        depth_in=round(units.mm_to_in(hwd[2]), 1) if hwd else None,
        weight_lb=round(units.kg_to_lb(kg), 1) if kg is not None else None,
        voltage_v=volts, amps=_num(r"([\d.,]+)\s*A\b", _get(desc_rows, r"^corrente")),
        frequency_hz=_num(r"(\d+)", _get(desc_rows, r"^frequencia")),
        pod_features=tr.translate_many(intro[:8], "value") if intro else [],
        extra_specs=extra, image_url=image, **data["signals"],
        **({"release_date": data["release"], "release_src": "distribution"} if data.get("release") else {}))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=s, key=l, value=v) for s, l, v in allrows]
    return record, raw, doc_links(data["caracteristicas"])


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    url_check(url)
    html = fetch_html(url, "product-schema")
    record, raw, links = parse_product(url, html)
    docs: list[DocumentRecord] = []
    for dtype, durl in links:
        rec = common.download_pdf(BRAND, record.model_number, dtype, durl)
        if rec:
            docs.append(rec)
    return record, docs, raw
