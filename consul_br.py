"""Consul BR adapter (catalog.py contract): cooking only, consul.com.br (VTEX FastStore shop of Whirlpool Corp.).

Same platform and data model as brastemp_br (shared code: _whirlpool_br_common.py). The shop sells no combination
(speed-cook) ovens, no induction cooktops and no OTR / electric (radiant) models.
"""
import _whirlpool_br_common as wb
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Consul"
COUNTRY = "br"
REGION = "sa"
CURRENCY = "BRL"
BASE = "https://www.consul.com.br"

_E = "eletrodomesticos/"
SUB_SOURCES = {
    "microwave": (_E + "micro-ondas",),
    "gas_oven": (_E + "fogao", _E + "forno"),      # fogao = range with oven; gas wall ovens
    "gas_cooktop": (_E + "cooktop",),
    "electric_oven": (_E + "forno",),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = wb.Site(brand=BRAND, base=BASE, domain="consul.com.br", sub_sources=SUB_SOURCES)


def classify(product: dict):
    """Cooking sub key of a catalog product dict; the one rule discover() and scrape() share."""
    return wb.classify_product(product)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return wb.discover(SITE, subcategory, limit)


def parse_product(product: dict, url: str, signals=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return wb.parse_product(SITE, product, url, signals, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return wb.scrape(SITE, url)
