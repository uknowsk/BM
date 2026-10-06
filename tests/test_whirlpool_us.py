"""Plain-assert offline tests (saved fixtures). Run: python tests/test_whirlpool_us.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
import whirlpool_us as wp

FX = Path(__file__).parent / "fixtures" / "whirlpool"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _rec(m):
    prod = _j(f"prod_{m}.json")
    return wp.parse_product(m, wp.BASE + prod["url"], _j(f"spec_{m}.json"), prod)


def test_supported_set():
    assert "built_in" not in wp.SUPPORTED_SUBCATEGORIES and "scr" not in wp.SUPPORTED_SUBCATEGORIES
    assert {"french_door", "top_load", "dryer", "laundry_center", "sco", "gas_oven", "gas_cooktop", "radiant", "electric_oven",
            "otr", "induction"} <= wp.SUPPORTED_SUBCATEGORIES


def test_discover_unsupported_raises():
    for bad in ("built_in", "scr", "nope"):
        try:
            wp.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_search_french_door():
    c = wp.parse_search(_j("search_KitchenRefrigerationRefrigeratorsFrenchDoor.json"), "french_door")
    assert len(c) == 8 and c[0].brand == "Whirlpool" and c[0].category == "refrigerator"
    assert c[0].subcategory == "french_door" and c[0].url.startswith("https://www.whirlpool.com/kitchen/")
    assert c[0].price_usd == 2599.0


def test_parse_search_filters_and_offdomain():
    data = _j("search_KitchenCompactSmallSpaces.json")
    codes = [c.model_number for c in wp.parse_search(data, "compact")]
    assert "WUR50X24HZ" in codes and "WRT312CZJZ" not in codes  # top-freezer: classify() files it under top_freezer
    assert "WRT312CZJZ" in {c.model_number for c in wp.parse_search(data, "top_freezer")}
    assert "WUW35X24DS" not in codes and "WUB50X24HZ" not in codes and "W5CE1522FB" not in codes
    bad = {"products": [{"code": "X1", "name": "refrigerator", "url": "https://evil.com/p.x.html"},
                        {"code": "X2", "name": "Refurbished refrigerator", "url": "/p.a.x2.html"}]}
    assert wp.parse_search(bad, "compact") == []


def test_cooktop_induction_vs_radiant():
    data = _j("search_KitchenCookingCooktops.json")
    ind = {c.model_number for c in wp.parse_search(data, "induction")}
    rad = {c.model_number for c in wp.parse_search(data, "radiant")}
    assert {"WCI55US0JB", "WCIT6030SB"} <= ind and not ind & rad
    assert {"WCE97US0KS", "WCC31430AW", "RCS2012RS"} <= rad and "WCGK5036PS" not in rad  # coil + ceramic electric; gas out
    assert "WCGK5036PS" in {c.model_number for c in wp.parse_search(data, "gas_cooktop")}


def _cooking_subs(listing_files):
    """sub key -> model set, replaying parse_search over the saved listing pages."""
    got = {k: set() for k, v in wp.SUBS.items() if v.major == "cooking"}
    for f in listing_files:
        data = _j(f)
        for k in got:
            got[k] |= {c.model_number for c in wp.parse_search(data, k)}
    return got


def test_cooking_overlap_ranges_ovens_microwaves():
    got = _cooking_subs(["search_KitchenCookingRanges.json", "search_KitchenCookingWallOvens.json",
                         "search_KitchenCookingCooktops.json", "search_KitchenCookingMicrowavesOvertheRange.json"])
    assert {"WOEC7030PV", "WOEC3030LS", "WOEC5027LW"} <= got["sco"]  # microwave combination wall ovens
    assert not got["sco"] & got["electric_oven"] and "WOEC7030PV" not in got["electric_oven"]
    assert {"WOED5030LZ", "WOES3030LS"} <= got["electric_oven"]
    assert {"WFGS5030RS", "WGG745S0FS", "WSGS7530RV"} <= got["gas_oven"]  # gas ranges
    assert {"WFES5030RW", "WGE745C0FS", "WSES7530RV"} <= got["radiant"] and "WFES5030RW" not in got["gas_oven"]
    assert "WCE97US0KS" in got["radiant"] and "WCGK5036PS" not in got["radiant"]  # electric cooktop still radiant
    assert "WCGK5036PS" in got["gas_cooktop"] and not got["gas_cooktop"] & got["gas_oven"]
    assert {"WCI55US0JB", "WCIT6030SB"} <= got["induction"]
    everything = [m for v in got.values() for m in v]
    assert len(everything) == len(set(everything))  # no model under two subs


def test_classify_range_and_oven_rules():
    mk = lambda piped, name: {"name": name, "primaryCategory": {"pipedCategory": piped}}
    c = wp.classify
    assert c(mk("Kitchen|Cooking|Wall-Ovens|Microwave-Oven-Combo", "6.4 Cu. Ft. Wall Oven")) == ("cooking", "sco")
    assert c(mk("Kitchen|Cooking|Wall-Ovens|Double-Oven", "Double Wall Oven")) == ("cooking", "electric_oven")
    assert c(mk("Kitchen|Cooking|Wall-Ovens|Single-Oven", "Single Wall Oven with Microwave")) == ("cooking", "sco")
    assert c(mk("Kitchen|Cooking|Microwaves|Built-In", "30-inch Built-In Microwave Oven")) == ("cooking", "microwave")
    assert c(mk("Kitchen|Cooking|Microwaves|Built-In", "Speed Oven with Microwave")) == ("cooking", "sco")
    assert c(mk("Kitchen|Cooking|Ranges|Gas", "30-inch Gas Range")) == ("cooking", "gas_oven")
    assert c(mk("Kitchen|Cooking|Ranges|Slide-In", "30-inch Dual Fuel Range")) == ("cooking", "gas_oven")
    assert c(mk("Kitchen|Cooking|Ranges|Electric", "30-inch Electric Range")) == ("cooking", "radiant")
    assert c(mk("Kitchen|Cooking|Ranges|Electric", "30-inch Induction Range")) == ("cooking", "induction")
    assert c(mk("Kitchen|Cooking|Cooktops|Gas", "30-inch Gas Cooktop")) == ("cooking", "gas_cooktop")
    assert c(mk("Kitchen|Cooking|Cooktops|Electric", "30-inch Electric Cooktop")) == ("cooking", "radiant")


def test_washer_excludes_laundry_centers_and_refurbs():
    codes = {c.model_number for c in wp.parse_search(_j("search_LaundryWashersTopLoad.json"), "top_load")}
    assert "WTW8127LW" in codes and "WGT4027HW" not in codes and "RWTW4957PW" not in codes


def test_fridge_record():
    r, raw = _rec("WRS325SDHZ")
    assert (r.category, r.subcategory, r.door_style) == ("refrigerator", "side_by_side", "Side-by-Side")
    assert r.price_usd == 1399.0 and r.finish_color == "Fingerprint Resistant Stainless Finish"
    assert (r.capacity_total_cuft, r.capacity_fridge_cuft, r.capacity_freezer_cuft) == (24.5, 15.44, 9.11)
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (35.875, 69.625, 33.625, 223.0)
    assert r.frequency_hz == 60.0 and r.energy_star is False and r.ice_maker is True and r.water_dispenser is True
    assert r.wifi_supported is False and "Connectivity" in r.wifi_evidence
    assert r.energy_kwh_year is None and "Frameless Glass Shelves" in r.pod_features
    assert "Prop 65" not in r.extra_specs and "<" not in "".join(x.value for x in raw)
    assert r.extra_specs["Handle Type"] == "Reach Through Handle" and "Width" not in r.extra_specs
    assert any(x.key == "Net Weight" and x.value == "223 lbs" and x.source == "web" for x in raw)


def test_washer_record_no_fridge_fields():
    r, raw = _rec("WFW5720RW")
    assert (r.category, r.subcategory) == ("washer", "front_load")
    assert r.door_style is None and r.capacity_total_cuft is None and r.ice_maker is None
    assert r.extra_specs["Capacity"] == "4.5 cu. ft." and r.extra_specs["Number of Wash Cycles"] == "12"
    assert (r.voltage_v, r.amps, r.frequency_hz, r.energy_star, r.wifi_supported) == ("120", 15.0, 60.0, True, True)
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (27.0, 39.0, 31.5625, 222.0)
    assert len(raw) == sum(1 for s in _j("spec_WFW5720RW.json")["specSections"] for _ in s["specs"])


def test_range_record():
    r, _ = _rec("WFES5030RW")
    assert (r.category, r.subcategory, r.wifi_supported) == ("cooking", "radiant", False)
    assert r.extra_specs["Fuel Type"] == "Electric" and r.voltage_v is None and r.energy_star is None
    assert "ENERGY STAR® Certified" in r.pod_features


def test_range_full_spec_table_and_image():
    r, raw = _rec("WFES5030RW")
    x = r.extra_specs
    assert x["Oven Cavity Details > Oven Rack Type"] == x["Oven Rack Type"]  # sectioned + legacy flat label
    assert "Oven Cavity Details > Number of Oven Racks" in x
    assert len(x) >= len(raw) and all(f"{q.section} > {q.key}" in x for q in raw)  # every row, incl. consumed ones
    assert r.image_url == ("https://www.whirlpool.com/is/image/content/dam/global/whirlpool/cooking/range/"
                           "images/hero-WFES5030RW.tif?fmt=jpeg&wid=1200")
    r2, _ = _rec("WRS325SDHZ")
    assert "Dimensions > Width" in r2.extra_specs and r2.image_url is None  # no picture in that fixture


def test_main_image_url_rules():
    assert wp.main_image_url({}) is None
    assert wp.main_image_url({"picture": "https://evil.example.com/a.tif"}) is None
    assert wp.main_image_url({"picture": "http://www.whirlpool.com/a.tif"}) is None
    assert wp.main_image_url({"picture": "", "thumbnail": "/is/image/x.tif"}) == (
        "https://www.whirlpool.com/is/image/x.tif?fmt=jpeg&wid=1200")


def test_classify_names():
    mk = lambda piped, name: {"name": name, "primaryCategory": {"pipedCategory": piped}}
    assert wp.classify(mk("Laundry|Laundry-Sets|Washer-Dryer-Combination", "3.5 cu.ft Gas Stacked Laundry Center")) == ("washer", "laundry_center")
    assert wp.classify(mk("Laundry|Laundry-Sets|Washer-Dryer-Combination", "Ventless All In One Washer Dryer")) == ("washer", None)
    assert wp.classify(mk("Kitchen|Cooking|Cooktops|Electric", '30" Induction Cooktop')) == ("cooking", "induction")
    assert wp.classify(mk("Kitchen|Cooking|Microwaves|Over-the-Range", "1.9 Microwave")) == ("cooking", "otr")
    assert wp.classify(mk("Kitchen|Cooking|Microwaves|Countertop", "0.9 Microwave")) == ("cooking", "microwave")
    assert wp.classify(mk("Kitchen|Dishwasher|X", "dw")) == ("other", None)


def test_unrecognised_structure_raises():
    try:
        wp.parse_product("X", "u", {"nope": 1}, {"name": "n"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_docs():
    d = wp.parse_docs(_j("docs_WRS325SDHZ.json"))
    assert [t for t, _ in d] == ["EnergyGuide", "Manual", "SpecSheet", "Installation", "QuickSpecs"], d
    assert all(u.startswith("https://www.whirlpool.com/") and u.endswith(".pdf") for _, u in d)
    w = dict(wp.parse_docs(_j("docs_WFW5720RW.json")))
    assert "installation-instructions" in w["Installation"].lower() and "cycle" not in str(w)
    offsite = {"documents": [{"doc_type": ["owners-manual"], "language": ["en_us"], "asseturl": "https://evil.com/a.pdf"}]}
    assert wp.parse_docs(offsite) == []


def test_energy_kwh():
    assert wp.parse_energy_kwh("715 kWh\nEstimated Yearly Electricity Use\n") == 715.0
    eg = lambda *ln: chr(10).join(ln) + chr(10)
    assert wp.parse_energy_kwh(eg("716 kWh", "640 kWh", "700", "x")) == 700.0
    assert wp.parse_energy_kwh(eg("640 kWh", "716 kWh", "999", "x")) is None
    assert wp.parse_energy_kwh("none") is None


def test_model_from_url():
    u = "https://www.whirlpool.com/laundry/washers/front-load/p.4.5-cu.-ft.-front-load-energy-star-washer.wfw4720rw.html"
    assert wp.model_from_url(u) == "WFW4720RW"
    for bad in (u.replace("https", "http"), "https://evilwhirlpool.com/p.x.wfw4720rw.html",
                "https://whirlpool.com.evil.io/p.x.wfw4720rw.html", "https://www.whirlpool.com/laundry/washers.html"):
        try:
            wp.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_modes():
    old, env = wp._headless_ok, os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for cached in (None, True, False):
            wp._headless_ok = cached
            os.environ["FRIDGE_BROWSER_MODE"] = "headless"
            assert wp._modes() == [True]
            os.environ["FRIDGE_BROWSER_MODE"] = "visible"
            assert wp._modes() == [False]
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        wp._headless_ok = None
        assert wp._modes() == [True, False]
        wp._headless_ok = False
        assert wp._modes() == [False, True]
    finally:
        wp._headless_ok = old
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if env is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = env


def test_hosts_and_paths():
    assert wp._pdf_url_ok("https://www.whirlpool.com/a.pdf") and wp._pdf_url_ok("https://x.whirlpoolcorp.com/a.pdf")
    for bad in ("http://www.whirlpool.com/a.pdf", "https://whirlpool.com.evil.io/a.pdf", "https://evil.com/a.pdf"):
        assert not wp._pdf_url_ok(bad)
    for bad in ("https://example.com/x", "http://www.whirlpool.com/x"):
        try:
            wp._check_final_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    p = wp._dest_path("../../evil/W1", "Manual")
    assert p.resolve().is_relative_to(common.DOWNLOADS.resolve()) and "/" not in p.name and "\\" not in p.name
    assert p.parent.name == "whirlpool"


_R = "Kitchen|Refrigeration|Refrigerators|"


def _prod(name, *paths, code="WX1"):
    return {"code": code, "name": name, "url": f"/kitchen/refrigeration/refrigerators/p.x.{code.lower()}1234.html",
            "categories": [{"pipedCategory": p} for p in paths]}


def test_classify_counter_depth_side_by_side_is_not_french_door():
    cd_sxs = _prod("36-inch Wide Counter Depth Side-by-Side Refrigerator", _R + "Counter-Depth", _R + "Side-by-Side")
    assert wp.classify(cd_sxs) == ("refrigerator", "side_by_side")
    cd_sxs_only = _prod("36-inch Wide Counter-Depth Side-by-Side Refrigerator", _R + "Counter-Depth")
    assert wp.classify(cd_sxs_only) == ("refrigerator", "side_by_side")
    cd_fd = _prod("36-inch Wide Counter-Depth French Door Refrigerator", _R + "Counter-Depth", _R + "French-Door")
    assert wp.classify(cd_fd) == ("refrigerator", "french_door")
    assert wp.classify(_prod("French Door", _R + "French-Door", _R + "Bottom-Freezer")) == ("refrigerator", "french_door")
    assert wp.classify(_prod("Counter-Depth thing", _R + "Counter-Depth")) == ("refrigerator", None)


def test_parse_search_agrees_with_classify():
    data = {"products": [
        _prod("Counter Depth Side-by-Side Refrigerator", _R + "Counter-Depth", _R + "Side-by-Side", code="CDSXS"),
        _prod("French Door Refrigerator", _R + "French-Door", code="FD1"),
        _prod("Bottom-Freezer Refrigerator", _R + "Bottom-Freezer", code="BF1")]}
    codes = lambda sub: {c.model_number for c in wp.parse_search(data, sub)}
    assert codes("french_door") == {"FD1"} and codes("side_by_side") == {"CDSXS"} and codes("bottom_freezer") == {"BF1"}


def test_subs_are_mutually_exclusive_for_overlap_products():
    prods = [_prod("Counter-Depth Side-by-Side Refrigerator", _R + "Counter-Depth", _R + "Side-by-Side"),
             _prod("French Door Refrigerator", _R + "French-Door", _R + "Bottom-Freezer"),
             _prod("Refrigerator", _R + "Counter-Depth", _R + "French-Door", _R + "Side-by-Side")]
    import io
    from contextlib import redirect_stderr
    for p in prods:
        with redirect_stderr(io.StringIO()):
            hits = [k for k in wp.SUBS if wp.parse_search({"products": [p]}, k)]
        assert len(hits) <= 1, hits

def _fake_browsers(mod_base):
    import types
    from playwright.sync_api import Error as PwError

    class Page:
        url = mod_base

        def goto(self, *a, **k): return types.SimpleNamespace(status=200)
        def wait_for_function(self, *a, **k): pass
        def wait_for_load_state(self, *a, **k): pass
        def inner_text(self, sel): return "x" * 300

    class Good:
        def new_context(self, **k): return types.SimpleNamespace(new_page=lambda: Page())
        def close(self): pass

    class Bad(Good):
        def new_context(self, **k): raise PwError("nav failed")
        def close(self): raise PwError("close failed")

    return PwError, Good, Bad

def test_connect_survives_launch_error_and_close_error():
    PwError, Good, Bad = _fake_browsers(wp.BASE)
    seq = [PwError("launch failed"), Bad(), Good()]

    def launch(*a, **k):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    names = [(wp, '_launch')]
    orig = [(o, n, getattr(o, n)) for o, n in names]
    for o, n in names:
        setattr(o, n, launch)
    patches = [(wp, "_modes", lambda: [True, True, False]), (wp, "_dismiss_consent", lambda page: None),
               (wp.common, "looks_blocked", lambda *a: False)]
    orig += [(o, n, getattr(o, n)) for o, n, _ in patches]
    for o, n, v in patches:
        setattr(o, n, v)
    try:
        browser, page = wp._connect(None, wp.BASE + "/kitchen/refrigeration/refrigerators.html")
    finally:
        for o, n, v in orig:
            setattr(o, n, v)
    assert isinstance(browser, Good) and not seq


def test_rating_signals_listing_and_detail():
    data = _j("search_KitchenCookingRanges.json")
    cands = {c.model_number: c for s in wp.SUPPORTED_SUBCATEGORIES for c in wp.parse_search(data, s)}
    c = cands["WFES5030RW"]
    assert c.attrs == {"rating": 4.54, "review_count": 104} and c.attrs_src == {"rating": "listing", "review_count": "listing"}
    r = _rec("WFES5030RW")[0]
    assert (r.rating, r.review_count) == (4.54, 104) and r.is_new is None and r.release_date is None
    assert _rec("WRS325SDHZ")[0].rating is None  # fixture without review fields -> unknown


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f()
                print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)
