"""AEG UK adapter (catalog.py contract): cooking only (ovens, microwaves, hobs incl. gas, free-standing cookers).

aeg.co.uk lists through the shop's OCC API (/external/commerce/ccv2/occ/GBR-AEG/products/search/, the call the
category pages make, with the site's own d2cSellable filter); prices are GBP incl. VAT: the shown selling price
(`offerPrice`, else `price`). The product pages are English (no translation layer needed). No OTR and no gas ovens
are sold. Shared code: _electrolux_common.py.
"""
import _electrolux_common as ec
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "AEG"
COUNTRY = "uk"
REGION = "eu"
CURRENCY = "GBP"
BASE = "https://www.aeg.co.uk"

_C = "kitchen/cooking/"
SUB_SOURCES = {
    "microwave": (_C + "microwaves",),
    "sco": (_C + "ovens",),
    "electric_oven": (_C + "ovens",),
    "gas_cooktop": (_C + "hobs/gas-hob",),
    "induction": (_C + "hobs/induction-hob", _C + "hobs/combohob", _C + "cookers/electric-cooker"),
    "radiant": (_C + "hobs/electric-hob", _C + "hobs/combohob", _C + "cookers/electric-cooker"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = ec.EuSite(brand=BRAND, country=COUNTRY, base=BASE, domain="aeg.co.uk", currency=CURRENCY, lang="en",
                 sub_sources=SUB_SOURCES, api_site="GBR-AEG")


def classify(category_path: str, name: str = "", description: str = ""):
    """Cooking sub key from the category path + name/description; the one rule discover() and scrape() share."""
    return ec.classify_eu(category_path, name, description)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return ec.eu_discover(SITE, subcategory, limit)


def parse_product(model: str, url: str, pdp: dict, price=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return ec.eu_parse_product(SITE, model, url, pdp, price, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return ec.eu_scrape(SITE, url)
