"""Siemens DE adapter (catalog.py contract): cooking only (ovens, microwaves, cooktops), German site, EUR prices.

siemens-home.bsh-group.com/de/de is the same BSH Next.js platform as bosch-home.com / thermador.com (core in
thermador_us.py): category pages `/de/de/category/<path>[?pageNumber=N]` (12 items/page, SSR productList with
`price` {SHOP_PRICE, EUR, VAT incl.}; only some cooking items carry a shop price, the rest are None) and product pages
`/de/de/product/<path>/<MODEL>` with a LABELED spec table (German labels, platform keys such as DIM, WEIGHT_NET are
language independent). Robots.txt (checked 2026-10-06) disallows /manual/ (interactive manuals; never fetched),
*/search/*, */comparison/*, graphql, ajax. Siemens sells no appliances in the US, hence only 'de'.
Labels/values are English via a built-in key table (stable platform keys) then i18n.get('de') (glossary, cache, local
LLM); RawSpec keeps the German original. EU energy class is stored as 'EU class X' (extra_specs only). The discount
badge ("15 % Rabatt im Warenkorb") is a cart promotion and is ignored; the price is the list shop price.
"""
import re
import sys
import time
from urllib.parse import urlparse

import i18n
import units
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec
from thermador_us import (Site, boolean, clean_name, download_docs, flight_text, item_price, item_url, item_wifi,
                          iter_listing, json_after, main_image_url, session, spec_rows, title_of, host_in, DELAY_S)

BRAND = "Siemens"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
SITE = Site(BRAND, "siemens_de", "https://www.siemens-home.bsh-group.com/de/de", ("siemens-home.bsh-group.com",),
            ("bsh-group.com",), ("bsh-group.com",), "Optionales ablehnen", "Einverstanden")

# sub key -> category pages (under /category/). Not sold: gas ovens/ranges, over-the-range microwaves (no German
# market). Refrigerators/washers exist on the site but are out of scope this round (cooking only).
# Assumptions: sco = ovens with microwave function; electric_oven = built-in / compact / steam ovens without microwave;
# induction includes hobs with integrated extractor; radiant = glass-ceramic electric hobs; built-in ranges
# (Einbauherde), range sets and domino hobs are not classified.
_BO = "kochen-backen/backoefen-herde/"
_KF = "kochen-backen/kochfelder-kochstellen/"
SUB_SOURCES: dict[str, tuple[str, ...]] = {
    "microwave": ("kochen-backen/mikrowellen/einbau-mikrowellen", "kochen-backen/mikrowellen/freistehend"),
    "sco": (_BO + "backoefen-mit-mikrowelle", _BO + "kompakt-backoefen"),
    "electric_oven": (_BO + "einbau-backoefen", _BO + "kompakt-backoefen",
                      "kochen-backen/dampfgarer-dampfbackoefen/dampfbackoefen"),
    "gas_cooktop": (_KF + "gaskochfelder",),
    "induction": (_KF + "induktionskochfelder", _KF + "induktionskochfelder-aus-glaskeramik",
                  _KF + "kochfelder-integrierter-dunstabzug"),
    "radiant": (_KF + "glaskeramik-kochfelder",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
ROOT = "kochen-backen"
_CODE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")


def classify(path: str, name: str = "") -> str | None:
    """Sub key from a /product/ path + German product name; the single rule for discover() and scrape()."""
    rel = path.split("/product/", 1)[-1].strip("/").lower()
    n = (name or "").lower()
    if not rel.startswith(ROOT + "/") or "zubehoer" in rel or "einbau-herde" in rel or "herd-sets" in rel:
        return None
    if "/mikrowellen/" in "/" + rel:
        return "sco" if "backofen" in n else "microwave"
    if "backoefen-herde/" in rel or "dampfgarer-dampfbackoefen/" in rel:
        if "dampfgarer/" in rel:
            return None
        if "backofen" not in n:  # microwave units are listed under the compact-oven path
            return "microwave" if "mikrowelle" in n else None
        return "sco" if "mikrowelle" in n else "electric_oven"
    if "kochfelder-kochstellen/" in rel:
        if "induktion" in n or "induktionskochfelder" in rel or "integrierter-dunstabzug" in rel:
            return "induction"
        if "gas" in n or "gaskochfelder" in rel:
            return "gas_cooktop"
        return "radiant" if "glaskeramik-kochfelder" in rel and re.search(r"elektro|glaskeramik|kochfeld", n) else None
    return None


_FINISH_DE = (("black_stainless", r"schwarz\w*\s+(?:edel)?stahl|dark inox"), ("stainless", r"edelstahl|stahl|inox"),
              ("white", r"wei(?:ss|ß)"), ("black", r"schwarz"), ("slate", r"anthrazit|grau"))


def finish_de(text: str) -> str | None:
    t = (text or "").lower()
    return next((k for k, pat in _FINISH_DE if re.search(pat, t)), None)


def listing_attrs(item: dict, parts: list, sub: str) -> dict:
    """Candidate.attrs in standard units from what the listing states (name parts: series, type, size, colour)."""
    attrs: dict = {}
    size = " ".join(str(p) for p in parts[2:3])
    two = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*cm", size)
    one = re.search(r"(\d+(?:[.,]\d+)?)\s*cm", size)
    w = max(two.groups(), key=lambda x: float(x.replace(",", "."))) if two else one.group(1) if one else None
    if w and 20 <= float(w.replace(",", ".")) <= 120:
        attrs["width_in"] = round(units.mm_to_in(float(w.replace(",", ".")) * 10), 1)
    if item_wifi(item):
        attrs["wifi"] = True
    fin = finish_de(" ".join(str(p) for p in parts[3:]))
    if fin:
        attrs["finish"] = fin
    return attrs


def item_to_candidate(item: dict, sub: str) -> Candidate | None:
    url = item_url(SITE, item, "product", ROOT)
    if not url:
        return None
    parts = item.get("productName") or []
    name = clean_name(parts)
    if classify(item["urlPath"], name) != sub:
        return None
    price = item_price(item)
    attrs = listing_attrs(item, parts, sub)
    return Candidate(brand=BRAND, model_number=item["productCode"], name=name or item["productCode"], url=url,
                     price_usd=None, category="cooking", subcategory=sub, region=REGION, country=COUNTRY,
                     currency=CURRENCY, price_local=price, attrs=attrs, attrs_src={k: "listing" for k in attrs})


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUB_SOURCES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Siemens DE")
    found: dict[str, Candidate] = {}
    cats = SUB_SOURCES[subcategory]
    with session(SITE, f"{SITE.base}/category/{cats[0]}") as s:
        for cat in cats:
            listed, kept_before = 0, len(found)
            for item in iter_listing(s, cat, "category"):
                listed += 1
                c = item_to_candidate(item, subcategory)
                if c:
                    found.setdefault(c.model_number, c)
                if len(found) >= limit:
                    break
            print(f"siemens_de: discover({subcategory}) {cat}: {listed} listed, {len(found) - kept_before} kept",
                  file=sys.stderr)
            if len(found) >= limit:
                break
            time.sleep(DELAY_S)
    return list(found.values())[:limit]


# ---------------------------------------------------------------- scrape

def model_from_url(url: str) -> str:
    """Model from https://www.siemens-home.bsh-group.com/de/de/product/kochen-backen/.../<MODEL>; ValueError otherwise."""
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if (u.scheme != "https" or not host_in(url, SITE.page_hosts) or parts[:3] != ["de", "de", "product"]
            or len(parts) < 5 or ".." in parts or parts[3] != ROOT or not _CODE.match(parts[-1])):
        raise ValueError(f"not a supported Siemens DE cooking product URL: {url}")
    return parts[-1]


# Platform spec key -> English label (stable codes; anything else goes through i18n 'de').
KEY_LABELS = {
    "COL_MAIN": "Colour", "COL_DOOR": "Door colour/material", "COL_PANEL": "Control panel colour",
    "DIM": "Appliance dimensions (H x W x D) (mm)", "DIM_PACKED": "Packed dimensions (mm)",
    "WEIGHT_NET": "Net weight", "WEIGHT_GROSS": "Gross weight", "CORD_LGTH": "Connecting cable length",
    "VOLTAGE": "Voltage", "CONNECTION": "Connected load", "CURRENT": "Fuse rating", "TYPE_APPL_WIDTH": "Appliance width",
    "USE_VOL_CAVITY_2010": "Usable cavity volume", "CAP_CAVITY": "Cavity capacity",
    "MICRO_WAVE_MAX_POWER": "Maximum microwave power", "POWER_LEVELS_NUMBER": "Number of power levels",
    "TEMP_RANGE": "Available temperature range", "COOKING_METHOD": "Heating modes",
    "ANZAHL_BEHEIZUNGSARTEN": "Number of heating modes", "POSITIONS": "Number of cooking zones",
    "POWER_PLATES": "Cooking zone power", "DIM_PLATES": "Cooking zone size", "GAS_TYPE": "Gas type",
    "GAS_CONNECTION_RATING": "Gas connection rating", "COOKER_HOB_TYPE": "Hob type", "SURFACE_BASIC_MAT": "Main surface material",
    "HOMECONNECTABLE": "Home Connect", "HOMECONNECT_TYPE": "Home Connect features", "ACCESSORIES_INCL": "Included accessories",
    "ENERGY_CLASS_2010": "Energy efficiency class", "ENERGY_CLASS_2017": "Energy efficiency class",
    "ENERGY_CLASS_2019": "Energy efficiency class", "NICHE_SIZE": "Installation niche (H x W x D) (mm)",
}
SECTION_LABELS = {"GENERAL_PROPERTIES": "General", "DIMENSION_PROPERTIES": "Size and weight", "TECHNICAL_SPECIFICATIONS": "Technical data",
                  "COMFORT": "Comfort", "CONNECTIVITY": "Connectivity", "SAFETY": "Safety", "INSTALLATION": "Installation",
                  "DESIGN": "Design", "ACCESSORIES": "Accessories"}


def _mm_to_in(mm: float) -> float:
    return round(units.mm_to_in(mm), 1)


def english_table(rows: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """({'Section > Label': value}, {key: translated value}) in English; one label batch and one value batch to i18n."""
    tr = i18n.get("de")
    labels = [r["label"] for r in rows if r["key"] not in KEY_LABELS]
    secs = [r["sec"] for r in rows if r["sec_key"] not in SECTION_LABELS]
    names = dict(zip(labels + secs, tr.translate_many(labels + secs, "label"))) if labels or secs else {}
    needs = list(dict.fromkeys(r["value"] for r in rows if r["value"] not in ("Yes", "No")))
    vals = dict(zip(needs, tr.translate_many(needs, "value"))) if needs else {}
    table: dict[str, str] = {}
    by_key: dict[str, str] = {}
    for r in rows:
        label = KEY_LABELS.get(r["key"]) or names.get(r["label"]) or r["label"]
        sec = SECTION_LABELS.get(r["sec_key"]) or names.get(r["sec"]) or r["sec"]
        val = vals.get(r["value"], r["value"])
        key = f"{sec} > {label}"
        table[key] = f"{table[key]} | {val}" if key in table else val
        by_key.setdefault(r["key"], val)
    return table, by_key


def _first_float(text: str, pattern: str = r"([\d.,]+)") -> float | None:
    m = re.search(pattern, text or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_product(model: str, url: str, flight: str) -> tuple[ProductRecord, list[RawSpec]]:
    """Product record (web data) and the German RawSpec table from a Siemens DE product-page payload."""
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
    raw_by_key: dict[str, str] = {}
    for r in rows:
        raw_by_key.setdefault(r["key"], r["value"])
    table, by_key = english_table(rows)
    tr = i18n.get("de")
    name_en = clean_name([title_class, tr.translate_value(headline)]) or model

    dim = re.findall(r"\d+(?:[.,]\d+)?", raw_by_key.get("DIM", ""))
    hwd = [float(x.replace(",", ".")) for x in dim] if len(dim) == 3 else None
    kg = _first_float(raw_by_key.get("WEIGHT_NET", ""))
    cls_key = next((k for k in raw_by_key if k.startswith("ENERGY_CLASS")), None)
    eu_class = units.eu_energy_class(raw_by_key.get(cls_key, "")) if cls_key else None
    if eu_class:
        table["General > Energy efficiency class"] = eu_class
    kwh_key = next((k for k in raw_by_key if k.startswith("ENERGY_CONS_ANNUAL")), None)
    kwh = units.parse_kwh_per_year(raw_by_key.get(kwh_key, "").replace("kWh/annum", "kWh/a")) if kwh_key else None
    amount = pricing.get("amount") if isinstance(pricing, dict) else None
    price = float(amount) if isinstance(amount, (int, float)) and amount > 0 else None
    hc = by_key.get("HOMECONNECTABLE")
    volts = (raw_by_key.get("VOLTAGE") or "").replace(" V", "").strip() or None
    amps = _first_float(raw_by_key.get("CURRENT", ""))
    path = urlparse(url).path
    record = ProductRecord(
        brand=BRAND, model_number=model, product_name=name_en, category="cooking",
        subcategory=classify(path, headline), product_url=url, finish_color=by_key.get("COL_MAIN"),
        region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None, price_local=price,
        height_in=_mm_to_in(hwd[0]) if hwd else None, width_in=_mm_to_in(hwd[1]) if hwd else None,
        depth_in=_mm_to_in(hwd[2]) if hwd else None,
        weight_lb=round(units.kg_to_lb(kg), 1) if kg is not None else None,
        voltage_v=volts, amps=amps, energy_kwh_year=kwh, wifi_supported=boolean(hc),
        wifi_evidence=(f"Home Connect: {hc}" + (f"; features: {by_key['HOMECONNECT_TYPE']}" if by_key.get("HOMECONNECT_TYPE") else ""))
        if hc else None,
        pod_features=tr.translate_many([h["headline"]["text"] for h in highlights if isinstance(h, dict)
                                        and isinstance(h.get("headline"), dict) and h["headline"].get("text")], "value"),
        extra_specs=table, image_url=main_image_url(SITE, flight))
    raw = [RawSpec(brand=BRAND, model_number=model, source="web", section=r["sec"], key=r["label"], value=r["value"])
           for r in rows]
    return record, raw


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = model_from_url(url)
    with session(SITE, url) as s:
        flight = flight_text(s.html(url))
    record, raw = parse_product(model, url, flight)
    return record, download_docs(SITE, BRAND, model, flight), raw
