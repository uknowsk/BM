"""Plain-assert offline tests (saved fixtures). Run: python tests/test_bertazzoni_us.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bertazzoni_us as b

FX = Path(__file__).parent / "fixtures" / "bertazzoni_us"
P = "https://us.bertazzoni.com/products/"
URLS = {
    "PRO366BCFGMCAT": P + "professional-series/ranges/36-inch-all-gas-range-6-brass-burners-and-cast-iron-griddle",
    "PRO48RT6ICAT": P + "professional-series/cooktops/48-6-induction-zones-rangetop",
    "FPRO30FSVTC": P + "professional-series/convection-ovens/30-inch-electric-oven-with-steam-self-clean-and-bertazzoni-assistant",
}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def test_supported_cooking_only():
    import catalog
    assert b.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking")) and "radiant" not in b.SUPPORTED_SUBCATEGORIES
    assert b.COUNTRY == "us" and b.REGION == "na"
    try:
        b.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_classify_rules():
    c = b.classify
    assert c("36 inch All Gas Range, 6 Brass Burners", "ranges") == "gas_oven"
    assert c("48 inch Dual Fuel Range, 6 Brass Burners", "ranges") == "gas_oven"
    assert c("30 inch Induction Range, 4 Heating Zones", "ranges") == "induction"
    assert c("36 Gas Rangetop 6 brass burners", "cooktops") == "gas_cooktop"
    assert c('36" 5 Induction Zones Rangetop', "cooktops") == "induction"
    assert c("30 inch Electric Speed Oven Combination with Steam", "convection-ovens") == "sco"
    assert c("30 Microwave Oven", "speciality-ovens") == "microwave" and c("24 Microwave Drawer", "speciality-ovens") == "microwave"
    assert c("30 Convection Steam Oven", "speciality-ovens") == "electric_oven"
    assert c("30 Warming Drawer", "speciality-ovens") is None and c("36 Hood", "ventilation") is None


def test_listings_and_candidates():
    r = b.parse_listing(_t("listing_ranges.html"))
    assert len(r) == 35 and r[0]["model"] == "PRO486IGFEPCAT" and r[0]["name"].startswith("48 inch Induction Range")
    assert len([c for c in r if b.classify(c["name"], b.category_of(c["path"])) == "gas_oven"]) == 23
    ck = b.parse_listing(_t("listing_cooktops.html"))
    assert ck[0]["name"] == '48" 6 Induction Zones Rangetop'  # the 'New' badge is stripped
    cand = b._candidate(ck[3], "gas_cooktop")
    assert cand.url.startswith("https://us.bertazzoni.com/products/professional-series/cooktops/")
    assert cand.price_usd is None and cand.attrs["fuel"] == "gas" and cand.attrs["width_in"] == 48.0 and cand.attrs["burners"] == 6
    ov = b.parse_listing(_t("listing_convection-ovens.html")) + b.parse_listing(_t("listing_speciality-ovens.html"))
    assert {b.classify(c["name"], b.category_of(c["path"])) for c in ov} == {"electric_oven", "sco", "microwave", None}


def test_discover_filters_by_sub():
    seen = []
    orig = b.fetch_html
    b.fetch_html = lambda url, probe: seen.append(url) or _t("listing_cooktops.html")
    try:
        got = b.discover("induction", limit=3)
    finally:
        b.fetch_html = orig
    assert len(got) == 3 and all(c.subcategory == "induction" for c in got) and seen == [P + "cooktops"]


def test_scrape_parse_fixtures():
    exp = {"PRO366BCFGMCAT": ("gas_oven", 36.0, 5.9, "110"), "PRO48RT6ICAT": ("induction", 48.0, None, "208"),
           "FPRO30FSVTC": ("electric_oven", 30.0, 4.8, "120/240")}
    for model, (sub, w, cap, volt) in exp.items():
        rec, raw = b.parse_product(URLS[model], _t(f"pdp_{model}.html"))
        assert rec.model_number == model and rec.subcategory == sub and rec.width_in == w, model
        assert rec.capacity_total_cuft == cap and rec.voltage_v == volt and rec.price_usd is None, model
        assert all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw) and len(raw) >= 10, model
        assert rec.image_url.startswith("https://us.bertazzoni.com/media/"), model
    rec, _ = b.parse_product(URLS["PRO366BCFGMCAT"], _t("pdp_PRO366BCFGMCAT.html"))
    assert rec.extra_specs["Oven cavity > Oven cooking modes"] == "bake | broil | convection bake"


def test_scrape_returns_no_documents_per_robots():
    orig = b.fetch_html
    b.fetch_html = lambda url, probe: _t("pdp_PRO48RT6ICAT.html")
    try:
        rec, docs, raw = b.scrape(URLS["PRO48RT6ICAT"])
    finally:
        b.fetch_html = orig
    assert docs == [] and rec.model_number == "PRO48RT6ICAT" and raw


def test_url_validation_and_modes():
    assert b.parse_url(URLS["PRO48RT6ICAT"])[0] == "cooktops"
    for bad in ("http://us.bertazzoni.com/products/a-b/ranges/abc", "https://evil.example.com/products/a-b/ranges/abc",
                P + "ranges", P + "a-b/ranges/abc?x=1", P + "a-b/ventilation/abc", P + "a-b/ranges/../x"):
        try:
            b.parse_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        assert b._strategies() == ["requests", "headless", "visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert b._strategies() == ["visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert b._strategies() == ["headless"]
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
