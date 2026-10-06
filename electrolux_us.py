"""Electrolux US adapter (catalog.py contract): cooking only (ranges, rangetops/cooktops, wall ovens, microwaves).

Same SAP Commerce OCC platform as Frigidaire (apolloapi.electrolux.com/occ/v2/electrolux behind electrolux.com);
see _electrolux_common.py. Electrolux US sells no electric (radiant) ranges or cooktops (both categories are empty),
so 'radiant' is not supported. Refrigerators and laundry are not implemented on purpose (scope: cooking only).
"""
import _electrolux_common as ec
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Electrolux"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
BASE = "https://www.electrolux.com"

SUB_CATEGORIES = {
    "microwave": (ec.US_MICRO + "_BuiltIn",),
    "otr": (ec.US_MICRO + "_OverTheRange",),
    "sco": (ec.US_WALL + "_MicrowaveCombination",),
    "gas_oven": (ec.US_RANGE + "_Gas", ec.US_RANGE + "_DualFuel"),
    "gas_cooktop": (ec.US_TOP + "_Gas",),
    "electric_oven": (ec.US_WALL + "_Single", ec.US_WALL + "_Double"),
    "induction": (ec.US_RANGE + "_Induction", ec.US_TOP + "_Induction"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_CATEGORIES)
SITE = ec.UsSite(brand=BRAND, site="electrolux", base=BASE, domain="electrolux.com", sub_categories=SUB_CATEGORIES)


def classify(category_codes, name: str):
    """Cooking sub key from OCC category codes + product name; the one rule discover() and scrape() share."""
    return ec.classify_us(category_codes, name)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return ec.us_discover(SITE, subcategory, limit)


def parse_product(model: str, url: str, product: dict) -> tuple[ProductRecord, list[RawSpec]]:
    return ec.us_parse_product(SITE, model, url, product)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return ec.us_scrape(SITE, url)
