"""Collection schema (contract shared by all scrapers). Units: inches, lb, cu ft, kWh/yr."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ProductRecord(BaseModel):
    brand: str
    model_number: str
    product_name: str
    category: str = Field("Refrigerator", description="major product group (catalog key such as 'washer', or a legacy label)")
    subcategory: Optional[str] = Field(None, description="sub product group key from catalog.CATEGORY_TREE, e.g. 'french_door'")
    door_style: Optional[str] = None
    finish_color: Optional[str] = None
    product_url: str
    price_usd: Optional[float] = None
    region: str = Field("na", description="catalog.REGIONS key")
    country: str = Field("us", description="catalog.COUNTRIES key")
    currency: str = Field("USD", description="ISO 4217 code of price_local")
    price_local: Optional[float] = Field(None, description="list price in `currency` (set for non-USD markets)")
    capacity_total_cuft: Optional[float] = None
    capacity_fridge_cuft: Optional[float] = None
    capacity_freezer_cuft: Optional[float] = None
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    depth_in: Optional[float] = None
    weight_lb: Optional[float] = None
    voltage_v: Optional[str] = None
    amps: Optional[float] = None
    frequency_hz: Optional[float] = None
    energy_kwh_year: Optional[float] = None
    energy_star: Optional[bool] = None
    ice_maker: Optional[bool] = None
    water_dispenser: Optional[bool] = None
    wifi_supported: Optional[bool] = None
    wifi_evidence: Optional[str] = Field(None, description="source sentence/keyword proving wifi inference")
    pod_features: list[str] = Field(default_factory=list, description="key selling points, English")
    scraped_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    fridge_temp_range_f: Optional[str] = Field(None, description="fresh-food setpoint range, e.g. '34-44 F'")
    freezer_temp_range_f: Optional[str] = Field(None, description="freezer setpoint range, e.g. '-6 to 8 F'")
    extra_specs: dict[str, str] = Field(
        default_factory=dict,
        description="category-specific specs, English label -> value string (e.g. 'Spin speed (rpm)': '1300')")
    rating: Optional[float] = Field(None, ge=0, le=5, description="average consumer rating on the brand site, 5-point scale")
    review_count: Optional[int] = Field(None, ge=0, description="number of ratings/reviews behind `rating`")
    is_new: Optional[bool] = Field(None, description="True only when the site itself flags the model as new")
    release_date: Optional[str] = Field(None, description="'YYYY-MM-DD' | 'YYYY-MM' | 'YYYY' as stated by the site/doc; never guessed")
    release_src: Optional[str] = Field(None, description="'site' | 'doc' | 'sitemap': where release_date came from")
    image_url: Optional[str] = Field(None, description="main product image URL on the brand site (https)")
    image_path: Optional[str] = Field(None, description="downloaded main image, path relative to the project root")


class DocumentRecord(BaseModel):
    brand: str
    model_number: str
    doc_type: str  # Manual | QuickSpecs | EnergyGuide | Installation | Warranty | SpecSheet | Other
    source_url: str
    local_path: str
    sha256: str
    size_bytes: int
    pages: Optional[int] = None
    downloaded_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class RawSpec(BaseModel):
    brand: str
    model_number: str
    source: str  # "web" or PDF filename
    section: str = ""
    key: str
    value: str


MODE_CATEGORIES = ("Cooling", "Freezing", "Freshness", "Away", "Religious", "Energy",
                   "Ice&Water", "Alert", "Smart", "Other")


class ModeRecord(BaseModel):
    brand: str
    model_number: str
    mode_name: str
    category: str = "Other"  # one of MODE_CATEGORIES
    description: str
    setting_range: Optional[str] = None  # e.g. '34-44 F'
    how_to_activate: Optional[str] = None
    source_doc: str  # PDF filename
    source_page: Optional[int] = None  # 1-based
