"""Product-group tree and brand adapter registry for the brand-selectable search app.

Product groups are a two-level tree: major group (대분류) -> sub group (소분류), with stable English keys
and Korean labels (CATEGORY_TREE).

Each brand adapter module (samsung_us.py, lg_us.py, kitchenaid_us.py, ge_us.py, whirlpool_us.py, bosch_us.py)
must expose:
    discover(subcategory: str, limit: int = 30) -> list[Candidate]
        Walk the brand's US listing for one sub key (pagination / sitemap) and return product candidates.
        Raise ValueError for a sub key the adapter does not support.
    scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]
        Same output contract as ge.scrape()/bosch.scrape(), but for any product URL of that brand.
and may expose:
    SUPPORTED_SUBCATEGORIES: set[str]   (sub keys; absent => the five fridge keys in DEFAULT_SUPPORTED)
"""
import importlib
import importlib.util
import logging
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# SCO = Speed Cook Oven: microwave/convection combination wall ovens, speed ovens, light-wave ovens (e.g. Samsung
# Qooker). Replaces the retired 'scr' key. Defined here only.
SCO_LABEL_KO = "SCO (스피드쿡 오븐)"

CATEGORY_TREE: dict[str, dict] = {
    "refrigerator": {"label_ko": "냉장고", "children": {
        "french_door": "프렌치도어", "side_by_side": "사이드바이사이드", "top_freezer": "상냉동",
        "bottom_freezer": "하냉동", "built_in": "빌트인", "compact": "소형/언더카운터"}},
    "washer": {"label_ko": "세탁기", "children": {
        "top_load": "전자동/탑로더", "front_load": "드럼", "dryer": "건조기", "laundry_center": "트윈/스택·워시타워"}},
    "cooking": {"label_ko": "조리기기", "children": {
        "microwave": "전자레인지", "sco": SCO_LABEL_KO, "otr": "OTR 오버더레인지", "gas_oven": "가스오븐",
        "electric_oven": "전기오븐", "induction": "인덕션", "radiant": "라디언트"}},
}

# What the three original fridge-only adapters support (they predate SUPPORTED_SUBCATEGORIES).
DEFAULT_SUPPORTED = frozenset({"french_door", "side_by_side", "top_freezer", "bottom_freezer", "built_in"})

# Sales regions (market grouping) and countries. 'enabled' is NOT stored here: a region is enabled when at least one
# (brand, country) adapter exists (see region_enabled). Default region = North America (user decision).
REGIONS: dict[str, dict] = {
    "kr": {"label_ko": "한국", "countries": ["kr"], "currency": "KRW"},
    "na": {"label_ko": "북미", "countries": ["us", "ca"], "currency": "USD", "default": True},
    "eu": {"label_ko": "유럽", "countries": ["de", "uk", "fr", "es", "it"], "currency": "EUR"},
    "sa": {"label_ko": "남미", "countries": ["br", "ar", "cl", "co", "mx"], "currency": "BRL"},
    "me": {"label_ko": "중동", "countries": ["ae", "sa"], "currency": "AED"},
    "as": {"label_ko": "아시아", "countries": ["jp", "in", "cn"], "currency": "JPY"},
    "oc": {"label_ko": "오세아니아", "countries": ["au", "nz"], "currency": "AUD"},
}
DEFAULT_REGION = "na"
DEFAULT_COUNTRY = "us"
COUNTRIES: dict[str, dict] = {
    "us": {"region": "na", "currency": "USD", "lang": "en", "voltage": "120V/60Hz", "units": "imperial"},
    "ca": {"region": "na", "currency": "CAD", "lang": "en", "voltage": "120V/60Hz", "units": "imperial"},
    "kr": {"region": "kr", "currency": "KRW", "lang": "ko", "voltage": "220V/60Hz", "units": "metric"},
    "de": {"region": "eu", "currency": "EUR", "lang": "de", "voltage": "230V/50Hz", "units": "metric"},
    "uk": {"region": "eu", "currency": "GBP", "lang": "en", "voltage": "230V/50Hz", "units": "metric"},
    "fr": {"region": "eu", "currency": "EUR", "lang": "fr", "voltage": "230V/50Hz", "units": "metric"},
    "es": {"region": "eu", "currency": "EUR", "lang": "es", "voltage": "230V/50Hz", "units": "metric"},
    "it": {"region": "eu", "currency": "EUR", "lang": "it", "voltage": "230V/50Hz", "units": "metric"},
    "br": {"region": "sa", "currency": "BRL", "lang": "pt", "voltage": "127V|220V/60Hz", "units": "metric"},
    "ar": {"region": "sa", "currency": "ARS", "lang": "es", "voltage": "220V/50Hz", "units": "metric"},
    "cl": {"region": "sa", "currency": "CLP", "lang": "es", "voltage": "220V/50Hz", "units": "metric"},
    "co": {"region": "sa", "currency": "COP", "lang": "es", "voltage": "110V/60Hz", "units": "metric"},
    "mx": {"region": "sa", "currency": "MXN", "lang": "es", "voltage": "127V/60Hz", "units": "metric"},
    "ae": {"region": "me", "currency": "AED", "lang": "ar", "voltage": "230V/50Hz", "units": "metric"},
    "sa": {"region": "me", "currency": "SAR", "lang": "ar", "voltage": "230V/60Hz", "units": "metric"},
    "jp": {"region": "as", "currency": "JPY", "lang": "ja", "voltage": "100V/50-60Hz", "units": "metric"},
    "in": {"region": "as", "currency": "INR", "lang": "en", "voltage": "230V/50Hz", "units": "metric"},
    "cn": {"region": "as", "currency": "CNY", "lang": "zh", "voltage": "220V/50Hz", "units": "metric"},
    "au": {"region": "oc", "currency": "AUD", "lang": "en", "voltage": "230V/50Hz", "units": "metric"},
    "nz": {"region": "oc", "currency": "NZD", "lang": "en", "voltage": "230V/50Hz", "units": "metric"},
}

# Adapter registry. ADAPTERS maps a brand to its US module (country 'us'). Adapters for other countries are modules
# named '<brand lower-case>_<country>' (samsung_kr, lg_kr, samsung_de ...), auto-discovered by importlib when present.
ADAPTERS = {
    "Samsung": "samsung_us",
    "LG": "lg_us",
    "KitchenAid": "kitchenaid_us",
    "GE": "ge_us",
    "Whirlpool": "whirlpool_us",
    "Bosch": "bosch_us",
}

# Free-text aliases accepted by normalize_major (ProductRecord.category historically held "Refrigerator").
_MAJOR_ALIASES = {
    "refrigerator": ("refrigerator", "refrigerators", "fridge"),
    "washer": ("washer", "washers", "washing machine", "laundry", "dryer", "dryers"),
    "cooking": ("cooking", "range", "ranges", "oven", "ovens", "cooktop", "cooktops", "microwave", "microwaves", "stove"),
}
_SUB_TO_MAJOR = {sub: major for major, node in CATEGORY_TREE.items() for sub in node["children"]}


class Candidate(BaseModel):
    brand: str
    model_number: str
    name: str
    url: str
    price_usd: Optional[float] = None  # kept for North America (back-compat); other regions use price_local
    category: str = "refrigerator"  # major key
    subcategory: Optional[str] = None  # sub key
    region: str = DEFAULT_REGION  # REGIONS key
    country: str = DEFAULT_COUNTRY  # COUNTRIES key (lower-case ISO alpha-2; 'uk' for the United Kingdom)
    currency: str = "USD"  # ISO 4217 of price_local
    price_local: Optional[float] = None
    attrs: dict[str, Any] = {}  # listing facts in standard units (cu ft, in, kWh/yr); a missing key = unknown
    attrs_src: dict[str, str] = {}  # attr key -> "listing" | "name" | "detail"


# ------------------------------------------------------------------ tree helpers
def major_keys() -> list[str]:
    return list(CATEGORY_TREE)


def sub_keys(major: Optional[str] = None) -> list[str]:
    """Sub keys in tree order (all, or those of one major group)."""
    return [s for m, node in CATEGORY_TREE.items() if major in (None, m) for s in node["children"]]


def major_of(sub: str) -> Optional[str]:
    return _SUB_TO_MAJOR.get(sub)


def is_major(key: str) -> bool:
    return key in CATEGORY_TREE


def is_sub(key: str) -> bool:
    return key in _SUB_TO_MAJOR


def label_ko(key: str) -> str:
    if key in CATEGORY_TREE:
        return CATEGORY_TREE[key]["label_ko"]
    major = _SUB_TO_MAJOR.get(key)
    return CATEGORY_TREE[major]["children"][key] if major else key


def expand(selector: str) -> list[str]:
    """Major key -> all its sub keys; sub key -> [sub]; anything else -> ValueError."""
    if is_major(selector):
        return sub_keys(selector)
    if is_sub(selector):
        return [selector]
    raise ValueError(f"unknown product group {selector!r}")


def normalize_major(value: Optional[str]) -> Optional[str]:
    """Map a major key, Korean label or English alias ('Refrigerator') to a major key; None if unknown."""
    v = (value or "").strip().casefold()
    if not v:
        return None
    for major, node in CATEGORY_TREE.items():
        if v == major or v == node["label_ko"].casefold() or v in _MAJOR_ALIASES[major]:
            return major
    return None


# ------------------------------------------------------------------ adapters
def is_region(key: object) -> bool:
    return isinstance(key, str) and key in REGIONS


def region_of(country: str) -> Optional[str]:
    c = COUNTRIES.get(country)
    return c["region"] if c else None


def countries_of(region: str) -> list[str]:
    return list(REGIONS[region]["countries"])


def currency_of(country: str) -> str:
    return COUNTRIES.get(country, {}).get("currency", "USD")


def module_name(brand: str, country: str = DEFAULT_COUNTRY) -> Optional[str]:
    """Adapter module name for (brand, country), or None when no such module exists (nothing is imported)."""
    if country == DEFAULT_COUNTRY:
        return ADAPTERS.get(brand)
    if country not in COUNTRIES or brand not in ADAPTERS:
        return None
    name = f"{brand.lower()}_{country}"
    try:
        return name if importlib.util.find_spec(name) is not None else None
    except (ImportError, ValueError):
        return None


def adapter(brand: str, country: str = DEFAULT_COUNTRY):
    name = module_name(brand, country)
    if name is None:
        raise KeyError(f"no adapter for {brand} / {country}")
    return importlib.import_module(name)


def declared_subcategories(module) -> set[str]:
    """Sub keys an adapter module (or object) declares via SUPPORTED_SUBCATEGORIES; unknown keys are dropped."""
    declared = getattr(module, "SUPPORTED_SUBCATEGORIES", None)
    if declared is None:
        declared = DEFAULT_SUPPORTED
    return {s for s in declared if s in _SUB_TO_MAJOR}


def supported(brand: str, country: str = DEFAULT_COUNTRY) -> set[str]:
    """Sub keys the (brand, country) adapter supports; empty when it is unavailable (missing/broken module)."""
    try:
        # US goes through adapter(brand) so single-argument adapter overrides (mock/tests) keep working
        return declared_subcategories(adapter(brand) if country == DEFAULT_COUNTRY else adapter(brand, country))
    except Exception as exc:  # noqa: BLE001 - a broken/missing adapter module just means "not usable"
        logger.info("adapter for %s/%s unavailable: %s", brand, country, type(exc).__name__)
        logger.debug("adapter import detail", exc_info=True)
        return set()


def region_support(region: str) -> dict[str, set[str]]:
    """brand -> sub keys supported in at least one country of the region (brands with none are omitted)."""
    out: dict[str, set[str]] = {}
    for brand in ADAPTERS:
        subs: set[str] = set()
        for country in REGIONS[region]["countries"]:
            subs |= supported(brand, country)
        if subs:
            out[brand] = subs
    return out
