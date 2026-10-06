"""Electrolux DE adapter (catalog.py contract): cooking only (built-in ovens, microwaves, hobs).

electrolux.de has no web shop: category pages embed the product list in __NEXT_DATA__ (15 per page, ?page=N shows
15*N items) with the recommended retail price (UVP, EUR, incl. VAT) as `price`; product pages render the full German
spec table client side. German labels/values are normalised to English through i18n.get('de') (RawSpec keeps the
German source text). Only built-in ovens, microwaves and hobs exist: no gas cooktops, OTR, gas ovens or free-standing
cookers on the German site. Shared code: _electrolux_common.py.
"""
import _electrolux_common as ec
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Electrolux"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
BASE = "https://www.electrolux.de"

_C = "kitchen/cooking/"
SUB_SOURCES = {
    "microwave": (_C + "microwaves/microwave-oven",),
    "sco": (_C + "ovens/oven", _C + "ovens/pyrolytic-oven"),
    "electric_oven": (_C + "ovens/oven", _C + "ovens/pyrolytic-oven", _C + "ovens/pizza-oven"),
    "induction": (_C + "hobs/induction-hob", _C + "hobs/combohob"),
    "radiant": (_C + "hobs/electric-hob", _C + "hobs/combohob"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = ec.EuSite(brand=BRAND, country=COUNTRY, base=BASE, domain="electrolux.de", currency=CURRENCY, lang="de",
                 sub_sources=SUB_SOURCES, api_site=None)


def classify(category_path: str, name: str = "", description: str = ""):
    """Cooking sub key from the category path + name/description; the one rule discover() and scrape() share."""
    return ec.classify_eu(category_path, name, description)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return ec.eu_discover(SITE, subcategory, limit)


def parse_product(model: str, url: str, pdp: dict, price=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return ec.eu_parse_product(SITE, model, url, pdp, price, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return ec.eu_scrape(SITE, url)
