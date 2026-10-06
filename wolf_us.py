"""Wolf US adapter (catalog.py contract): cooking (ranges, rangetops/cooktops, wall ovens, speed ovens, microwaves).

Wolf sells on https://www.subzero-wolf.com together with Sub-Zero; the data comes from the site's Commerce GraphQL
catalogue (see _subzerowolf_common.py). Wolf has no OTR microwaves and no radiant (electric resistance) cooktops, so
`otr` and `radiant` are not supported. Sub-Zero is refrigeration only (no cooking) -> no subzero_us module in this pass.
"""
import _subzerowolf_common as sz
from catalog import Candidate
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Wolf"
COUNTRY, REGION, CURRENCY = sz.COUNTRY, sz.REGION, sz.CURRENCY

# Wolf `mnseries` value -> (sub key, fuel). Everything else (hoods, coffee systems, burner/side-burner/cooktop modules,
# drawers, vacuum drawers, ...) is not a product group of this app.
SERIES: dict[str, tuple[str, str | None]] = {
    "Dual Fuel Range": ("gas_oven", "dual_fuel"),
    "Gas Range": ("gas_oven", "gas"),
    "Induction Range": ("induction", "induction"),
    "Sealed Burner Rangetop": ("gas_cooktop", "gas"),
    "Gas Cooktop": ("gas_cooktop", "gas"),
    "Induction Cooktop": ("induction", "induction"),
    "Single Oven": ("electric_oven", "electric"),
    "Double Oven": ("electric_oven", "electric"),
    "Convection Steam Oven": ("electric_oven", "electric"),
    "Speed Oven": ("sco", "electric"),
    "Drawer Microwave Oven": ("microwave", None),
    "Drop-Down Door Microwave Oven": ("microwave", None),
    "Convection Microwave Oven": ("microwave", None),
    "Standard Microwave Oven": ("microwave", None),
}
SUPPORTED_SUBCATEGORIES = {sub for sub, _ in SERIES.values()}


def classify(series: str, name: str = "") -> str | None:
    """Sub key from the product's `mnseries` (the catalogue's product type, with the finish/series prefix such as
    'E Series Transitional Speed Oven' / 'M Series Professional Speed Oven' folded to the type); None for any other type.
    One rule for discover() and scrape()."""
    s = (series or "").strip()
    if s in SERIES:
        return SERIES[s][0]
    for key, (sub, _fuel) in SERIES.items():
        if s.endswith(key):
            return sub
    return None


def _series_for(sub: str) -> list[str]:
    return [k for k, (s, _f) in SERIES.items() if s == sub]


def _fuel_of(series: str) -> str | None:
    for key, (_sub, fuel) in SERIES.items():
        if series == key or series.endswith(key):
            return fuel
    return None


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"unsupported subcategory {subcategory!r} for Wolf US")
    return sz.search_products("Wolf", _series_for(subcategory), classify, _fuel_of, limit, subcategory,
                              f"wolf_us: discover({subcategory})")


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    return sz.scrape_product(url, "Wolf", BRAND, classify)
