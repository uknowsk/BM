"""Electrolux BR adapter (catalog.py contract): cooking only, loja.electrolux.com.br (VTEX IO shop; electrolux.com.br
redirects there).

The shop's public VTEX catalog API gives the listing, prices (current sale price in BRL, never the PIX price) and the
full spec table. The review summary (aggregateRating) exists only in the rendered DOM, so scrape() reads it from a real
browser page (headless first, visible window as the fallback). No OTR microwaves, no electric (radiant) cooktops,
no hybrid gas+induction hobs, no 'Torre de coccao' bundles, no outlet stock. Shared code: _whirlpool_br_common.py.
"""
import _whirlpool_br_common as wb
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Electrolux"
COUNTRY = "br"
REGION = "sa"
CURRENCY = "BRL"
BASE = "https://loja.electrolux.com.br"

_E = "eletrodomesticos/"
SUB_SOURCES = {
    "microwave": (_E + "micro-ondas",),
    "sco": (_E + "fornos", _E + "micro-ondas"),    # 'Forno de Embutir Multifuncional 2 em 1 com Micro-ondas'
    "gas_oven": (_E + "fogao", _E + "fornos"),     # fogao = range with oven (floor / built-in); gas wall ovens
    "gas_cooktop": (_E + "cooktops",),
    "electric_oven": (_E + "fornos",),
    "induction": (_E + "cooktops",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = wb.Site(brand=BRAND, base=BASE, domain="electrolux.com.br", sub_sources=SUB_SOURCES, render_rating=True)


def classify(product: dict):
    """Cooking sub key of a catalog product dict; the one rule discover() and scrape() share."""
    return wb.classify_product(product)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return wb.discover(SITE, subcategory, limit)


def parse_product(product: dict, url: str, signals=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return wb.parse_product(SITE, product, url, signals, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return wb.scrape(SITE, url)
