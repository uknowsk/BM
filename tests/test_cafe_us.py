"""Offline tests for the Café US adapter (cafe_us, shared core of haier_us). Run: python tests/test_cafe_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cafe_us as cafe
import catalog

FX = Path(__file__).parent / "fixtures" / "cafe_us"
COOKING = {"microwave", "sco", "otr", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"}


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _rec(sku):
    return cafe.parse_product(cafe.CAFE, f"https://www.cafeappliances.com/appliance/Thing-{sku}", _j(f"po_{sku}.json"))


def test_supported_are_cooking_catalog_keys():
    assert cafe.SUPPORTED_SUBCATEGORIES <= COOKING <= set(catalog.sub_keys())
    assert {"gas_oven", "gas_cooktop", "sco", "otr", "induction", "radiant", "electric_oven", "microwave"} == cafe.SUPPORTED_SUBCATEGORIES
    assert catalog.supported("Café", "us") == cafe.SUPPORTED_SUBCATEGORIES
    for sub, (major, _) in cafe.CAFE_SUB_SOURCES.items():
        assert catalog.major_of(sub) == major == "cooking"
    assert (cafe.COUNTRY, cafe.REGION, cafe.CURRENCY) == ("us", "na", "USD")


def test_parse_search_candidates_and_offdomain():
    data = _j("ss_gas_ranges.json")
    got = cafe.parse_search(cafe.CAFE, data, "gas_oven")
    c = got[0][0]
    assert (c.brand, c.category, c.subcategory, c.country, c.currency) == ("Café", "cooking", "gas_oven", "us", "USD")
    assert c.url.startswith("https://www.cafeappliances.com/appliance/") and c.price_usd and c.model_number == c.model_number.upper()
    bad = dict(data["results"][0], sku="EVIL1", custom_url="https://evil.example.com/appliance/x-EVIL1")
    acc = dict(data["results"][0], sku="ACC1", item_commercial_category2="Range Accessories")
    assert [x.model_number for x, _ in cafe.parse_search(cafe.CAFE, dict(data, results=[bad, acc]), "gas_oven")] == []
    try:
        cafe.parse_search(cafe.CAFE, {"nope": 1}, "gas_oven")
    except cafe.SiteError:
        return
    raise AssertionError("expected SiteError")


def test_discover_filters_limits_and_rejects_unsupported():
    data = _j("ss_gas_ranges.json")
    orig_page, orig_sleep = cafe._search_page, cafe.time.sleep
    cafe._search_page = lambda site, path, n: data
    cafe.time.sleep = lambda s: None
    try:
        got = cafe.discover("gas_oven", limit=50)
        assert got and all(c.subcategory == "gas_oven" for c in got)
        assert len({c.model_number for c in got}) == len(got)
        assert len(cafe.discover("gas_oven", limit=2)) == 2
        for bad in ("french_door", "built_in", "front_load"):
            try:
                cafe.discover(bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad}")
    finally:
        cafe._search_page, cafe.time.sleep = orig_page, orig_sleep


def test_classify_priority_and_root_strip():
    # Advantium wall oven is also listed under Single Wall Ovens and Built In Microwaves -> sco wins; 'Cafe Appliances>' may be missing
    cats = {"Cafe Appliances>Major Appliances>Cooking>Wall Ovens>Single Wall Ovens",
            "Major Appliances>Cooking>Wall Ovens>Advantium Ovens>Advantium Wall Ovens"}
    assert cafe.classify(cafe.CAFE, cats, {"name": "x"}) == "sco"
    assert cafe.classify(cafe.CAFE, {"Cafe Appliances>Major Appliances>Cooking>Wall Ovens>Double Wall Ovens"}, {}) == "electric_oven"
    assert cafe.classify(cafe.CAFE, {"Cafe Appliances>Major Appliances>Cooling>Built-In Refrigerators"}, {}) is None  # fridges out of scope
    # dual-fuel range filed under Double Oven Ranges is gas_oven via the fuel predicate; electric one is radiant
    dbl = {"Cafe Appliances>Major Appliances>Cooking>Ranges>Double Oven Ranges"}
    assert cafe.classify(cafe.CAFE, dbl, {"name": "48\" Dual Fuel Range", "spec_features_fuel_type": "Dual Fuel"}) == "gas_oven"
    assert cafe.classify(cafe.CAFE, dbl, {"name": "30\" Electric Double Oven Range"}) == "radiant"


def test_gas_range_record():
    r, raw, docs = _rec("CGY366P2TS1")
    assert (r.brand, r.category, r.subcategory) == ("Café", "cooking", "gas_oven")
    assert r.price_usd == 7019.0 and (r.country, r.currency) == ("us", "USD")
    assert (r.width_in, r.height_in, r.depth_in) == (35.875, 35.25, 31.125) and r.capacity_total_cuft == 6.2
    assert r.wifi_supported is True and "Café spec table" in r.wifi_evidence
    assert r.image_url.startswith("https://cdn11.bigcommerce.com/")
    assert len(r.extra_specs) > 50 and all(x.brand == "Café" for x in raw) and any(k.count(" > ") == 1 for k in r.extra_specs)
    assert [t for t, _ in docs] == ["Manual", "QuickSpecs", "Installation"]


def test_other_cooking_records():
    assert _rec("CGU366P4TW2")[0].subcategory == "gas_cooktop"
    r = _rec("CSB912P2VS1")[0]
    assert r.subcategory == "sco" and r.price_usd == 2799.0
    r = _rec("UVM9125DYWW")[0]
    assert r.subcategory == "otr" and r.capacity_total_cuft == 1.2


def test_urls_and_hosts():
    assert cafe.model_from_url(cafe.CAFE, "https://www.cafeappliances.com/appliance/CAFE-36-Range-CGY366P2TS1") == "CGY366P2TS1"
    for bad in ("http://www.cafeappliances.com/appliance/X-ABCD1234", "https://evil.example.com/appliance/X-ABCD1234",
                "https://www.cafeappliances.com/cooking/ranges/"):
        try:
            cafe.model_from_url(cafe.CAFE, bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
    try:
        cafe._check_final_url(cafe.CAFE, "https://evil.example.com/x")
    except cafe.SiteError:
        pass
    else:
        raise AssertionError("expected SiteError")


def test_strategies_env():
    import os
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        assert cafe._strategies(cafe.CAFE) == ["headless", "visible"]  # Cloudflare: plain requests are not tried
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert cafe._strategies(cafe.CAFE) == ["visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert cafe._strategies(cafe.CAFE) == ["headless"]
    finally:
        if old is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_signals_listing_and_detail():
    c = cafe.parse_search(cafe.CAFE, _j("ss_gas_ranges.json"), "gas_oven")[0][0]
    assert (c.attrs["rating"], c.attrs["review_count"]) == (4.19, 300) and "release_date" not in c.attrs and "is_new" not in c.attrs
    assert c.attrs_src == {"rating": "listing", "review_count": "listing"} and "finish" in c.attrs  # existing attrs kept
    item = dict(_j("ss_gas_ranges.json")["results"][0], product_first_distribution_date="2021/04/05 00:00:00")
    c = cafe.parse_search(cafe.CAFE, {"results": [item], "pagination": {}}, "gas_oven")[0][0]
    assert (c.attrs["release_date"], c.attrs["release_src"], c.attrs_src["release_date"]) == ("2021-04-05", "distribution", "listing")
    r = _rec("CGY366P2TS1")[0]  # PDP has the distribution date, no Bazaarvoice summary
    assert (r.release_date, r.release_src, r.rating, r.review_count, r.is_new) == ("2021-04-05", "distribution", None, None, None)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
