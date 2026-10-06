"""Frigidaire US adapter (catalog.py contract): cooking only (ranges, cooktops, wall ovens, microwaves).

Data source: SAP Commerce OCC JSON behind frigidaire.com (apolloapi.electrolux.com/occ/v2/frigidaire), fetched from
inside a real browser page. Akamai refuses headless Chromium, so FRIDGE_BROWSER_MODE=auto ends in a visible window.
Everything is implemented in _electrolux_common.py (shared with electrolux_us); refrigerators and laundry are not
implemented on purpose (scope: cooking only).
"""
import _electrolux_common as ec
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Frigidaire"
COUNTRY = "us"
REGION = "na"
CURRENCY = "USD"
BASE = "https://www.frigidaire.com"

# sub key -> OCC category codes. Frigidaire sells no countertop microwaves (category empty) and no gas wall ovens.
SUB_CATEGORIES = {
    "microwave": (ec.US_MICRO + "_BuiltIn",),
    "otr": (ec.US_MICRO + "_OverTheRange",),
    "sco": (ec.US_WALL + "_MicrowaveCombination",),
    "gas_oven": (ec.US_RANGE + "_Gas", ec.US_RANGE + "_DualFuel"),
    "gas_cooktop": (ec.US_TOP + "_Gas",),
    "electric_oven": (ec.US_WALL + "_Single", ec.US_WALL + "_Double"),
    "induction": (ec.US_RANGE + "_Induction", ec.US_TOP + "_Induction"),
    "radiant": (ec.US_RANGE + "_Electric", ec.US_TOP + "_Electric"),
}
SUPPORTED_SUBCATEGORIES = set(SUB_CATEGORIES)
SITE = ec.UsSite(brand=BRAND, site="frigidaire", base=BASE, domain="frigidaire.com", sub_categories=SUB_CATEGORIES)


def classify(category_codes, name: str):
    """Cooking sub key from OCC category codes + product name; the one rule discover() and scrape() share."""
    return ec.classify_us(category_codes, name)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return ec.us_discover(SITE, subcategory, limit)


def parse_product(model: str, url: str, product: dict) -> tuple[ProductRecord, list[RawSpec]]:
    return ec.us_parse_product(SITE, model, url, product)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return ec.us_scrape(SITE, url)
