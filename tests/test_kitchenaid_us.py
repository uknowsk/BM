"""Plain-assert offline tests (saved fixtures). Run: python tests/test_kitchenaid_us.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import kitchenaid_us as ka

FX = Path(__file__).parent / "fixtures" / "kitchenaid"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _url(m):
    return ka.BASE + _j(f"prod_{m}.json")["url"]


def test_parse_search_filters_and_prices():
    cands = ka.parse_search(_j("search.json"))
    codes = [c.model_number for c in cands]
    assert "KRFC236SJP" in codes and "KUIX315SPS" not in codes  # ice maker excluded
    c = cands[0]
    assert c.brand == "KitchenAid"
    assert c.url.startswith("https://www.kitchenaid.com/major-appliances/") and c.price_usd == 2699.0


def test_french_door_record():
    m = "KRFF436SBE"
    r, raw = ka.parse_product(m, _url(m), _j(f"spec_{m}.json"), _j(f"prod_{m}.json"))
    assert r.door_style == "French Door" and r.finish_color == "Black Ore" and r.price_usd == 2499.0
    assert (r.capacity_total_cuft, r.capacity_fridge_cuft, r.capacity_freezer_cuft) == (30.52, 21.51, 9.01)
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (35.9375, 70.0625, 37.4375, 333.0)
    assert (r.voltage_v, r.amps, r.frequency_hz) == ("115", 20.0, 60.0)
    assert r.energy_star is True and r.ice_maker is True and r.water_dispenser is True
    assert r.wifi_supported is False and "Connectivity" in r.wifi_evidence
    assert "Preserva® Food Care System" in r.pod_features and len(r.pod_features) == len(set(r.pod_features))
    assert r.energy_kwh_year is None  # comes from the Energy Guide PDF
    assert any(x.key == "Net Weight" and x.value == "333 lbs" and x.source == "web" for x in raw)


def test_side_by_side_record():
    m = "KRSF536RPS"
    r, _ = ka.parse_product(m, _url(m), _j(f"spec_{m}.json"), _j(f"prod_{m}.json"))
    assert r.door_style == "Side-by-Side" and r.capacity_fridge_cuft == 17.58 and r.capacity_total_cuft == 28.7


def test_docs_and_energy_guide():
    docs = ka.parse_docs(_j("docs_KRFF436SBE.json"))
    types = [t for t, _ in docs]
    assert types == ["EnergyGuide", "Manual", "SpecSheet", "Installation", "QuickSpecs"], types
    assert all(u.startswith("https://") and u.endswith(".pdf") for _, u in docs)
    assert ka.parse_energy_kwh((FX / "energy_guide_KRFF436SBE.txt").read_text(encoding="utf-8")) == 715.0
    assert ka.parse_energy_kwh("no usage here") is None


def test_model_from_url():
    assert ka.model_from_url(_url("KRFF436SBE")) == "KRFF436SBE"
    for bad in ("http://www.kitchenaid.com/p.x.krff436sbe.html", "https://example.com/p.x.krff436sbe.html",
                "https://www.kitchenaid.com/major-appliances.html"):
        try:
            ka.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_unrecognised_structure_raises():
    try:
        ka.parse_product("X", "u", {"nope": 1}, {"name": "n"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_energy_kwh_fallback_layout():
    txt = (FX / "energy_guide_KRSF705HPS.txt").read_text(encoding="utf-8")
    assert ka.parse_energy_kwh(txt) == 640.0
    eg = lambda *lines: chr(10).join(lines) + chr(10)
    assert ka.parse_energy_kwh(eg("716 kWh", "640 kWh", "700", "x")) == 700.0  # lo/hi either order
    assert ka.parse_energy_kwh(eg("640 kWh", "716 kWh", "999", "x")) is None  # outside the range -> unknown
    assert ka.parse_energy_kwh(eg("640 kWh", "716 kWh")) is None


def test_model_from_url_host_strict():
    good = "https://shop.kitchenaid.com/p.x.krff436sbe.html"
    assert ka.model_from_url(good) == "KRFF436SBE"
    for bad in ("https://evilkitchenaid.com/p.x.krff436sbe.html", "https://kitchenaid.com.evil.io/p.x.krff436sbe.html"):
        try:
            ka.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_modes_explicit_honored_and_auto_is_order():
    old, env = ka._headless_ok, os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for cached in (None, True, False):
            ka._headless_ok = cached
            os.environ["FRIDGE_BROWSER_MODE"] = "headless"
            assert ka._modes() == [True]
            os.environ["FRIDGE_BROWSER_MODE"] = "visible"
            assert ka._modes() == [False]
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        ka._headless_ok = None
        assert ka._modes() == [True, False]
        ka._headless_ok = False
        assert ka._modes() == [False, True]
    finally:
        ka._headless_ok = old
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if env is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = env


def test_host_and_pdf_checks():
    assert ka._ka_host("https://www.kitchenaid.com/x") and not ka._ka_host("https://evilkitchenaid.com/x")
    ka._check_final_url("https://www.kitchenaid.com/x")
    try:
        ka._check_final_url("https://evil.example/x")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert ka._pdf_url_ok("https://www.kitchenaid.com/a.pdf") and ka._pdf_url_ok("https://www.whirlpool.com/a.pdf")
    assert not ka._pdf_url_ok("http://www.kitchenaid.com/a.pdf") and not ka._pdf_url_ok("https://evil.example/a.pdf")
    c = ka.parse_search({"products": [
        {"code": "X1", "name": "A Refrigerator", "url": "https://evil.example/p"},
        {"code": "X2", "name": "B Refrigerator", "url": "/major-appliances/p.x2.html"}]})
    assert [x.model_number for x in c] == ["X2"]


def test_dest_path_sanitised_and_contained():
    d = ka._dest_path("KRSF705HPS", "EnergyGuide")
    assert d.name == "KRSF705HPS_EnergyGuide.pdf" and d.resolve().is_relative_to(ka.common.DOWNLOADS.resolve())
    d = ka._dest_path("../../evil", "Manual")
    assert d.resolve().is_relative_to(ka.common.DOWNLOADS.resolve()) and "/" not in d.name and "\\" not in d.name


def test_connect_closes_browser_on_any_exception():
    closed = []

    class B:
        def new_context(self, **kw):
            raise KeyboardInterrupt  # not an Exception subclass

        def close(self):
            closed.append(1)

    class Chromium:
        def launch(self, **kw):
            return B()

    class P:
        chromium = Chromium()

    orig = ka.common.launch_browser
    ka.common.launch_browser = lambda p, headless=None: B()
    try:
        try:
            ka._connect(P(), "https://www.kitchenaid.com/")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected KeyboardInterrupt")
    finally:
        ka.common.launch_browser = orig
    assert closed == [1]


# ---------------------------------------------------------------- multi-category
def _rec(m):
    prod = _j(f"prod_{m}.json")
    return ka.parse_product(m, ka.BASE + prod["url"], _j(f"spec_{m}.json"), prod)


def _simulate_discover(sub):
    """What discover() collects for a sub key, replayed offline from the saved category pages."""
    data, found = _j("search_all.json"), {}
    for seg in ka.SUB_RULES[sub].segments:
        page = {"products": [p for p in data["products"] if p["_cat"] == ka.SEGMENT_CATEGORY[seg]]}
        for c in ka.parse_search(page, sub, seg):
            found.setdefault(c.model_number, c)
    return found


def test_combo_wall_oven_full_spec_table_and_image():
    r, raw = _rec("KOEC730SWH")
    x = r.extra_specs
    assert x["Oven Cavity Details > Oven Rack Type"] == "1 Standard Rack, 1 Gliding Roll-out Rack"
    assert x["Oven Cavity Details > Number of Oven Racks"] == "2"
    assert x["Microwave Details > Sensor Cooking"] == "Yes"
    assert x["Oven Cavity Details > Cooking Power"] == "900 w"
    assert "Air Fry" in x["Oven Cavity Details > Convection Functions"]
    assert x["Air fry"] == "Yes" and "Oven cleaning" in x  # curated labels kept
    assert len(x) >= len(raw)  # every Specs & Details row is present
    assert r.image_url == ("https://www.kitchenaid.com/is/image/content/dam/global/kitchenaid/cooking/"
                           "built-in-oven/images/hero-KOEC730SWH.tif?fmt=jpeg&wid=1200")


def test_main_image_url_rules():
    assert ka.main_image_url({}) is None
    assert ka.main_image_url({"picture": "https://evil.example.com/a.tif"}) is None
    assert ka.main_image_url({"picture": "http://www.kitchenaid.com/a.tif"}) is None
    assert ka.main_image_url({"picture": "", "thumbnail": "/is/image/x.tif"}) == (
        "https://www.kitchenaid.com/is/image/x.tif?fmt=jpeg&wid=1200")
    assert ka._noise("A\u00ae\nB\u2122 ") == "A | B"


def test_supported_subcategories_and_unsupported_raises():
    assert ka.SUPPORTED_SUBCATEGORIES == set(ka.SUB_RULES)
    assert not ka.SUPPORTED_SUBCATEGORIES & {"top_load", "front_load", "dryer", "laundry_center", "top_freezer", "scr"}
    assert {"sco", "gas_oven", "gas_cooktop", "radiant", "induction", "electric_oven", "microwave", "otr"} <= ka.SUPPORTED_SUBCATEGORIES
    assert all(catalog.major_of(s) == ka.SUB_RULES[s].major for s in ka.SUPPORTED_SUBCATEGORIES)
    for bad in ("front_load", "dryer", "scr", "top_freezer", "refrigerator"):
        try:
            ka.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_search_url_uses_category_code():
    u = ka._search_url("MajorAppliancesRanges", 2)
    assert "query=%3Arelevance%3Acategory%3AMajorAppliancesRanges%3AshowMajorProductsOnly%3Atrue" in u and "currentPage=2" in u


def test_discover_by_sub_classification():
    got = {sub: _simulate_discover(sub) for sub in ka.SUB_RULES}
    assert all(got[s] for s in got), {s: len(v) for s, v in got.items()}
    for sub, found in got.items():
        assert all(c.subcategory == sub and c.category == ka.SUB_RULES[sub].major for c in found.values()), sub
    assert "KBSD742SPS" in got["built_in"] and "KBSD742SPS" not in got["side_by_side"]
    assert "KBFN502EBS" in got["built_in"]  # filed under /black-stainless/: family comes from the name
    assert "KRSF536RPS" in got["side_by_side"] and "KRFF436SBE" in got["french_door"] and "KRMF706EBS" in got["french_door"]
    assert "KRBR130SPS" in got["bottom_freezer"] and "KURR124SSB" in got["compact"] and "KUCT524SSB" in got["compact"]
    everything = set().union(*[set(v) for v in got.values()])
    for excluded in ("KUIX315SPS", "KUWR524SBE", "KUBR524SPS"):  # ice maker, wine, beverage
        assert excluded not in everything, excluded
    assert "KMMF730PPS" in got["otr"] and "KMMF730PPS" not in got["electric_oven"]  # OTR oven listed under wall ovens
    assert "KMMS130RPS" in got["otr"] and "KMCS324SSS" in got["microwave"] and "KMCS324SSS" not in got["otr"]
    assert "KFIS930SSS" in got["induction"] and "KCIT736SSS" in got["induction"] and "KFIS930SSS" not in got["gas_oven"]
    assert "KSGS530SPS" in got["gas_oven"] and "KFGS936SSS" in got["gas_oven"] and "KFDS936SSS" in got["gas_oven"]
    assert "KOED730SBE" in got["electric_oven"] and "KOEC730SWH" not in got["electric_oven"]
    assert "KOEC730SWH" in got["sco"] and "KOCE900HSS" in got["sco"] and "KMBS724SPS" in got["sco"]  # combo / speed oven
    assert "KMBT730SPS" in got["sco"] and "KMBS727SPS" in got["microwave"] and "KMBD104GSS" in got["microwave"]
    assert "KOED430RSS" in got["electric_oven"]  # listed under combination-wall-ovens but is a double oven
    assert "KFES530SPS" in got["radiant"] and "KFED500ESS" in got["radiant"] and "KSES530SPS" in got["radiant"]
    assert "KFGS530SPS" not in got["radiant"] and "KFIS930SSS" not in got["radiant"]
    assert "KCES550HBL" in got["radiant"] and "KCIT736SSS" not in got["radiant"] and "KCGK330SSS" not in got["radiant"]
    assert {"KCGK330SSS", "KCGD506GSS", "KCGC506JSS", "KCGG536PBL"} <= set(got["gas_cooktop"])  # cooktops + rangetops
    assert not set(got["gas_cooktop"]) & set(got["gas_oven"])
    cooking = [set(got[s]) for s in ("otr", "sco", "microwave", "induction", "gas_cooktop", "gas_oven", "radiant", "electric_oven")]
    assert sum(map(len, cooking)) == len(set().union(*cooking))  # no model under two sub keys


def test_parse_search_legacy_without_sub_is_refrigerators_only():
    codes = [c.model_number for c in ka.parse_search(_j("search_all.json"))]
    assert "KRFF436SBE" in codes and "KUCT524SSB" in codes  # drawer fridge-freezer with an ice maker is a refrigerator
    assert not {"KSGS530SPS", "KMCS324SSS", "KUWR524SBE", "KUIX315SPS"} & set(codes)


def test_infer_subcategory_from_url_and_name():
    f = ka.infer_subcategory
    base = ka.BASE + "/major-appliances/"
    assert f(base + "refrigeration/x/p.a.html", "36-Inch French Door Refrigerator") == "french_door"
    assert f(base + "refrigeration/x/p.a.html", "42-Inch Built-In French Door Refrigerator") == "built_in"
    assert f(base + "hoods-and-vents/x/p.a.html", "Multifunction Over-the-Range Oven") == "otr"
    assert f(base + "cooktops/x/p.a.html", "36-inch Induction Downdraft Cooktop") == "induction"
    assert f(base + "cooktops/x/p.a.html", "36 in. Gas Cooktop") == "gas_cooktop"
    assert f(base + "cooktops/x/p.a.html", "36'' 6-Burner Commercial-Style Gas Rangetop") == "gas_cooktop"
    assert f(base + "ranges/x/p.a.html", "36'' Commercial-Style Gas Rangetop") == "gas_cooktop"  # filed under /ranges/
    assert f(base + "ranges/x/p.a.html", "36-Inch Gas Range with Cooktop") == "gas_oven"  # has an oven
    assert f(base + "cooktops/x/p.a.html", "30-inch Electric Cooktop") == "radiant"
    assert f(base + "dishwashers/x/p.a.html", "Dishwasher") is None
    assert f(base + "wall-ovens/x/p.a.html", "30-inch Smart Electric Combo Wall Oven") == "sco"
    assert f(base + "wall-ovens/x/p.a.html", "30-inch Electric Double Wall Oven") == "electric_oven"
    assert f(base + "microwaves/x/p.a.html", '24" Built-In More-In-One Convection Microwave Speed Oven') == "sco"
    assert f(base + "ranges/x/p.a.html", "30-Inch 5 Burner Gas Double Oven Convection Range") == "gas_oven"
    assert f(base + "ranges/x/p.a.html", "36-Inch Commercial-Style Dual Fuel Range") == "gas_oven"
    assert f(base + "ranges/x/p.a.html", "30-Inch 5-Element Electric Slide-In Convection Range") == "radiant"
    assert f(base + "ranges/x/p.a.html", "30-Inch 4-Element Induction Freestanding Range") == "induction"


def test_gas_range_record_gas_oven():
    r, raw = _rec("KSGS530SPS")
    assert (r.category, r.subcategory) == ("cooking", "gas_oven") and r.capacity_total_cuft == 5.0
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (29.9, 36.7, 25.4, 222.0)
    assert (r.voltage_v, r.amps, r.frequency_hz) == ("120", 15.0, 60.0)
    assert r.wifi_supported is True and "Alexa" in r.wifi_evidence
    assert r.door_style is None and r.capacity_fridge_cuft is None and r.ice_maker is None and r.water_dispenser is None
    ex = r.extra_specs
    assert ex["Fuel type"] == "Gas" and ex["Burners/elements"] == "5" and ex["Cooking system"] == "True Convection"
    assert ex["Cutout dimensions (W x H x D)"] == "30 in x 36 in x 24 in"
    assert "Left Front: 18,000 BTU" in ex["Burner/element detail"]
    assert any(x.key == "Net Weight" and x.value == "222 lbs" for x in raw) and len(raw) > 50


def test_wall_oven_cooktops_and_microwaves():
    r, _ = _rec("KOED730SBE")
    assert (r.subcategory, r.capacity_total_cuft) == ("electric_oven", 10.0)
    assert r.extra_specs["Upper oven capacity (cu ft)"] == "5" and r.extra_specs["Configuration"] == "Double Oven"
    r, _ = _rec("KOEC730SWH")
    assert r.subcategory == "sco"  # combo wall oven (microwave + oven)
    r, _ = _rec("KCIT736SSS")
    assert r.subcategory == "induction" and r.capacity_total_cuft is None and r.energy_star is True
    assert r.extra_specs["Burners/elements"] == "5" and r.extra_specs["Fuel type"] == "Electric Induction"
    r, _ = _rec("KCES550HBL")
    assert r.subcategory == "radiant" and r.wifi_supported is None and r.extra_specs["Cooktop element style"] == "Radiant"
    r, _ = _rec("KMCS324SSS")
    assert (r.subcategory, r.capacity_total_cuft, r.wifi_supported) == ("microwave", 2.2, False)
    assert r.extra_specs["Microwave cooking power (W)"] == "1200" and r.extra_specs["Power levels"] == "10"
    r, _ = _rec("KMHC319TSS")
    assert r.subcategory == "otr" and r.extra_specs["Vent CFM"] == "400" and r.extra_specs["Convection"] == "Yes"


def test_fridge_variants_keep_fridge_fields():
    r, _ = _rec("KBSD742SPS")
    assert (r.category, r.subcategory, r.door_style) == ("refrigerator", "built_in", "Side-by-Side")
    assert r.ice_maker is True and r.water_dispenser is True and r.capacity_fridge_cuft == 16.3
    assert r.extra_specs["Installation"] == "Built-In" and r.extra_specs["Number of doors"] == "2"
    r, _ = _rec("KURR124SSB")
    assert (r.subcategory, r.door_style) == ("compact", "Undercounter")
    assert r.capacity_total_cuft == 5.0  # no Capacity spec row: taken from the product name
    assert r.ice_maker is None and r.wifi_supported is None
    r, _ = _rec("KRFF436SBE")
    assert (r.category, r.subcategory) == ("refrigerator", "french_door")


def test_unsupported_family_raises_value_error():
    spec = {"specSections": [{"name": "S", "specs": [{"name": "Width", "value": "24 in"}]}]}
    for prod in ({"name": "24-Inch Dishwasher", "url": "/major-appliances/dishwashers/p.x.kdts.html"},):
        try:
            ka.parse_product("KDTS", ka.BASE + prod["url"], spec, prod)
        except ValueError:
            continue
        raise AssertionError(prod)


def test_launch_prefers_new_headless_then_old():
    calls = []

    class Chromium:
        def launch(self, **kw):
            calls.append(kw)
            if kw.get("channel") == "chromium" and len(calls) == 1:
                raise ka.PlaywrightError("no channel")
            return "browser"

    class P:
        chromium = Chromium()

    orig = ka.common.launch_browser
    ka.common.launch_browser = lambda p, headless=None: ("legacy", headless)
    try:
        assert ka._launch(P(), True) == ("legacy", True)  # channel launch failed -> old headless
        assert ka._launch(P(), True) == "browser" and calls[-1] == {"headless": True, "channel": "chromium"}
        assert ka._launch(P(), False) == ("legacy", False)  # visible never uses the channel
    finally:
        ka.common.launch_browser = orig


def _kp(code, name):
    return {"code": code, "name": name, "url": f"/major-appliances/refrigeration/p.x.{code.lower()}.html"}


def test_overlap_products_land_under_exactly_one_sub_key():
    prods = [_kp("BIFD1", "KitchenAid 36-Inch Built-In French Door Refrigerator"),
             _kp("FD1234", "KitchenAid 36-Inch Multi-Door Freestanding French Door Refrigerator"),
             _kp("FDBM12", "KitchenAid 25 cu ft French Door Bottom Mount Refrigerator"),
             _kp("BM1234", "KitchenAid 20 cu ft Bottom Mount Refrigerator"),
             _kp("BISX12", "KitchenAid 48-Inch Built-In Side-by-Side Refrigerator")]
    want = {"BIFD1": "built_in", "FD1234": "french_door", "FDBM12": "french_door", "BM1234": "bottom_freezer",
            "BISX12": "built_in"}
    got = {}
    for sub in ("built_in", "french_door", "side_by_side", "bottom_freezer", "compact"):
        for c in ka.parse_search({"products": prods}, sub, "refrigeration"):
            assert c.model_number not in got, (c.model_number, got[c.model_number], sub)
            got[c.model_number] = sub
    assert got == want
    for p in prods:  # scrape's rule agrees
        assert ka.infer_subcategory(ka.BASE + p["url"], p["name"]) == want[p["code"]]

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
    PwError, Good, Bad = _fake_browsers(ka.BASE)
    seq = [PwError("launch failed"), Bad(), Good()]

    def launch(*a, **k):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    names = [(ka, '_launch')]
    orig = [(o, n, getattr(o, n)) for o, n in names]
    for o, n in names:
        setattr(o, n, launch)
    patches = [(ka, "_modes", lambda: [True, True, False]), (ka, "_dismiss_consent", lambda page: None),
               (ka.common, "looks_blocked", lambda *a: False)]
    orig += [(o, n, getattr(o, n)) for o, n, _ in patches]
    for o, n, v in patches:
        setattr(o, n, v)
    try:
        browser, page = ka._connect(None, ka.LISTING_PAGE)
    finally:
        for o, n, v in orig:
            setattr(o, n, v)
    assert isinstance(browser, Good) and not seq


def test_rating_signals_listing_and_detail():
    c = {x.model_number: x for x in ka.parse_search(_j("search_all.json"), "french_door")}["KRFF436SBE"]
    assert c.attrs == {"rating": 3.54, "review_count": 109} and c.attrs_src == {"rating": "listing", "review_count": "listing"}
    m = "KRFF436SBE"
    r, _ = ka.parse_product(m, _url(m), _j(f"spec_{m}.json"), _j(f"prod_{m}.json"))
    assert (r.rating, r.review_count) == (3.54, 109) and r.is_new is None and r.release_date is None


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
