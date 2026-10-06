"""Gaggenau DE adapter (catalog.py contract): cooking only (cooktops, ovens), German site, no prices.

Same platform and approach as gaggenau_us (see its docstring): gaggenau.com/de/de/mkt-product/... pages carry an
unlabeled German bullet list and NO price; discover() reads the robots-listed /de/sitemap.xml. The bullets are kept
verbatim as RawSpec rows (German); extra_specs / pod_features are English via i18n.get('de') (glossary, cache, local
LLM; untranslatable text stays German, never invented). Energy class is stored as 'EU class X'.
"""
import re
import sys

import i18n
import units
from catalog import Candidate
from gaggenau_us import (_first, build_record, candidates_from_urls, model_from_url as _model_from_url, sitemap_urls)
from schema import DocumentRecord, ProductRecord, RawSpec
from thermador_us import Site, download_docs, flight_text, noise, session

BRAND = "Gaggenau"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
SITE = Site(BRAND, "gaggenau_de", "https://www.gaggenau.com/de/de", ("gaggenau.com",),
            ("gaggenau.com", "bsh-group.com"), ("bsh-group.com", "gaggenau.com"), "Optionales ablehnen", "Einverstanden")

# Same groups as gaggenau_us plus radiant (Glaskeramik-Kochfelder, model CE). No gas ovens, OTR or hobs without prefix.
SUPPORTED_SUBCATEGORIES = {"microwave", "sco", "gas_cooktop", "electric_oven", "induction", "radiant"}
_CATEGORY_TOP = {"gas_cooktop": "kochfelder", "induction": "kochfelder", "radiant": "kochfelder"}


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Gaggenau DE")
    top = _CATEGORY_TOP.get(subcategory, "backoefen")
    with session(SITE, f"{SITE.base}/mkt-category/{top}") as s:
        urls = sitemap_urls(s)
    print(f"gaggenau_de: discover({subcategory}): {len(urls)} product urls in sitemap", file=sys.stderr)
    return candidates_from_urls(urls, SITE, subcategory, "cooking", limit, REGION, COUNTRY, CURRENCY)


def model_from_url(url: str) -> str:
    return _model_from_url(url, SITE, "de/de")


def _cm_to_in(cm: float) -> float:
    return round(units.mm_to_in(cm * 10), 1)


def facts_de(bullets: list[str], headline: str) -> tuple[dict, dict]:
    """(ProductRecord field updates, extra_specs with English labels) from the German bullet list and headline."""
    text = "\n".join(bullets)
    upd: dict = {}
    extra: dict = {}
    two = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*cm", headline)
    one = re.search(r"(\d+(?:[.,]\d+)?)\s*cm", headline)
    w = max(two.groups(), key=lambda x: float(x.replace(",", "."))) if two else one.group(1) if one else None
    if w:
        upd["width_in"] = _cm_to_in(float(w.replace(",", ".")))
    kg = _first(r"gewicht[^\n]*?([\d.,]+)\s*kg", text)
    if kg:
        upd["weight_lb"] = round(units.kg_to_lb(float(kg.replace(",", "."))), 1)
    volts = _first(r"(\d{3}(?:\s*-\s*\d{3})?)\s*V\b", text)
    if volts:
        upd["voltage_v"] = re.sub(r"\s", "", volts)
    hz = _first(r"(\d{2})\s*Hz", text)
    if hz:
        upd["frequency_hz"] = float(hz)
    kwh = units.parse_kwh_per_year(text)
    if kwh is not None:
        upd["energy_kwh_year"] = kwh
        extra["Technical data > Energy consumption (kWh/year)"] = f"{kwh:g}"
    cls = units.eu_energy_class(_first(r"energieeffizienzklasse\s*[A-G]\b", text) or "")
    if cls:
        extra["Technical data > Energy efficiency class"] = cls
    cap = _first(r"nutz(?:volumen|inhalt)[^\n\d]*([\d.,]+)\s*Liter", text)
    if cap:
        extra["Technical data > Cavity capacity (L)"] = cap
    load = _first(r"(?:gesamtanschlusswert|anschlusswert)[^\n\d]*([\d.,]+\s*k?W)", text)
    if load:
        extra["Technical data > Connected load"] = noise(load)
    hc = re.search(r"home connect|fernsteuerung", text, re.I)
    if hc:
        upd["wifi_supported"] = True
        upd["wifi_evidence"] = hc.group(0)
    return upd, extra


def _translate(texts: list[str]) -> list[str]:
    return i18n.get("de").translate_many(texts, "value")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    model = model_from_url(url)
    with session(SITE, url) as s:
        flight = flight_text(s.html(url))
    record, raw = build_record(SITE, model, url, flight, facts_de, dict(region=REGION, country=COUNTRY, currency=CURRENCY),
                               _translate)
    return record, download_docs(SITE, BRAND, model, flight), raw
