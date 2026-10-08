"""LG Brazil adapter (https://www.lg.com/br/): cooking appliances (contract: catalog.py).

Brazilian Portuguese site, prices in BRL. LG Brasil sells ONLY microwave ovens in the cooking group today (verified
2026-10-08: 8 ACTIVE models, "Micro-Ondas Solo" / "Micro-Ondas Grill"); its wall oven (LSWS306ST), gas cooktop (LSCG367ST),
hoods and convection microwave (MJ3967) are all DISCONTINUED and are not listed. There is no fogao / induction / OTR / SCO
product, so SUPPORTED_SUBCATEGORIES is {"microwave"}; classify() still files such a product correctly if one appears.

Data sources (what the pages themselves load; plain requests work, headless Chromium gets "Access Denied" from Akamai so a
visible window is the last-resort fallback only):
  discover: Coveo search (the listing page's own backend). A short-lived anonymous token comes from
            GET /ncms/latam/api/v1/coveo/token, then POST platform-eu.cloud.coveo.com/rest/search/v2 (organizationId
            lgcorporationproduction0fxcu0qx, searchHub BR-B2C-Search) with cq = ACTIVE cooking models. Per model:
            `ec_price` (selling price), `ec_msrp` (struck-through), `ec_cheaper_price` (PIX/card price, ignored),
            `ec_review_score` + `ec_review_rating` (average and COUNT), `ec_model_year`, `ec_specs` (spec summary).
  scrape:   GET the PDP (`c-compare-selling__table` blocks = the complete spec table; schema.org Product JSON-LD for the image)
            + one Coveo lookup by `ec_model_url_path` for the price block and rating.
Price rule: the PDP JSON-LD price is the 5% PIX price (e.g. 664.05) -- NOT used. price_local = `ec_price` (699.00), the price
without the PIX/card discount; the struck-through MSRP and the PIX price are only recorded in extra_specs. No conversion.
Signals: rating (0-5) and review_count from `ec_review_score` / `ec_review_rating` when the count is > 0; release_date =
`ec_model_year` (the model year the PDP states, release_src 'site'); is_new is never set (the Coveo `ec_target`='NEW' flag
appears on discontinued 2013 models too, so it is not a NEW badge).
Documents: the manual list of lg.com/br is loaded by a login-less XHR that only runs in a browser, and headless Chromium is
refused by the site, so no PDF is downloaded (documents == []).
Translation: spec labels / values are Portuguese; English comes from samsung_br.translate_many (curated cooking vocabulary,
then i18n.get('pt')); RawSpec keeps the original text.
Robots: https://www.lg.com/robots.txt has no rule for /br/ product pages or /ncms/ (only /br/partners, /br/small-medium-business,
/*/search* ... which are not used). Terms: public product data only, >= 1 s between requests.
Fetch order: requests, then headless, then a visible window (FRIDGE_BROWSER_MODE = auto | headless | visible), cookie banner
declined first. Hosts to allow: www.lg.com (pages, images under /content/dam/) is covered by the existing "lg.com" entry; no
PDF host is needed. The Coveo API host (platform-eu.cloud.coveo.com) is called by this adapter only, never downloaded from.
"""
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

import units
from catalog import Candidate
from samsung_br import (Net, clean, dims_in, fmt, fold, pos_float, pt_number, translate_many, yes_no,
                        classify as _classify_generic)
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "LG"
COUNTRY = "br"
REGION = "sa"
CURRENCY = "BRL"
BASE = "https://www.lg.com"
SITE = "/br/"
TOKEN_URL = BASE + "/ncms/latam/api/v1/coveo/token"
COVEO_HOST = "platform-eu.cloud.coveo.com"
COVEO_ORG = "lgcorporationproduction0fxcu0qx"
COVEO_URL = f"https://{COVEO_HOST}/rest/search/v2?organizationId={COVEO_ORG}"
SEARCH_HUB = "BR-B2C-Search"
PAGE_SIZE = 200

# Sold today: see the module docstring.
SUPPORTED_SUBCATEGORIES = {"microwave"}
UNSUPPORTED_SUBCATEGORIES = {"sco", "otr", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"}

_FIELDS = ["ec_model_name", "ec_user_friendly_name", "ec_model_url_path", "ec_price", "ec_msrp", "ec_original_price",
           "ec_cheaper_price", "ec_review_score", "ec_review_rating", "ec_model_year", "ec_category_name",
           "ec_sub_category_name", "ec_classification_flag_lv_4", "ec_model_status_code", "ec_salable_status",
           "ec_specs", "ec_large_image_addr"]
_ACTIVE_COOKING = '@ec_model_status_code==ACTIVE @ec_bu_name_3=="Cooking Appliance" @ec_model_type==G'
_PATH_RE = re.compile(r"^/br/[a-z0-9][a-z0-9/_.\-]{2,200}/$")


class LgBrPageError(RuntimeError):
    """An LG BR page/API did not have the expected structure, left /br/, or was blocked."""


# ---------------------------------------------------------------- url helpers
def _is_lg_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and not parts.username and (host == "lg.com" or host.endswith(".lg.com"))


def is_br_page(url: str) -> bool:
    """https lg.com URL inside the Brazilian site (/br/...)."""
    return _is_lg_host(url) and (urlsplit(url).path or "").startswith(SITE)


def _allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if is_br_page(url) or (_is_lg_host(url) and parts.path.startswith("/ncms/latam/api/")):
        return True
    return (parts.scheme == "https" and (parts.hostname or "").lower() == COVEO_HOST
            and parts.path.startswith("/rest/search/v2") and not parts.username)


def _require_br(url: str) -> None:
    if not is_br_page(url):
        raise LgBrPageError(f"not an LG Brasil page (https lg.com/br only): {url!r}")


def _safe_model(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", model).strip(".")


def _net() -> Net:
    return Net(BASE + SITE, _allowed, name="lg_br", lang="pt-BR", error=LgBrPageError)


# ---------------------------------------------------------------- classification (shared by discover and scrape)
def classify(name: str, path: str = "", model: str = "", category: str = "") -> str | None:
    """THE single classifier: catalog sub key or None (hoods, tabletop toaster ovens, unknown). `path` is the PDP url path
    ('/br/micro-ondas/micro-ondas-solo/ms3043br/'), `category` the site's category text ('Micro-Ondas Solo'). A built-in
    oven ('forno ... de embutir') wins over the microwave path (LSWS306ST sits under /micro-ondas/); a microwave that names an
    electric/convection oven (NeoChef Convection) is a speed-cook (sco); everything else goes through samsung_br.classify()."""
    n = fold(name)
    if re.search(r"coifa|depurador|exaustor", n) and not re.search(r"micro-?ondas", n):
        return None
    if re.search(r"forno", n) and re.search(r"embutir|built-?in", n) and not re.search(r"micro-?ondas", n):
        return "gas_oven" if re.search(r"\bga[sz]\b|glp", n) else "electric_oven"
    if re.search(r"micro-?ondas", n) or "/micro-ondas/" in (path or "").lower() or fold(category).startswith("micro-ondas"):
        if re.search(r"coifa|sobre o fogao|over[- ]the[- ]range", n):
            return "otr"
        if re.search(r"forno (eletrico|a gas)|convec|combina|combo", n):
            return "sco"
        return "microwave"
    return _classify_generic(name, model)


# ---------------------------------------------------------------- Coveo
def _token(net: Net) -> str:
    data = json.loads(net.text(TOKEN_URL, ok=lambda t: t.lstrip().startswith("{") and '"token"' in t))
    token = data.get("token")
    if not isinstance(token, str) or token.count(".") != 2:
        raise LgBrPageError("coveo token endpoint returned no token")
    return token


def _search(net: Net, token: str, cq: str, first: int = 0, number: int = PAGE_SIZE) -> dict:
    body = {"locale": "pt-BR", "searchHub": SEARCH_HUB, "cq": cq, "fieldsToInclude": _FIELDS,
            "numberOfResults": number, "firstResult": first}
    text = net.text(COVEO_URL, method="POST", headers={"Authorization": "Bearer " + token}, body=body,
                    ok=lambda t: t.lstrip().startswith("{") and '"results"' in t)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise LgBrPageError(f"coveo returned invalid JSON ({e})") from e
    if not isinstance(data.get("results"), list):
        raise LgBrPageError("coveo response has no 'results' list")
    return data


def parse_ec_specs(text: str) -> list[tuple[str, str]]:
    """Coveo `ec_specs` ('Label: value,;Label: value,;...') -> [(label, value)] (Portuguese, in order)."""
    out = []
    for piece in re.split(r",;", text or ""):
        label, sep, value = piece.rstrip(", ").partition(": ")
        if sep and clean(label) and clean(value):
            out.append((clean(label), clean(value)))
    return out


def _spec(pairs, label_pat: str) -> str | None:
    for label, value in pairs:
        if re.fullmatch(label_pat, fold(label)):
            return value
    return None


def _signals(raw: dict) -> dict:
    """rating / review_count / release_date the site publishes (nothing else: see the module docstring)."""
    out: dict = {}
    rating, count = pos_float(raw.get("ec_review_score")), None
    try:
        count = int(float(raw.get("ec_review_rating")))
    except (TypeError, ValueError):
        pass
    if rating is not None and rating <= 5 and count and count > 0:
        out["rating"] = round(rating, 2)
        out["review_count"] = count
    year = str(raw.get("ec_model_year") or "").strip()
    if re.fullmatch(r"(19|20)\d\d", year):
        out["release_date"], out["release_src"] = year, "site"
    return out


def _listing_attrs(raw: dict, sub: str | None) -> dict:
    pairs = parse_ec_specs(raw.get("ec_specs") or "")
    attrs: dict = {"fuel": "electric"} if sub in ("microwave", "sco", "electric_oven") else {}
    liters = pt_number(_spec(pairs, r"capacidade do forno \(l\)"))
    if liters:
        attrs["capacity_total_cuft"] = attrs["oven_capacity_cuft"] = round(units.l_to_cuft(liters), 2)
    watts = pt_number(_spec(pairs, r"potencia util do micro-ondas \(w\)"))
    if watts:
        attrs["microwave_watts"] = int(watts)
    width = dims_in(_spec(pairs, r"dimensoes do produto \(l x a x p\) \(mm\)") or "")[0]
    if width:
        attrs["width_in"] = width
    wifi = yes_no(_spec(pairs, r"thinq \(wi-?fi\)"))
    if wifi is not None:
        attrs["wifi"] = wifi
    air = yes_no(_spec(pairs, r"fritura a ar"))
    if air:
        attrs["air_fry"] = True
    conv = yes_no(_spec(pairs, r"cozer por convecc?ao"))
    if conv:
        attrs["convection"] = True
    attrs.update({k: v for k, v in _signals(raw).items()})
    return attrs


def _selling_price(raw: dict) -> float | None:
    """`ec_price`: the selling price without the PIX/card discount (0 / missing = not for sale)."""
    return pos_float(raw.get("ec_price"))


def parse_search(data: dict, sub: str | None = None, limit: int = 10 ** 6) -> list[Candidate]:
    """Coveo response -> Candidates (ACTIVE models only, deduped by model). With `sub`, only products classify() files there."""
    out: dict[str, Candidate] = {}
    for result in data.get("results") or []:
        raw = result.get("raw") or {}
        if raw.get("ec_model_status_code") not in (None, "ACTIVE"):
            continue
        model = clean(raw.get("ec_model_name"))
        path = raw.get("ec_model_url_path") or ""
        name = clean(raw.get("ec_user_friendly_name") or result.get("title") or model)
        category = " ".join(raw.get("ec_sub_category_name") or raw.get("ec_category_name") or [])
        found = classify(name, path, model, category)
        if not model or not _PATH_RE.match(path) or found is None or (sub is not None and found != sub):
            continue
        url = BASE + path
        if model in out or not is_br_page(url):
            continue
        attrs = _listing_attrs(raw, found)
        out[model] = Candidate(brand=BRAND, model_number=model, name=name, url=url, price_usd=None, category="cooking",
                               subcategory=found, region=REGION, country=COUNTRY, currency=CURRENCY,
                               price_local=_selling_price(raw), attrs=attrs, attrs_src={k: "listing" for k in attrs})
        if len(out) >= limit:
            break
    return list(out.values())


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"LG BR adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    with _net() as net:
        token = _token(net)
        first, total = 0, None
        while len(found) < limit and (total is None or first < total):
            data = _search(net, token, _ACTIVE_COOKING, first)
            for c in parse_search(data, subcategory):
                found.setdefault(c.model_number, c)
            got = len(data["results"])
            total = int(data.get("totalCount") or 0) if got else first
            first += got
    return list(found.values())[:limit]


# ---------------------------------------------------------------- PDP
_TABLE = re.compile(r'<div class="c-compare-selling__table">(.*?)</ul>\s*</div>', re.S)
_HEAD = re.compile(r"<h4[^>]*>(.*?)</h4>", re.S)
_ROW = re.compile(r'c-compare-selling__spec-name">(.*?)</div>\s*<div[^>]*c-compare-selling__spec-desc">(.*?)</div>\s*</li>', re.S)
_LD_JSON = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def parse_spec_rows(page_html: str) -> list[tuple[str, str, str]]:
    """Every row of the server-rendered spec table: (section, label, value) in Portuguese, page order."""
    rows = []
    for block in _TABLE.findall(page_html):
        head = _HEAD.search(block)
        section = clean(head.group(1)) if head else ""
        for label, value in _ROW.findall(block):
            label, value = clean(label), clean(value)
            if label and value:
                rows.append((section, label, value))
    return rows


def parse_product_ld(page_html: str) -> dict:
    for raw in _LD_JSON.findall(page_html):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and str(data.get("@type", "")).lower() == "product":
            return data
    return {}


def _image_url(ld: dict, raw: dict) -> str | None:
    cand = ld.get("image") if isinstance(ld.get("image"), str) else None
    if not cand and raw.get("ec_large_image_addr"):
        cand = "/content/dam/channel/wcms" + raw["ec_large_image_addr"]
    if not cand:
        return None
    url = urljoin(BASE + SITE, cand.strip())
    return url if _is_lg_host(url) else None


def _pick(rows, label_pat: str, section_pat: str | None = None) -> str | None:
    for sec, label, value in rows:
        if re.fullmatch(label_pat, fold(label)) and (section_pat is None or re.search(section_pat, fold(sec))):
            return value
    return None


_FEATURE_SECTIONS = ("recursos", "modos de cozimento", "tecnologia", "caracteristicas do forno micro")


def build_record(url: str, ld: dict, rows, raw: dict) -> ProductRecord:
    """Pure function: parsed PDP + spec rows + Coveo record -> ProductRecord."""
    model = clean(raw.get("ec_model_name") or ld.get("mpn") or "")
    name = clean(raw.get("ec_user_friendly_name") or ld.get("name") or model)
    category = " ".join(raw.get("ec_sub_category_name") or raw.get("ec_category_name") or [])
    sub = classify(name, urlsplit(url).path, model, category)
    if sub is None:
        raise LgBrPageError(f"{url} is not a supported cooking product (hood, accessory or unknown type)")
    price = _selling_price(raw)
    liters = pt_number(_pick(rows, r"capacidade do forno \(l\)"))
    w, h, d = dims_in(_pick(rows, r"dimensoes do produto \(l x a x p\) \(mm\)") or "")
    weight_kg = pt_number(_pick(rows, r"peso do produto \(kg\)"))
    power = _pick(rows, r"fonte de alimentacao necessaria \(volt/hz\)") or ""
    volt, hz = re.search(r"(\d{2,3})\s*V", power), re.search(r"(\d{2})\s*Hz", power, re.I)
    watts = pt_number(_pick(rows, r"potencia util do micro-ondas \(w\)"))
    thinq = next(((yes_no(v), f"{s} > {l} = {v}") for s, l, v in rows if re.fullmatch(r"thinq \(wi-?fi\)", fold(l))),
                 (None, None))
    finish_pt = _pick(rows, r"cor externa")
    energy = units.br_energy_class(_pick(rows, r"classe de eficiencia energetica"))

    labels_pt = list(dict.fromkeys(t for sec, label, _ in rows for t in (sec, label) if t))
    labels = dict(zip(labels_pt, translate_many(labels_pt, "label")))
    values = translate_many([v for _, _, v in rows], "value")
    table: dict[str, str] = {}
    for (sec, label, _), val in zip(rows, values):
        key = f"{labels[sec]} > {labels[label]}" if sec else labels[label]
        table[key] = f"{table[key]} | {val}" if key in table else val
    pod = [labels[l] for s, l, v in rows if fold(s).startswith(_FEATURE_SECTIONS) and fold(v) == "sim"]

    std: dict[str, str] = {"Fuel": "electric"} if sub in ("microwave", "sco", "electric_oven") else {}
    if liters:
        std["Oven capacity (L)"] = fmt(liters)
        std["Oven capacity (cu ft)"] = fmt(round(units.l_to_cuft(liters), 2))
    if watts:
        std["Microwave output power (W)"] = fmt(watts)
    for key, val in zip(("Width (mm)", "Height (mm)", "Depth (mm)"),
                        dims_in(_pick(rows, r"dimensoes do produto \(l x a x p\) \(mm\)") or "", 4)):
        if val is not None:
            std[key] = fmt(round(units.in_to_mm(val)))
    if weight_kg:
        std["Weight (kg)"] = fmt(weight_kg)
    if energy:
        std["Energy class"] = energy
    if price:
        std["List price (BRL)"] = fmt(price)
    msrp, pix = pos_float(raw.get("ec_msrp")), pos_float(raw.get("ec_cheaper_price"))
    if msrp and msrp != price:
        std["Struck-through MSRP (BRL)"] = fmt(msrp)
    if pix and price and pix < price:
        std["PIX/card price excluded (BRL)"] = fmt(pix)
    std["Price basis"] = "lg.com/br selling price (ec_price); excludes PIX/card discount prices and the struck-through MSRP"
    std["Source language"] = "pt-BR (labels/values translated; originals in RawSpec)"

    return ProductRecord(
        brand=BRAND, model_number=model, product_name=name, product_url=url, category="cooking", subcategory=sub,
        finish_color=translate_many([finish_pt], "value")[0] if finish_pt else None,
        region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None, price_local=price,
        capacity_total_cuft=round(units.l_to_cuft(liters), 2) if liters else None,
        width_in=w, height_in=h, depth_in=d,
        weight_lb=None if not weight_kg else round(units.kg_to_lb(weight_kg), 1),
        voltage_v=volt.group(1) if volt else None, frequency_hz=float(hz.group(1)) if hz else None,
        energy_kwh_year=None, energy_star=None, wifi_supported=thinq[0], wifi_evidence=thinq[1],
        pod_features=list(dict.fromkeys(pod)), extra_specs={**table, **std}, image_url=_image_url(ld, raw),
        **_signals(raw),
    )


def lookup(net: Net, token: str, url: str) -> dict:
    """The Coveo `raw` record of a PDP (by its url path, then by model name); LgBrPageError when the site has none."""
    path = urlsplit(url).path
    if not _PATH_RE.match(path):
        raise LgBrPageError(f"unexpected LG BR product path {path!r}")
    for cq in (f'@ec_model_url_path=="{path}"', f'@ec_model_name=="{path.rstrip("/").rsplit("/", 1)[-1].upper()}"'):
        results = _search(net, token, cq, 0, 5)["results"]
        for r in results:
            raw = r.get("raw") or {}
            if raw.get("ec_model_url_path") == path or cq.startswith("@ec_model_name"):
                return raw
    raise LgBrPageError(f"{url}: no product record found in the LG BR catalog search")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _require_br(url)
    with _net() as net:
        page_html = net.text(url, as_page=True, ok=lambda t: "c-compare-selling__table" in t)
        rows = parse_spec_rows(page_html)
        if not rows:
            raise LgBrPageError(f"{url}: no specification table found on the page")
        try:
            raw = lookup(net, _token(net), url)
        except LgBrPageError as exc:
            print(f"lg_br: catalog record unavailable ({exc}); price/rating left empty", file=sys.stderr)
            raw = {}
    ld = parse_product_ld(page_html)
    if not raw:
        raw = {"ec_user_friendly_name": ld.get("name"), "ec_model_name": ld.get("mpn")}
    product = build_record(url, ld, rows, raw)
    rawspecs = [RawSpec(brand=BRAND, model_number=product.model_number, source="web", section=s, key=k, value=v)
                for s, k, v in rows]
    return product, [], rawspecs
