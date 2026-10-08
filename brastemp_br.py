"""Brastemp BR adapter (catalog.py contract): cooking only, brastemp.com.br (VTEX FastStore shop of Whirlpool Corp.).

The shop's public VTEX catalog API gives the listing, prices (current sale price in BRL, never the PIX price) and the
full spec table; the review summary comes from the product page. No hoods, bundles or portable units; the shop sells
no OTR microwaves, no electric (radiant) cooktops. Shared code: _whirlpool_br_common.py.
"""
import _whirlpool_br_common as wb
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Brastemp"
COUNTRY = "br"
REGION = "sa"
CURRENCY = "BRL"
BASE = "https://www.brastemp.com.br"

_E = "eletrodomesticos/"
SUB_SOURCES = {
    "microwave": (_E + "micro-ondas",),
    "sco": (_E + "micro-ondas", _E + "forno"),     # 'Forno Multifuncional com Micro-ondas' sits in the microwave category
    "gas_oven": (_E + "fogao", _E + "forno"),      # fogao = range with oven (floor / built-in); gas wall ovens
    "gas_cooktop": (_E + "cooktop",),
    "electric_oven": (_E + "forno",),
    "induction": (_E + "cooktop",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = wb.Site(brand=BRAND, base=BASE, domain="brastemp.com.br", sub_sources=SUB_SOURCES)


def classify(product: dict):
    """Cooking sub key of a catalog product dict; the one rule discover() and scrape() share."""
    return wb.classify_product(product)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return wb.discover(SITE, subcategory, limit)


def parse_product(product: dict, url: str, signals=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return wb.parse_product(SITE, product, url, signals, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return wb.scrape(SITE, url)
