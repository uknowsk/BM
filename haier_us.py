"""Haier Appliances US adapter (https://www.haierappliances.com; brand 'Haier').

Same GE Appliances BigCommerce/Searchspring platform as cafe_us.py / ge_us.py; all logic lives in cafe_us (`Site`
describes the storefront, Searchspring site id hpd3yx). The catalog is small (about 19 available kitchen products),
so a sub key is only listed when the site actually carries products for it (cooking scope only, per coordinator):
  supported: otr, gas_oven (Gas Ranges), radiant (Electric Ranges, 24" small-space range)
  not supported: microwave (only OTR models exist), induction/gas_cooktop/electric_oven/sco (no products);
  refrigerators (the site has French/Quad/Top/Bottom freezer models) and laundry are intentionally not implemented.
Cloudflare challenge on plain requests -> storefront pages are loaded in a browser (headless first, then visible).
"""
import cafe_us as core
import ge_us as ge
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

COUNTRY = core.COUNTRY
REGION = core.REGION
CURRENCY = core.CURRENCY

_R = "Haier>Kitchen>"
_RNG = _R + "Cooking>Ranges>"
_MW = _R + "Cooking>Microwaves>"

HAIER_SUB_SOURCES: dict[str, tuple[str, list]] = {
    "otr": ("cooking", [(_MW + "Over-the-Range Microwave Ovens", None)]),
    "gas_oven": ("cooking", [(_RNG + "Gas Ranges", None)]),
    "radiant": ("cooking", [(_RNG + "Electric Ranges", ge._not_induction)]),
}

HAIER = core.Site(brand="Haier", base="https://www.haierappliances.com", hosts=("haierappliances.com",),
                  ss_site="hpd3yx", sub_sources=HAIER_SUB_SOURCES, root="Haier")
BRAND = HAIER.brand
SUPPORTED_SUBCATEGORIES = set(HAIER_SUB_SOURCES)


def classify(cats: set[str], item: dict) -> str | None:
    return core.classify(HAIER, cats, item)


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    return core.discover_site(HAIER, subcategory, limit)


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return core.scrape_site(HAIER, url)
