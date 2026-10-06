"""Amana US adapter (catalog.py contract): entry-level refrigeration, laundry and cooking.

Same Whirlpool-Corp OCC platform as maytag_us.py / whirlpool_us.py (site code `amana-us`): listing = OCC search over the
whole (45 product) catalog, specs = `classifications` of the FULL OCC product, documents = AEM document search, all
fetched from inside a real browser page (new headless `channel="chromium"` passes Akamai, visible fallback). The engine
lives in maytag_us.py; this module only declares the site and which sub keys amana.com really sells.
amana.com also lists Whirlpool-branded and Unbranded hoods / microwaves; only products whose OCC `brand` is Amana are
candidates. Not sold (verified on the live catalog): French door, built-in refrigerators, wall ovens, cooktops,
induction, countertop microwaves (only OTR), laundry centers. Amana's top-freezer line includes 'ARTX...' models;
bottom-freezer and side-by-side refrigerators exist.
"""
import maytag_us as _engine
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Amana"
COUNTRY, REGION, CURRENCY = "us", "na", "USD"

SITE = _engine.Site(
    brand=BRAND, base="https://www.amana.com", host="amana.com", occ="/ws/v2/amana-us",
    # SCOPE (user instruction): cooking only for now; Amana's refrigerators (top/bottom freezer, side-by-side) and
    # laundry (top/front load, dryer) are sold but intentionally not exposed.
    codes={sub: (None,) for sub in ("otr", "gas_oven", "radiant")})
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
