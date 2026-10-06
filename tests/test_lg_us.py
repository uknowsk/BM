"""Plain-assert offline tests for lg_us. Run: python tests/test_lg_us.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import lg_us

FIX = Path(__file__).parent / "fixtures" / "lg"
read = lambda n: (FIX / n).read_text(encoding="utf-8")


def test_parse_listing_dedupes_and_limits():
    data = json.loads(read("coveo_sample.json"))
    got = lg_us.parse_listing(data, 30)
    models = [c.model_number for c in got]
    assert len(models) == len(set(models)) == 7  # 6 unique + X1, the duplicate dropped
    first = got[0]
    assert first.brand == "LG" and first.model_number == "LRFLC2716V"
    assert first.url == "https://www.lg.com/us/refrigerators/lg-lrflc2716v-french-3-door-refrigerator"
    assert first.price_usd == 1799.0
    assert got[-1].price_usd is None
    assert len(lg_us.parse_listing(data, 3)) == 3


def test_parse_listing_bad_shape_raises():
    try:
        lg_us.parse_listing({"oops": 1}, 5)
    except lg_us.LGPageError:
        return
    raise AssertionError("expected LGPageError")


def test_pdp_french_door():
    p, raw, links, support = lg_us.parse_pdp(read("fd_pdp.html"), "https://x/fd")
    assert p.brand == "LG" and p.model_number == "LRFLC2716V"
    assert p.door_style == "French Door" and p.finish_color == "Stainless Look"
    assert p.price_usd == 1799
    assert (p.capacity_total_cuft, p.capacity_fridge_cuft, p.capacity_freezer_cuft) == (26.5, 17.8, 8.7)
    assert (p.width_in, p.height_in, p.depth_in) == (35.75, 70.25, 29.12)
    assert p.weight_lb == 249  # source says "249bs / 271 lbs"
    assert p.energy_kwh_year == 632 and p.energy_star is True
    assert p.ice_maker is True and p.water_dispenser is True
    assert p.wifi_supported is True and "Wi-Fi Enabled = Yes" in p.wifi_evidence
    assert len(p.pod_features) == 6 and not any("purchased through" in f for f in p.pod_features)
    assert p.voltage_v is None  # comes from the manual PDF, not the PDP
    assert any(r.key == "Total Capacity (cu.ft.)" and r.value == "26.5 cu.ft." for r in raw)
    assert "SpecSheet" in links and "EnergyGuide" not in links  # energy label is a .jpg
    assert links["Warranty"].endswith("Warranty_REF.pdf")
    assert support.endswith("lg-LRFLC2716V.ANSCNA1")


def test_pdp_bottom_freezer():
    p, raw, links, _ = lg_us.parse_pdp(read("bf_pdp.html"), "https://x/bf")
    assert p.model_number == "LB26H2200S" and p.door_style == "Bottom Freezer"
    assert p.depth_in == 33.43 and p.weight_lb == 234 and p.energy_kwh_year == 562
    assert p.water_dispenser is None  # no dispenser row -> unknown, not False
    assert "EnergyGuide" in links


def test_pdp_unrecognised_raises():
    for html in ("<html></html>", '<script id="__NEXT_DATA__">{"props":{}}</script>'):
        try:
            lg_us.parse_pdp(html, "u")
        except lg_us.LGPageError:
            continue
        raise AssertionError("expected LGPageError")


def test_support_manuals_english_only():
    m = lg_us.parse_support_manuals(read("fd_support.html"))
    assert m == {"Manual": lg_us.MANUAL_DL + "MYBhij3ENObr9tx4D7zw"}
    assert lg_us.parse_support_manuals("") == {}


def test_electrical():
    e = lg_us._electrical("Electrical requirements: 115 V, 60 Hz\nAC, 15 amps minimum.")
    assert e == {"voltage_v": "115", "frequency_hz": 60.0, "amps": 15.0}
    assert lg_us._electrical("nothing") == {"voltage_v": None, "frequency_hz": None, "amps": None}


def test_pdp_thinq_wifi_and_ice_and_color():
    p, raw, links, _ = lg_us.parse_pdp(read("mx_pdp.html"), "https://www.lg.com/x")
    assert p.model_number == "LRMXS3006S"
    assert p.wifi_supported is True and "ThinQ" in p.wifi_evidence  # key actually matched, not 'Wi-Fi Enabled'
    assert "Wi-Fi Enabled" not in p.wifi_evidence
    assert p.ice_maker is True
    assert p.finish_color == "Stainless Steel"  # from 'All Available Colors'


def _pdp_with_spec(rows, features=()):
    nd = {"props": {"pageProps": {"productData": {
        "product": {"sku": "T1", "title": "T", "category": ["refrigerators"], "keyFeatures": [{"feature": f} for f in features]},
        "allInfo": [{"subtitle": "S", "tableData": [{"term": k, "description": v} for k, v in rows]}]}}}}
    return '<script id="__NEXT_DATA__">' + json.dumps(nd) + "</script>"


def test_ice_maker_sources_and_unknown():
    for rows in ([("Dual Ice", "Yes")], [("Ice System", "Slim SpacePlus")], [("Craft Ice™ Daily Ice Production", "2 lbs")]):
        assert lg_us.parse_pdp(_pdp_with_spec(rows), "u")[0].ice_maker is True, rows
    assert lg_us.parse_pdp(_pdp_with_spec([("Width", "30")]), "u")[0].ice_maker is None
    assert lg_us.parse_pdp(_pdp_with_spec([("Width", "30")], ["Includes an ice maker"]), "u")[0].ice_maker is True
    p = lg_us.parse_pdp(_pdp_with_spec([("Wi-Fi Enabled", "No"), ("ThinQ Care", "Yes")]), "u")[0]
    assert p.wifi_supported is False and "Wi-Fi Enabled" in p.wifi_evidence


def _with_env(value, fn):
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        if value is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = value
        return fn()
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_modes_explicit_honored_and_auto_is_order():
    old = lg_us._working_headless
    try:
        for cached in (None, True, False):
            lg_us._working_headless = cached
            assert _with_env("headless", lg_us._modes) == [True]
            assert _with_env("visible", lg_us._modes) == [False]
        lg_us._working_headless = None
        assert _with_env("auto", lg_us._modes) == [True, False]
        lg_us._working_headless = False
        assert _with_env(None, lg_us._modes) == [False, True]  # cached = preferred first, fallback kept
    finally:
        lg_us._working_headless = old


def test_host_checks():
    ok = lg_us._lg_host
    assert ok("https://www.lg.com/us/x") and ok("https://lg.com/x")
    assert not ok("https://evil-lg.com/x") and not ok("https://lg.com.evil.io/x") and not ok("not a url")
    lg_us._check_final_url("https://www.lg.com/us/y")
    try:
        lg_us._check_final_url("https://evil.example/y")
    except lg_us.LGPageError:
        pass
    else:
        raise AssertionError("expected LGPageError")


def test_listing_drops_offdomain_and_pdf_allowlist():
    data = {"results": [
        {"raw": {"ec_model_display_name": "A1", "clickableuri": "https://evil.example/a"}},
        {"raw": {"ec_model_display_name": "B1", "clickableuri": "/us/refrigerators/b1"}}]}
    assert [c.model_number for c in lg_us.parse_listing(data, 9)] == ["B1"]
    assert lg_us._pdf_url_ok("https://gscs-b2c.lge.com/downloadFile?fileId=x")
    assert lg_us._pdf_url_ok("https://www.lg.com/us/a.pdf") and lg_us._pdf_url_ok("https://lg.com/a.pdf")
    assert not lg_us._pdf_url_ok("http://www.lg.com/a.pdf") and not lg_us._pdf_url_ok("https://evil.example/a.pdf")
    assert not lg_us._pdf_url_ok("https://notlge.com/a.pdf")


def test_safe_name():
    assert lg_us._safe_name("../a b") == "_a_b"
    assert "/" not in lg_us._safe_name("a/b" + chr(92) + "c") and not lg_us._safe_name("..x..").startswith(".")
    assert lg_us._safe_name("LRMXS3006S") == "LRMXS3006S"


# ---------------------------------------------------------------- multi-category
def _pdp(name):
    return lg_us.parse_pdp(read(name), "https://www.lg.com/us/x")


def test_supported_subcategories_and_unsupported_raises():
    assert lg_us.SUPPORTED_SUBCATEGORIES == set(lg_us.SUB_RULES)
    assert "scr" not in lg_us.SUPPORTED_SUBCATEGORIES and "french_door" in lg_us.SUPPORTED_SUBCATEGORIES
    assert {"sco", "gas_oven", "gas_cooktop", "radiant", "induction", "electric_oven", "microwave", "otr"} <= lg_us.SUPPORTED_SUBCATEGORIES
    assert all(catalog.major_of(s) == lg_us.SUB_RULES[s].major for s in lg_us.SUPPORTED_SUBCATEGORIES)
    for bad in ("scr", "refrigerator", "nope"):
        try:
            lg_us.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_cq_built_from_rule():
    assert lg_us._cq(lg_us.SUB_RULES["french_door"]).endswith('@ec_category_code=="french_door"')
    assert '(@ec_category_code=="ranges_induction" OR @ec_category_code=="cooktop_induction")' in lg_us._cq(lg_us.SUB_RULES["induction"])
    assert lg_us._cq(lg_us.SUB_RULES["microwave"]).endswith('NOT @ec_category_code=="over_the_range"')
    assert "@ec_model_type==Product" in lg_us._cq(lg_us.SUB_RULES["gas_oven"])


def test_infer_subcategory_from_codes_and_title():
    f = lg_us.infer_subcategory
    assert f(["refrigerators", "french_door", "3_door"], "27 cu. ft. French Door Refrigerator") == "french_door"
    assert f(["refrigerators", "side_by_side"], "26 cu. ft. Smart Side-by-Side Built-In Refrigerator") == "built_in"
    assert f(["refrigerators", "single_door"], "7 cu. ft. Single Door Refrigerator") == "compact"
    assert f(["refrigerators", "single_door"], "11.4 cu. ft. Counter Depth Upright Freezer Column") is None
    assert f(["washers", "washers_and_dryers", "front_load"], "Front Load Washer") == "front_load"
    assert f(["washtower", "washer_dryer_combos"], "WashTower") == "laundry_center"
    assert f(["microwaves", "countertop"], "Countertop Microwave") == "microwave"
    assert f(["microwaves", "over_the_range"], "Over-the-Range Microwave") == "otr"
    assert f(["ranges", "ranges_induction", "wall_ovens"], "Double Oven Induction Slide-in Range") == "induction"
    assert f(["ranges", "wall_ovens", "double"], "LG STUDIO Double Oven Slide-in Range") == "radiant"  # not gas/induction
    assert f(["ranges", "gas"], "6.3 cu. ft. Smart Gas Slide-in Range") == "gas_oven"
    assert f(["ranges", "dual_fuel"], "6.3 cu. ft. Smart Dual Fuel Slide-in Range") == "gas_oven"
    assert f(["ranges", "electric"], "6.3 cu. ft. Electric Slide-in Range") == "radiant"
    assert f(["wall_ovens", "single"], "4.7 cu. ft. Smart Wall Oven") == "electric_oven"
    assert f(["wall_ovens", "combination_wall_ovens"], "1.7/4.7 cu. ft. Smart Combination Wall Oven") == "sco"
    assert f(["wall_ovens", "double"], "1.7/4.7 cu. ft. Combination Double Wall Oven") == "sco"  # no combination code
    assert f(["microwaves", "built_in"], "1.7 cu. ft. Smart Built-In Microwave Speed Oven") == "sco"
    assert f(["microwaves", "over_the_range"], "Over-the-Range Microwave Hood Combination") == "otr"
    assert f(["cooktops", "cooktops_electric", "cooktop_induction"], "Induction Cooktop") == "induction"
    assert f(["cooktops", "cooktops_electric"], "Electric Cooktop") == "radiant"
    assert f(["cooktops", "cooktops_gas"], "Gas Cooktop") == "gas_cooktop"
    assert f(["ranges", "gas"], "Gas Range") == "gas_oven"  # a gas range is not a gas cooktop
    assert f(["dishwashers"], "Dishwasher") is None


def test_parse_listing_by_sub_filters_and_tags():
    data = json.loads(read("coveo_catalog.json"))
    allm = lg_us.parse_listing(data, 500)
    assert all(c.category == "refrigerator" and c.subcategory is None for c in allm)  # untyped legacy call
    # the catalog fixture has no gas cooktop: borrow the one from the cooking fixture so every sub key has a hit
    data["results"] += [r for r in json.loads(read("coveo_cooking.json"))["results"] if "Gas Cooktop" in r["title"]]
    seen = {}
    for sub in lg_us.SUB_RULES:
        got = lg_us.parse_listing(data, 500, sub)
        assert got, sub
        assert all(c.subcategory == sub and c.category == lg_us.SUB_RULES[sub].major for c in got), sub
        seen[sub] = {c.model_number for c in got}
    assert "LRFLC2716V" in seen["french_door"] and "SRSXB2622S" in seen["built_in"]
    assert "SRSXB2622S" not in seen["side_by_side"]
    assert not seen["gas_oven"] & seen["induction"] and not seen["microwave"] & seen["otr"]
    assert len(lg_us.parse_listing(data, 2, "radiant")) <= 2


def test_cooking_fixture_one_sub_per_model():
    data = json.loads(read("coveo_cooking.json"))
    cooking = [k for k, r in lg_us.SUB_RULES.items() if r.major == "cooking"]
    got = {k: {c.model_number for c in lg_us.parse_listing(data, 500, k)} for k in cooking}
    flat = [m for v in got.values() for m in v]
    assert len(flat) == len(set(flat))  # exclusive
    assert {"WCEP6423F", "WCES6428N", "LWC3063BD", "MZBZ1715S"} == got["sco"]  # combo wall ovens + speed oven
    assert {"WSEP4723D", "LWD3063ST"} == got["electric_oven"] and not got["sco"] & got["electric_oven"]
    assert {"LSDL6336F", "LTGL6937F", "LRGL5825F"} <= got["gas_oven"]  # dual fuel + gas ranges
    assert {"LTEL7337F", "LSEL6331F", "LRE3194SW"} <= got["radiant"] and not got["radiant"] & got["gas_oven"]
    assert {"LTIS7338XE", "LSIL6334FE", "LSIU6339XE"} <= got["induction"] and not got["induction"] & got["radiant"]
    assert got["gas_cooktop"] == {"CBGJ3027S"} and not got["gas_cooktop"] & got["gas_oven"]  # oven-less gas cooktop


def test_pdp_front_load_washer():
    p, raw, links, _ = _pdp("wm_pdp.html")
    assert (p.category, p.subcategory, p.model_number) == ("washer", "front_load", "WM6700HWA")
    assert p.capacity_total_cuft == 5.0 and (p.width_in, p.height_in, p.depth_in) == (27.0, 39.0, 33.125)
    assert p.weight_lb == 209.5 and (p.voltage_v, p.amps, p.frequency_hz) == ("120", 10.0, 60.0)
    assert p.energy_star is True and p.wifi_supported is True and "Wi-Fi = Yes" in p.wifi_evidence
    assert p.door_style is None and p.capacity_fridge_cuft is None and p.ice_maker is None
    assert p.water_dispenser is None and p.energy_kwh_year is None
    assert p.extra_specs["Max spin speed (rpm)"] == "1300" and p.extra_specs["IMEF"] == "3.1"
    assert p.extra_specs["Drum axis"] == "Horizontal"
    assert any(r.key == "Max RPM" and r.value == "1300" for r in raw)


def test_pdp_top_load_dryer_and_washtower():
    p, *_ = _pdp("tl_pdp.html")
    assert p.subcategory == "top_load" and p.extra_specs["Agitator"] == "No" and p.extra_specs["Impeller"] == "Yes"
    p, *_ = _pdp("dr_pdp.html")
    assert (p.category, p.subcategory, p.capacity_total_cuft) == ("washer", "dryer", 7.4)
    assert (p.voltage_v, p.amps) == ("120", 11.5) and p.extra_specs["Dryer power source"] == "Gas"
    assert p.extra_specs["Dryer BTU rating"] == "20000"
    p, *_ = _pdp("wt_pdp.html")
    assert p.subcategory == "laundry_center" and p.capacity_total_cuft == 4.5
    assert p.extra_specs["Dryer capacity (cu ft)"] == "7.4" and p.extra_specs["Washer power source"] == "Electric"
    assert p.height_in == 74.375


def test_pdp_microwaves():
    p, *_ = _pdp("mw_pdp.html")
    assert (p.category, p.subcategory, p.capacity_total_cuft) == ("cooking", "microwave", 2.0)
    assert p.wifi_supported is False and "ThinQ" in p.wifi_evidence and p.energy_star is False
    assert p.extra_specs["Microwave power (W)"] == "1200" and p.extra_specs["Turntable diameter (in)"] == "16"
    assert p.door_style is None and p.ice_maker is None and p.energy_kwh_year is None
    p, *_ = _pdp("otr_pdp.html")
    assert p.subcategory == "otr" and p.capacity_total_cuft == 2.1 and p.wifi_supported is True
    assert (p.voltage_v, p.amps, p.frequency_hz) == ("120", 15.0, 60.0) and p.extra_specs["Filtration"] == "Charcoal Filter"


def test_pdp_ranges_ovens_cooktops():
    p, raw, *_ = _pdp("rg_pdp.html")
    assert (p.subcategory, p.capacity_total_cuft) == ("gas_oven", 6.9)
    assert p.extra_specs["Product type"].startswith("Gas Double Oven")  # Summary 'Type', not the Cooktop 'Type'
    assert p.extra_specs["Burners/elements"] == "5" and "Left Front: 17,000 / 12,000" in p.extra_specs["Burner/element detail"]
    assert p.extra_specs["Upper oven capacity (cu ft)"] == "2.6" and p.extra_specs["Lower oven capacity (cu ft)"] == "4.3"
    p, *_ = _pdp("ir_pdp.html")  # induction range that also sits under wall_ovens: induction rule wins
    assert p.subcategory == "induction" and p.depth_in == 26.875
    p, *_ = _pdp("wo_pdp.html")
    assert (p.subcategory, p.capacity_total_cuft, p.width_in) == ("electric_oven", 4.7, 29.75)
    p, *_ = _pdp("ic_pdp.html")
    assert p.subcategory == "induction" and p.capacity_total_cuft is None and p.wifi_supported is None
    assert (p.voltage_v, p.frequency_hz) == ("208/240", 60.0) and p.extra_specs["Burners/elements"] == "4"
    p, *_ = _pdp("rd_pdp.html")
    assert p.subcategory == "radiant" and p.depth_in == 22.0 and p.extra_specs["Fuel type"] == "Electric"


def test_wall_oven_full_spec_table_and_image():
    p, raw, *_ = _pdp("wo_pdp.html")
    assert len(p.extra_specs) >= len({(r.section, r.key) for r in raw})  # every row, 'Section > Label' keys
    for r in raw:
        assert f"{lg_us._noise(r.section)} > {lg_us._noise(r.key)}" in p.extra_specs
    assert "Convection type" in p.extra_specs and "Sabbath mode" in p.extra_specs  # curated kept
    assert p.image_url == ("https://media.us.lg.com/transform/ecomm-PDPGalleryZoom-1600x1062/"
                           "e0e5c44f-902f-45b2-8a30-7792472da291/Wall-Oven-WSEP4727F-gallery-01_5000x5000")
    assert _pdp("fd_pdp.html")[0].image_url is None  # trimmed fixture has no image data: never guessed


def test_main_image_url_rules():
    f = lg_us.main_image_url
    thumb = "https://media.us.lg.com/transform/ecomm-PDPGalleryThumbnail-350x350/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/x_jpg"
    opt = lambda t, gal=None: {"productGroupAttributes": {"images": [{"asset_family_name": "Images", "images_A": {"image_url_asset": t}}],
                                                         "common_gallery_asset": gal or []}}
    assert f(None) is None and f({}) is None and f(opt("")) is None
    assert f(opt("https://evil.example.com/x.jpg")) is None and f(opt(thumb.replace("https", "http"))) is None
    assert f(opt(thumb)) == thumb  # no matching gallery asset: keep the page's own url
    gal = [{"gallery_1": {"zoom_image_addr_asset": "https://media.us.lg.com/z/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/x_jpg"}}]
    assert f(opt(thumb, gal)).startswith("https://media.us.lg.com/z/")
    assert lg_us._noise("Air Fry\u00ae\nSteam\u2122") == "Air Fry | Steam"


def test_fridge_record_marks_category_and_sub():
    p, *_ = lg_us.parse_pdp(read("fd_pdp.html"), "https://x/fd")
    assert (p.category, p.subcategory) == ("refrigerator", "french_door")
    assert p.extra_specs.get("Compressor type")


def test_unsupported_family_raises_value_error():
    nd = {"props": {"pageProps": {"productData": {
        "product": {"sku": "D1", "title": "Dishwasher", "category": ["dishwashers"]},
        "allInfo": [{"subtitle": "S", "tableData": [{"term": "Width", "description": "24"}]}]}}}}
    html = '<script id="__NEXT_DATA__">' + json.dumps(nd) + "</script>"
    try:
        lg_us.parse_pdp(html, "u")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_corrupt_dimension_dropped():
    rows = [("Overall Depth (in) - including handle", '2914743.2"'), ("Width", '30"'), ("Height to Cooking Surface (in)", '36"')]
    html = _pdp_with_spec(rows).replace('"category": ["refrigerators"]', '"category": ["ranges"]')
    p = lg_us.parse_pdp(html.replace('"title": "T"', '"title": "Slide-in Range"'), "u")[0]
    assert p.subcategory == "radiant" and (p.width_in, p.height_in, p.depth_in) == (30.0, 36.0, None)


def _res(model, title, codes):
    return {"title": title, "raw": {"ec_model_display_name": model, "ec_user_friendly_name": title,
                                    "clickableuri": f"/us/x/{model.lower()}", "ec_category_code": codes}}


def test_washtower_and_washcombo_are_laundry_center_not_front_load():
    tower = ["washers_and_dryers", "front_load", "washtower"]
    combo = ["washers_and_dryers", "front_load", "washer_dryer_combos"]
    assert lg_us.infer_subcategory(tower, "LG WashTower") == "laundry_center"
    assert lg_us.infer_subcategory(combo, "LG WashCombo") == "laundry_center"
    assert lg_us.infer_subcategory(["top_load", "washtower"], "LG WashTower") == "laundry_center"
    assert lg_us.infer_subcategory(["front_load"], "LG Washer") == "front_load"
    assert lg_us.infer_subcategory(["top_load"], "LG Washer") == "top_load"
    data = {"results": [_res("WT1", "LG WashTower", tower), _res("WC1", "LG WashCombo", combo),
                        _res("FL1", "LG Front Load Washer", ["front_load"]), _res("TL1", "LG Top Load", ["top_load"])]}
    got = lambda sub: {c.model_number for c in lg_us.parse_listing(data, 30, sub)}
    assert got("front_load") == {"FL1"} and got("top_load") == {"TL1"} and got("laundry_center") == {"WT1", "WC1"}


def test_parse_listing_uses_first_match_classification():
    data = {"results": [_res("FDBF", "LG French Door", ["refrigerators", "french_door", "bottom_freezer"]),
                        _res("BF1", "LG Bottom Freezer", ["refrigerators", "bottom_freezer"]),
                        _res("BI1", "LG Built-In French Door", ["refrigerators", "french_door"])]}
    got = lambda sub: {c.model_number for c in lg_us.parse_listing(data, 30, sub)}
    assert got("french_door") == {"FDBF"} and got("bottom_freezer") == {"BF1"} and got("built_in") == {"BI1"}


def test_discover_pages_through_coveo_and_logs_dropped():
    import io
    from contextlib import redirect_stderr
    posts = []

    def page(first, n):  # 250 results; the 3 built-in ones are the very last (past the first 200)
        plain = [_res(f"M{i}", "LG Refrigerator", ["refrigerators"]) for i in range(first, min(first + n, 247))]
        built = [_res(f"BI{i - 247}", "LG Built-In Refrigerator", ["refrigerators"]) for i in range(max(first, 247), min(first + n, 250))]
        return {"results": plain + built, "totalCount": 250}

    class R:
        def __init__(self, j): self.j, self.url = j, lg_us.TOKEN_URL
        def raise_for_status(self): pass
        def json(self): return self.j

    def post(url, json=None, **k):
        posts.append(json["firstResult"])
        return R(page(json["firstResult"], json["numberOfResults"]))

    orig = (lg_us.requests.get, lg_us.requests.post, lg_us.time.sleep)
    lg_us.requests.get = lambda *a, **k: R({"token": "t"})
    lg_us.requests.post, lg_us.time.sleep = post, (lambda _: None)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            got = lg_us.discover("built_in", limit=30)
    finally:
        lg_us.requests.get, lg_us.requests.post, lg_us.time.sleep = orig
    assert {c.model_number for c in got} == {"BI0", "BI1", "BI2"}
    assert len(posts) >= 2 and posts[0] == 0 and posts == sorted(set(posts))
    assert "247" in err.getvalue() and "dropped" in err.getvalue()


def test_manual_electrical_fills_gaps_but_never_overrides_spec_table():
    import types
    prod = types.SimpleNamespace(voltage_v="120", amps=None, frequency_hz=None)
    lg_us._apply_manual_electrical(prod, "Requires 240 V 60 Hz outlet, 30 amps minimum")
    assert prod.voltage_v == "120" and prod.amps == 30.0 and prod.frequency_hz == 60.0
    fridge = types.SimpleNamespace(voltage_v=None, amps=None, frequency_hz=None)
    lg_us._apply_manual_electrical(fridge, "115 V 60 Hz  15 amps minimum")
    assert (fridge.voltage_v, fridge.amps, fridge.frequency_hz) == ("115", 15.0, 60.0)


def test_pdp_signals_review_and_new_tag():
    new, _, _, _ = lg_us.parse_pdp(read("fd_pdp.html"), "https://x/fd")  # promotionTags ["New", ...], no review block
    assert new.is_new is True and new.rating is None and new.review_count is None
    mx, _, _, _ = lg_us.parse_pdp(read("mx_pdp.html"), "https://x/mx")  # review {points 2.82, reviewers 123}
    assert (mx.rating, mx.review_count) == (2.82, 123) and mx.is_new is None  # tags Top Deal / Best Seller: not new
    bf, _, _, _ = lg_us.parse_pdp(read("bf_pdp.html"), "https://x/bf")
    assert bf.is_new is None and bf.release_date is None


def test_listing_signals_rating_and_new_tag():
    """Coveo raw fields ec_s_rating / ec_default_product_tag (real excerpt); no review count in the listing."""
    mk = lambda m, **kw: {"raw": {"ec_model_display_name": m, "clickableuri": f"/us/refrigerators/{m.lower()}",
                                  "ec_user_friendly_name": "French Door Refrigerator",
                                  "ec_category_code": ["refrigerators", "french_door"], **kw}}
    data = {"results": [mk("LRFLC2716V", ec_s_rating=3.85, ec_default_product_tag="New;LG Online Exclusive"),
                        mk("LRFGC2706S", ec_s_rating=3.94, ec_default_product_tag="LG Online Exclusive"),
                        mk("LZERO", ec_s_rating=0), mk("LNONE")]}
    got = {c.model_number: c for c in lg_us.parse_listing(data, 30, "french_door")}
    a = got["LRFLC2716V"]
    assert a.attrs == {"rating": 3.85, "is_new": True} and a.attrs_src == {"rating": "listing", "is_new": "listing"}
    assert got["LRFGC2706S"].attrs == {"rating": 3.94}  # no 'New' tag -> key omitted, never is_new=False
    assert got["LZERO"].attrs == {} and got["LNONE"].attrs == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
