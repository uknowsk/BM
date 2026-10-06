"""Plain-assert tests (pytest-compatible). Run: python tests/test_brands.py
Brand registry of the 6 -> 30 expansion: metadata, module-name rule, 'ready only when the module file exists'."""
import importlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import service

NEW_24 = ["Maytag", "JennAir", "Amana", "Thermador", "Gaggenau", "Siemens", "Frigidaire", "Electrolux", "AEG", "Café",
          "Monogram", "Haier", "Fisher & Paykel", "Viking", "Sub-Zero", "Wolf", "Miele", "Smeg", "Liebherr",
          "Bertazzoni", "De Dietrich", "Beko", "Hisense", "Panasonic"]


def test_registry_has_30_brands_in_known_groups():
    assert len(catalog.BRAND_META) == 30 and list(catalog.BRAND_META)[:6] == [
        "Samsung", "LG", "KitchenAid", "GE", "Whirlpool", "Bosch"]
    assert list(catalog.BRAND_META)[6:] == NEW_24
    for name, meta in catalog.BRAND_META.items():
        assert meta["group"] in catalog.GROUPS, name
        assert meta["countries"] and all(c in catalog.ADAPTER_COUNTRIES for c in meta["countries"]), name
    assert catalog.brand_group("Maytag") == "Whirlpool Corp." and catalog.brand_group("Siemens") == "BSH"
    assert catalog.brand_group("nope") is None and catalog.brand_countries("De Dietrich") == ["fr"]
    assert catalog.ADAPTER_COUNTRIES == ("us", "kr", "de", "uk", "fr")


def test_brand_slug_rule():
    cases = {"Café": "cafe", "Fisher & Paykel": "fisherpaykel", "Sub-Zero": "subzero", "De Dietrich": "dedietrich",
             "JennAir": "jennair", "KitchenAid": "kitchenaid", "GE": "ge", "AEG": "aeg"}
    for brand, slug in cases.items():
        assert catalog.brand_slug(brand) == slug, brand
    slugs = [catalog.brand_slug(b) for b in catalog.BRAND_META]
    assert len(set(slugs)) == 30 and all(s.isascii() and s.isalnum() and s == s.lower() for s in slugs)


def test_yaml_lists_all_brands_with_rule_based_modules():
    cfg = service.load_config()
    names = [b["name"] for b in cfg["brands"]]
    assert names == list(catalog.BRAND_META)
    for b in cfg["brands"]:
        expect = catalog.ADAPTERS.get(b["name"]) if b["name"] in ("Samsung", "LG", "KitchenAid", "GE", "Whirlpool", "Bosch") \
            else f"{catalog.brand_slug(b['name'])}_us"
        assert b["module"] == expect, b


def test_missing_new_brand_module_is_inactive_not_an_error():
    for brand in NEW_24:
        for cc in catalog.ADAPTER_COUNTRIES:
            name = catalog.module_name(brand, cc)
            if name is None:  # no adapter file (yet): inactive
                assert catalog.supported(brand, cc) == set()
    assert catalog.module_name("Maytag", "zz") is None and catalog.module_name("NoSuchBrand") is None
    try:
        catalog.adapter("NoSuchBrand")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_new_brand_module_is_found_when_the_file_exists():
    # Hermetic: made-up brands, so real adapters added later by the brand agents never affect this test.
    fakes = {"Zéta & Co": ("us", "zetaco_us"), "Yo-Yo Zed": ("uk", "yoyozed_uk"), "Xi Xi": ("fr", "xixi_fr")}
    for brand in fakes:
        catalog.BRAND_META[brand] = {"group": "글로벌", "countries": [fakes[brand][0]]}
    with tempfile.TemporaryDirectory() as d:
        for brand, (cc, mod) in fakes.items():
            Path(d, f"{mod}.py").write_text(
                "SUPPORTED_SUBCATEGORIES = {'french_door', 'gas_cooktop'}" + chr(10) + f"COUNTRY = {cc!r}" + chr(10),
                encoding="utf-8")
        sys.path.insert(0, d)
        try:
            importlib.invalidate_caches()
            assert catalog.module_name("Zéta & Co") == "zetaco_us" and catalog.module_name("Zéta & Co", "us") == "zetaco_us"
            assert catalog.module_name("Yo-Yo Zed", "uk") == "yoyozed_uk" and catalog.module_name("Yo-Yo Zed") is None
            assert catalog.supported("Zéta & Co") == {"french_door", "gas_cooktop"}
            assert catalog.countries_with_adapter("Yo-Yo Zed") == ["uk"]
            assert "Zéta & Co" in catalog.region_support("na")
            assert {"Yo-Yo Zed", "Xi Xi"} <= set(catalog.region_support("eu"))
        finally:
            sys.path.remove(d)
            for brand, (cc, mod) in fakes.items():
                sys.modules.pop(mod, None)
                catalog.BRAND_META.pop(brand)
            importlib.invalidate_caches()
    assert catalog.module_name("Zéta & Co") is None


def test_existing_six_keep_explicit_us_modules():
    assert catalog.module_name("Samsung") == "samsung_us" and catalog.module_name("Bosch") == "bosch_us"
    assert catalog.countries_with_adapter("Samsung")[0] == "us"
    assert set(catalog.region_support("na")) >= {"Samsung", "LG", "KitchenAid", "GE", "Whirlpool", "Bosch"}


def test_slow_adapters_are_announced_with_their_delay():
    # Viking's robots.txt Crawl-delay (10 s) is declared by the adapter and surfaces for the UI warning;
    # brands without a long delay report None (no warning).
    assert catalog.request_delay("Viking") == 10.0
    assert catalog.request_delay("Samsung") is None and catalog.request_delay("De Dietrich") is None
    import server
    by_name = {b["name"]: b for b in server.api_brands(region="na")}
    assert by_name["Viking"]["delay_s"] == 10.0 and "10" in by_name["Viking"]["note"]
    assert by_name["Samsung"]["delay_s"] is None and by_name["Samsung"]["note"] == ""
    assert by_name["De Dietrich"]["note"] == "준비 중" and by_name["De Dietrich"]["delay_s"] is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
