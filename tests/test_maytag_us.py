"""Plain-assert offline tests (saved fixtures). Run: python tests/test_maytag_us.py
Covers the shared Whirlpool-Corp OCC engine in maytag_us.py (jennair_us / amana_us have their own test files)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import maytag_us as mt

FX = Path(__file__).parent / "fixtures" / "maytag_us"
COOKING = {"microwave", "sco", "otr", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"}


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _rec(m):
    prod = _j(f"prod_{m}.json")
    return mt.parse_product(m, mt.BASE + prod["url"], prod)


def _subs():
    data = _j("search_all.json")
    return {s: [c.model_number for c in mt.parse_search(data, s)] for s in mt.SUPPORTED_SUBCATEGORIES}


def test_contract_constants_and_scope():
    assert (mt.COUNTRY, mt.REGION, mt.CURRENCY) == ("us", "na", "USD")
    assert mt.SUPPORTED_SUBCATEGORIES <= COOKING and "microwave" not in mt.SUPPORTED_SUBCATEGORIES  # only OTR sold
    assert {"otr", "sco", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"} == mt.SUPPORTED_SUBCATEGORIES
    assert all(s in catalog.CATEGORY_TREE["cooking"]["children"] for s in mt.SUPPORTED_SUBCATEGORIES)
    assert catalog.brand_slug(mt.BRAND) + "_us" == "maytag_us"


def test_discover_unsupported_raises():
    for bad in ("microwave", "french_door", "top_load", "dryer", "nope"):
        try:
            mt.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_search_by_sub_and_exclusions():
    got = _subs()
    assert got["otr"] == ["MMMS4230PZ", "MMV4207JW", "MMMF8030PZ", "MMMF6030PZ", "MMV5227JZ"]
    assert got["sco"] == ["MOEC4030TZ", "MOEC6030LZ"]
    assert "MOED6030LZ" in got["electric_oven"] and "MOEC6030LZ" not in got["electric_oven"]
    assert "MGS8800PZ" in got["gas_oven"] and "MET8800FZ" not in got["gas_oven"] and "MGT8800FZ" in got["gas_oven"]
    assert got["gas_cooktop"] == ["MGC9536DS", "MGC7430DS", "MGC7536DS"]
    assert got["induction"] == ["MCIT8030SB", "MCIT8036SB"]
    assert {"MFES8030RZ", "MEC8830HS", "MER4800PZ"} <= set(got["radiant"])  # electric range, electric cooktop, Kitchen-Suite range
    everything = [c for v in got.values() for c in v]
    assert len(everything) == len(set(everything))  # one sub key per model
    for hood in ("WVI75UC6DS", "UXD8636DYS", "UXT3030ADB", "UVL5430JSS"):  # Whirlpool / Unbranded hoods
        assert hood not in everything
    assert not {"MRMF5436TZ", "MTW7405RR", "MDB4949SKZ", "MZCC4316TG"} & set(everything)  # fridge, washer, dishwasher, freezer


def test_candidate_fields():
    c = {x.model_number: x for x in mt.parse_search(_j("search_all.json"), "otr")}["MMMS4230PZ"]
    assert (c.brand, c.category, c.subcategory, c.price_usd) == ("Maytag", "cooking", "otr", 289.0)
    assert c.url.startswith("https://www.maytag.com/kitchen/") and "®" not in c.name


def test_parse_search_drops_offdomain_brand_and_member_price():
    prods = [{"code": "X1", "name": "30-inch Gas Range", "url": "https://evil.example/p.a.x1.html"},
             {"code": "X2", "name": "30-inch Gas Range", "brand": "Whirlpool", "url": "/p.a.x2000.html"},
             {"code": "X3", "name": "30-inch Gas Range", "brand": "Maytag", "url": "/p.a.x3000.html",
              "price": {"value": 899.0}, "isMemberPrice": True},
             {"code": "X4", "name": "30-inch Gas Range", "brand": "Maytag", "url": "/p.a.x4000.html",
              "price": {"value": 899.0}}]
    got = {c.model_number: c for c in mt.parse_search({"products": prods}, "gas_oven")}
    assert set(got) == {"X3", "X4"} and got["X3"].price_usd is None and got["X4"].price_usd == 899.0


def test_classify_names():
    f = lambda name, cat="": mt.classify({"name": name, "primaryCategory": {"pipedCategory": cat}, "url": ""})
    assert f("30-Inch Wide Double Oven Gas Range With True Convection") == ("cooking", "gas_oven")
    assert f("30-inch Dual-Fuel Range") == ("cooking", "gas_oven")
    assert f("36\" Gas Rangetop") == ("cooking", "gas_cooktop")
    assert f("36-Inch Electric Cooktop with Reversible Grill and Griddle") == ("cooking", "radiant")
    assert f("30-Inch Induction Slide-In Range") == ("cooking", "induction")
    assert f("27-inch Single Wall Oven with Air Fry") == ("cooking", "electric_oven")
    assert f("30-Inch Wall Oven Microwave Combo") == ("cooking", "sco")
    assert f("24\" Built-In Speed Oven") == ("cooking", "sco")
    assert f("Over-the-Range Microwave Hood Combination") == ("cooking", "otr")
    assert f("Countertop Microwave Oven with Convection") == ("cooking", "microwave")
    assert f("30\" Range Hood")[0] == "other" and f("Custom Hood Liner")[0] == "other"
    assert f("24\" Dishwasher")[0] == "other" and f("Frost Free Upright Freezer")[0] == "other"
    assert f("Refurbished 30-Inch Gas Range")[0] == "other" and f("Custom 15\" Electric Griddle")[0] == "other"
    assert f("Gas Range", "Parts-&-Accessories|Range-Parts") == ("other", None)
    # non-cooking keys are still classified (not exposed): fridge subs, laundry
    assert f("36-Inch French Door Bottom Mount Refrigerator") == ("refrigerator", "french_door")
    assert f("Built-In Side-by-Side Refrigerator") == ("refrigerator", "built_in")
    assert f("33-Inch Top-Freezer Refrigerator With Dual Crisper Drawers") == ("refrigerator", "top_freezer")
    assert f("Front Load Gas Dryer") == ("washer", "dryer") and f("Smart Front Load Washer") == ("washer", "front_load")


def test_gas_range_record():
    r, raw = _rec("MGS8800PZ")
    assert (r.brand, r.category, r.subcategory, r.price_usd) == ("Maytag", "cooking", "gas_oven", 849.0)
    assert r.capacity_total_cuft == 5.8 and (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (29.875, 36.0, 28.875, 194.0)
    assert (r.voltage_v, r.amps) == ("120", 15.0) and r.door_style is None and r.ice_maker is None
    assert r.wifi_supported is False and "Smart Compatibility" in r.wifi_evidence
    assert r.finish_color == "Fingerprint Resistant Stainless Steel" and "5 Burners" in r.pod_features
    assert r.extra_specs["Cooktop Details > Auto Re-ignition"] == "No"
    assert any(k.startswith("Element/Burner Details > ") and k.endswith("Element-Burner Power") for k in r.extra_specs)
    assert len(r.extra_specs) == len(raw) == 82 and all(" > " in k for k in r.extra_specs)
    assert all(x.source == "web" and x.brand == "Maytag" for x in raw)
    assert r.image_url == ("https://www.maytag.com/is/image/content/dam/global/maytag/cooking/range/images/"
                           "hero-MGS8800PZ.tif?fmt=jpeg&wid=1200")


def test_other_records():
    r, _ = _rec("MOEC6030LZ")
    assert (r.subcategory, r.capacity_total_cuft, r.voltage_v) == ("sco", 6.4, "240")
    r, _ = _rec("MOES6030LZ")
    assert (r.subcategory, r.capacity_total_cuft) == ("electric_oven", 5.0)
    r, _ = _rec("MCIT8030SB")
    assert (r.subcategory, r.capacity_total_cuft, r.energy_star, r.wifi_supported) == ("induction", None, True, True)
    r, _ = _rec("MGC7430DS")
    assert (r.subcategory, r.width_in, r.price_usd) == ("gas_cooktop", 30.0, 799.0)
    r, _ = _rec("MEC8830HS")
    assert r.subcategory == "radiant" and r.capacity_total_cuft is None
    r, _ = _rec("MFES8030RZ")
    assert (r.subcategory, r.capacity_total_cuft) == ("radiant", 5.3)
    r, _ = _rec("MMMS4230PZ")
    assert (r.subcategory, r.capacity_total_cuft, r.price_usd) == ("otr", 1.7, 289.0)


def test_scrape_and_discover_agree_on_sub_key():
    listing = {c.model_number: c.subcategory for s in mt.SUPPORTED_SUBCATEGORIES
               for c in mt.parse_search(_j("search_all.json"), s)}
    for m in ("MMMS4230PZ", "MOEC6030LZ", "MOES6030LZ", "MGS8800PZ", "MGC7430DS", "MCIT8030SB", "MFES8030RZ", "MEC8830HS"):
        assert _rec(m)[0].subcategory == listing[m], m


def test_unrecognised_structure_and_unsupported_category_raise():
    for prod in ({"name": "n"}, {"name": "", "classifications": [{"name": "S", "features": []}]}):
        try:
            mt.parse_product("X", "u", prod)
        except ValueError:
            continue
        raise AssertionError(prod)
    hood = {"name": "30\" Range Hood", "classifications": [{"name": "S", "features": [
        {"name": "Width", "featureValues": [{"value": "30"}], "featureUnits": [{"symbol": "in"}]}]}]}
    try:
        mt.parse_product("UXT3030ADB", "u", hood)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_spec_rows_units_and_multivalues():
    prod = {"classifications": [{"name": "Dimensions", "features": [
        {"name": "Width", "featureValues": [{"value": "29.875"}], "featureUnits": [{"symbol": "in"}]},
        {"name": "Racks", "featureValues": [{"value": "1 Flat"}, {"value": "2 Glide"}]},
        {"name": "Empty", "featureValues": []}]}]}
    assert mt.spec_rows(prod) == [("Dimensions", "Width", "29.875 in"), ("Dimensions", "Racks", "1 Flat, 2 Glide")]
    assert mt.full_spec_table([("S®", "L™", "V®")]) == {"S > L": "V"}


def test_docs_and_energy():
    docs = mt.parse_docs(_j("docs_MOEC6030LZ.json"))
    assert [t for t, _ in docs] == ["Manual", "SpecSheet", "QuickSpecs"]
    assert all(u.startswith("https://www.maytag.com/") and u.endswith(".pdf") for _, u in docs)
    bad = {"documents": [{"doc_type": ["owners-manual"], "language": ["en_us"], "asseturl": "https://evil.example/a.pdf"},
                         {"doc_type": ["owners-manual"], "language": ["fr_ca"], "asseturl": "/a.pdf"},
                         {"doc_type": ["cad"], "language": ["en_us"], "asseturl": "/a.pdf"}]}
    assert mt.parse_docs(bad) == []
    assert mt.parse_energy_kwh("715 kWh\nEstimated Yearly Electricity Use\n") == 715.0
    assert mt.parse_energy_kwh("640 kWh\n716 kWh\n700\nx\n") == 700.0 and mt.parse_energy_kwh("none") is None


def test_model_from_url_and_hosts():
    assert mt.model_from_url("https://www.maytag.com/kitchen/ranges/gas/p.gas-slide-in-range-5.8-cu.-ft.mgs8800pz.html") == "MGS8800PZ"
    assert mt.model_from_url("https://shop.maytag.com/x/p.a.mgs8800pz.html") == "MGS8800PZ"
    for bad in ("http://www.maytag.com/p.a.mgs8800pz.html", "https://evilmaytag.com/p.a.mgs8800pz.html",
                "https://maytag.com.evil.io/p.a.mgs8800pz.html", "https://www.maytag.com/kitchen.html"):
        try:
            mt.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    s = mt.SITE
    assert mt._pdf_url_ok(s, "https://www.maytag.com/a.pdf") and not mt._pdf_url_ok(s, "http://www.maytag.com/a.pdf")
    assert not mt._pdf_url_ok(s, "https://www.amana.com/a.pdf")
    try:
        mt._check_final_url(s, "https://evil.example/x")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_image_url_rules():
    assert mt.main_image_url({}) is None
    assert mt.main_image_url({"picture": "https://evil.example.com/a.tif"}) is None
    assert mt.main_image_url({"picture": "http://www.maytag.com/a.tif"}) is None
    assert mt.main_image_url({"picture": "", "thumbnail": "/is/image/x.tif"}) == (
        "https://www.maytag.com/is/image/x.tif?fmt=jpeg&wid=1200")


def test_dest_path_sanitised_and_contained():
    d = mt._dest_path(mt.SITE, "MGS8800PZ", "Manual")
    assert d.name == "MGS8800PZ_Manual.pdf" and d.parent.name == "maytag"
    d = mt._dest_path(mt.SITE, "../../evil", "Manual")
    assert d.resolve().is_relative_to(mt.common.DOWNLOADS.resolve()) and "/" not in d.name and "\\" not in d.name


def test_search_path():
    u = mt._search_path(mt.SITE, None, 1)
    assert u.startswith("/ws/v2/maytag-us/products/search/singlesource?query=%3Arelevance%3AshowMajorProductsOnly%3Atrue")
    assert "currentPage=1" in u and "pageSize=100" in u
    assert "category%3AKitchenCooking" in mt._search_path(mt.SITE, "KitchenCooking", 0)


def test_modes_and_reset():
    old, env = mt._headless_ok, os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for cached in (None, True, False):
            mt._headless_ok = cached
            os.environ["FRIDGE_BROWSER_MODE"] = "headless"
            assert mt._modes() == [True]
            os.environ["FRIDGE_BROWSER_MODE"] = "visible"
            assert mt._modes() == [False]
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        mt._headless_ok = False
        assert mt._modes() == [False, True]
        mt.reset_browser_mode()
        assert mt._modes() == [True, False]
    finally:
        mt._headless_ok = old
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if env is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = env


def test_connect_closes_browser_on_any_exception():
    closed = []

    class B:
        def new_context(self, **kw):
            raise KeyboardInterrupt  # not an Exception subclass

        def close(self):
            closed.append(1)

    orig = mt.common.launch_browser
    mt.common.launch_browser = lambda p, headless=None: B()
    mt._launch_orig = mt._launch
    mt._launch = lambda p, headless: B()
    try:
        try:
            mt._connect(None, mt.SITE, "https://www.maytag.com/")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected KeyboardInterrupt")
    finally:
        mt.common.launch_browser = orig
        mt._launch = mt._launch_orig
    assert closed == [1]


def test_connect_rejects_foreign_url_before_launch():
    try:
        mt._connect(None, mt.SITE, "https://evil.example/")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_occ_signals_listing_and_detail():
    cands = {c.model_number: c for s in mt.SUPPORTED_SUBCATEGORIES for c in mt.parse_search(_j("search_all.json"), s)}
    c = cands["MEC8830HS"]
    assert c.attrs == {"rating": 3.76, "review_count": 430} and c.attrs_src == {"rating": "listing", "review_count": "listing"}
    assert cands["MGC7430DS"].attrs == {} and cands["MGC7430DS"].attrs_src == {}  # no fields on the site -> unknown
    r, _ = _rec("MEC8830HS")
    assert (r.rating, r.review_count) == (3.76, 430) and r.is_new is None and r.release_date is None
    assert _rec("MGC7430DS")[0].rating is None


def test_occ_signals_edge_cases():
    assert mt.occ_signals({"averageRating": 0, "numberOfReviews": 0}) == {}  # unrated product
    assert mt.occ_signals({"averageRating": 4.5}) == {} and mt.occ_signals({"averageRating": 9, "numberOfReviews": 3}) == {}
    assert mt.occ_signals({"averageRating": "4.25", "numberOfReviews": "12"}) == {"rating": 4.25, "review_count": 12}


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
