"""JennAir US adapter (catalog.py contract): premium built-in refrigeration and cooking.

Same Whirlpool-Corp OCC platform as maytag_us.py / whirlpool_us.py / kitchenaid_us.py (site code `jennAir-us`, note the
capital A): listing = OCC category search, specs = `classifications` of the FULL OCC product, documents = AEM document
search, all fetched from inside a real browser page (new headless `channel="chromium"` passes Akamai, visible fallback).
The engine (browser, classify, parse, download) lives in maytag_us.py; this module only declares the site and which
sub keys jennair.com really sells. The OCC listing also holds ~400 parts/accessories, so discover queries one category
tree per sub key (Refrigeration, Microwaves, Wall Ovens, Ranges, Rangetops, Cooktops, Custom Cooktops) and classify()
sorts the products. Wine/beverage coolers, ice makers, column freezers, hoods, dishwashers, warming drawers and
compactors are not benchmark categories and are skipped.
Not sold (verified on the live catalog): washers/dryers (no laundry) and free-standing top-freezer / bottom-freezer /
side-by-side refrigerators (JennAir's side-by-side and bottom-freezer models are all built-in -> built_in).
"""
import maytag_us as _engine
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "JennAir"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"

SITE = _engine.Site(
    brand=BRAND, base="https://www.jennair.com", host="jennair.com", occ="/ws/v2/jennAir-us",
    codes={
        # SCOPE (user instruction): cooking only for now; built_in / french_door / compact refrigerators are sold
        # (RefrigerationBuilt-inRefrigerators, RefrigerationColumns, RefrigerationFrenchDoorRefrigerators,
        # RefrigerationFreestandingRefrigerators, RefrigerationUndercounterRefrigerators) but intentionally not exposed.
        "microwave": ("Microwaves",),
        "otr": ("Microwaves",),
        "sco": ("WallOvens", "Microwaves"),
        "electric_oven": ("WallOvens",),
        "gas_oven": ("Ranges",),
        "gas_cooktop": ("Cooktops", "Rangetops", "CustomCooktops"),
        "induction": ("Cooktops", "Ranges", "CustomCooktops"),
        "radiant": ("Cooktops", "Ranges", "CustomCooktops"),
    })
BASE = SITE.base
SUPPORTED_SUBCATEGORIES = SITE.supported

classify = _engine.classify
reset_browser_mode = _engine.reset_browser_mode
_modes = _engine._modes


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return _engine.site_discover(SITE, subcategory, limit)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return _engine.site_scrape(SITE, url)


def parse_search(data: dict, sub_key: str) -> list[Candidate]:
    return _engine.site_parse_search(SITE, data, sub_key)


def parse_product(model: str, url: str, prod: dict) -> tuple[ProductRecord, list[RawSpec]]:
    return _engine.site_parse_product(SITE, model, url, prod)


def parse_docs(data: dict) -> list[tuple[str, str]]:
    return _engine.site_parse_docs(SITE, data)


def model_from_url(url: str) -> str:
    return _engine.site_model_from_url(SITE, url)


def main_image_url(prod: dict) -> str | None:
    return _engine.site_main_image_url(SITE, prod)
