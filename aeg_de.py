"""AEG DE adapter (catalog.py contract): cooking only (ovens, microwaves, hobs, free-standing cookers).

aeg.de lists through the shop's OCC API (/external/commerce/ccv2/occ/DEU-AEG/products/search/, the same call the
category pages make, with the site's own d2cSellable filter); prices are EUR incl. VAT: the shown selling price
(`offerPrice`, else `price`; the struck-through list price and the UVP are not used). Product pages render the
German spec table client side; labels/values go to English via i18n.get('de') (RawSpec keeps the German source).
AEG DE has no sellable gas hobs (3 listed, none purchasable), no sellable microwave combination (sco) ovens, no OTR and no
gas ovens. Shared code:
_electrolux_common.py.
"""
import _electrolux_common as ec
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "AEG"
COUNTRY = "de"
REGION = "eu"
CURRENCY = "EUR"
BASE = "https://www.aeg.de"

_C = "kitchen/cooking/"
SUB_SOURCES = {
    "microwave": (_C + "microwaves",),
    "electric_oven": (_C + "ovens", _C + "compact-built-in-range/compact-oven"),
    "induction": (_C + "hobs/induction-hob", _C + "hobs/combohob", _C + "cookers/electric-cooker"),
    "radiant": (_C + "hobs/electric-hob", _C + "hobs/combohob", _C + "cookers/electric-cooker"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_SOURCES)
SITE = ec.EuSite(brand=BRAND, country=COUNTRY, base=BASE, domain="aeg.de", currency=CURRENCY, lang="de",
                 sub_sources=SUB_SOURCES, api_site="DEU-AEG")


def classify(category_path: str, name: str = "", description: str = ""):
    """Cooking sub key from the category path + name/description; the one rule discover() and scrape() share."""
    return ec.classify_eu(category_path, name, description)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return ec.eu_discover(SITE, subcategory, limit)


def parse_product(model: str, url: str, pdp: dict, price=None, translator=None) -> tuple[ProductRecord, list[RawSpec]]:
    return ec.eu_parse_product(SITE, model, url, pdp, price, translator)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return ec.eu_scrape(SITE, url)
