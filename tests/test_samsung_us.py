"""Plain-assert offline tests for samsung_us (saved fixtures). Run: python tests/test_samsung_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsung_us as s

FX = Path(__file__).resolve().parent / "fixtures" / "samsung"


def _load(name):
    return (FX / name).read_text(encoding="utf-8")


def _build(tag, url):
    nd = s.parse_next_data(_load(f"pdp_{tag}.html"))
    rows, spec_url, supports = s.parse_bridge(json.loads(_load(f"bridge_{tag}.json")), nd["product"]["modelCode"])
    return s.build_record(url, nd, rows, spec_url, supports)


def test_wall_oven_full_spec_table_and_image():
    p, _ = _build("wo700", "https://www.samsung.com/us/cooking-appliances/wall-ovens/x/")
    nd = s.parse_next_data(_load("pdp_wo700.html"))
    rows, _su, _sp = s.parse_bridge(json.loads(_load("bridge_wo700.json")), nd["product"]["modelCode"])
    for g, n, v in rows:  # every Specs row present, under 'Group > Name'
        assert f"{s._noise(g)} > {s._noise(n)}" in p.extra_specs, (g, n)
    assert len(p.extra_specs) >= len({(g, n) for g, n, _ in rows})
    assert "Oven capacity (cu ft)" in p.extra_specs or "Convection" in p.extra_specs  # curated labels kept
    assert p.image_url == ("https://images.samsung.com/is/image/samsung/p6pim/us/nv51cg700ssraa/gallery/"
                           "us-nv7000c-nv51cb700s12aa-nv51cg700ssraa-549633204?$product-details-jpg$")


def test_main_image_url_rules():
    f = s.main_image_url
    assert f({}) is None and f({"gallery": []}) is None and f({"gallery": [None]}) is None
    assert f({"defaultImage": "https://evil.example.com/a.png"}) is None
    assert f({"defaultImage": "http://images.samsung.com/a.png"}) is None
    assert f({"gallery": [{"url": "//images.samsung.com/a.jpg"}]}) == "https://images.samsung.com/a.jpg"
    assert f({"gallery": [{"url": ""}], "defaultImage": "/us/img/a.png"}) == "https://www.samsung.com/us/img/a.png"
    assert s.full_spec_table([("G\u00ae", "N", "a\nb"), ("G", "N", "c"), ("", "L", "?")]) == {"G > N": "a | b | c"}


def test_parse_search():
    c = s.parse_search(json.loads(_load("pf_search_page1.json")))
    assert len(c) == 6  # entry without pdpURL skipped; duplicate kept here, deduped in discover()
    assert c[0].brand == "Samsung" and c[0].model_number == "RF29DB9900QDAA"
    assert c[0].url.startswith("https://www.samsung.com/us/refrigerators/") and c[0].url.endswith("/")
    assert c[0].price_usd == 3699.0


def test_french_door_record():
    p, docs = _build("fd", "https://www.samsung.com/us/refrigerators/french-door/x/")
    assert p.model_number == "RF29DB9900QDAA" and p.door_style == "French Door"
    assert p.finish_color == "Stainless Steel" and p.price_usd == 3699.0
    assert (p.capacity_total_cuft, p.capacity_fridge_cuft, p.capacity_freezer_cuft) == (29.0, 17.4, 11.6)
    assert (p.width_in, p.height_in, p.depth_in, p.weight_lb) == (35.875, 73.0, 34.25, 363.8)
    assert p.voltage_v == "115" and p.frequency_hz == 60.0 and p.amps is None  # amps not published -> None
    assert p.energy_kwh_year == 700.0 and p.energy_star is True
    assert p.ice_maker is True and p.water_dispenser is True
    assert p.wifi_supported is True and "Wi-Fi" in p.wifi_evidence
    assert "AI Vision Inside" in p.pod_features and len(p.pod_features) == len(set(p.pod_features))
    types = [t for t, _ in docs]
    assert {"Manual", "EnergyGuide", "SpecSheet"} <= set(types) and len(docs) <= s.MAX_PDFS
    assert all(u.startswith("https://") for _, u in docs)  # http downloadcenter urls upgraded


def test_top_freezer_record():
    p, _ = _build("tf", "https://www.samsung.com/us/refrigerators/top-freezer/x/")
    assert p.door_style == "Top Freezer" and p.model_number == "RT70F18LASRAA"
    assert p.energy_kwh_year == 446.0 and p.depth_in == 31.875
    assert p.voltage_v is None and p.water_dispenser is None  # silent in data -> None, never invented


def test_unrecognised_structure_raises():
    for bad in ("<html>nothing</html>", '<script id="__NEXT_DATA__">{"props":{}}</script>'):
        try:
            s.parse_next_data(bad)
        except s.SamsungPageError:
            continue
        raise AssertionError("expected SamsungPageError")
    try:
        s.parse_bridge({"Specs": []}, "X")
    except s.SamsungPageError:
        return
    raise AssertionError("expected SamsungPageError")


def test_discover_dedupes_and_limits(monkeypatch_payload=None):
    payload = json.loads(_load("pf_search_page1.json"))
    calls = []

    class R:
        def raise_for_status(self): pass
        def json(self): return {**payload, "hasMoreResults": len(calls) < 3}

    real_post, real_sleep = s.requests.post, s.time.sleep
    s.requests.post = lambda *a, **k: (calls.append(k["data"]["startIndex"]), R())[1]
    s.time.sleep = lambda _: None
    s._clear_page_cache()
    try:
        got = s.discover("french_door", limit=4)
        assert len(got) == 4 and len({c.model_number for c in got}) == 4
        calls.clear()
        s._clear_page_cache()
        got = s.discover("french_door", limit=30)
        assert len(got) == 5  # 5 unique models, pagination stops when hasMoreResults is false
        assert calls == [0, 14, 28]
    finally:
        s.requests.post, s.time.sleep = real_post, real_sleep


def test_kk_record_formerly_none_fields():
    p, docs = _build("kk", "https://www.samsung.com/us/refrigerators/french-door/x/")
    assert p.model_number == "RF70H30KEEAA"
    assert (p.capacity_total_cuft, p.capacity_fridge_cuft, p.capacity_freezer_cuft) == (29.5, 20.8, 8.7)
    assert p.weight_lb == 282.19  # web 'Net Weight(lb)', not the PDF's shipping/other figure
    assert p.wifi_supported is True
    assert "Smart" in p.wifi_evidence and "Wi-Fi Embedded" in p.wifi_evidence
    assert p.water_dispenser is True
    assert p.energy_kwh_year == 689.0
    assert all(s._pdf_url_ok(u) for _, u in docs)


def test_energy_kwh_regex_ignores_lone_dot():
    rows = [("Performance", "Energy Consumption", ". kWh/year")]
    assert s._energy_kwh(rows) is None
    assert s._energy_kwh([("P", "Energy Consumption", "1,234.5 kWh/year")]) == 1234.5


def test_wifi_evidence_names_matched_key_and_smartthings():
    rows = [("App Connectivity", "SmartThings App Support", "Yes")]
    ok, ev = s._wifi(rows)
    assert ok is True and "App Connectivity" in ev and "SmartThings App Support" in ev
    assert s._wifi([("Smart", "Wi-Fi Embedded", "No")])[0] is False
    assert s._wifi([])[0] is None


def test_host_allowlist():
    assert s._is_samsung_url("https://www.samsung.com/us/x/")
    assert s._is_samsung_url("https://images.samsung.com/a.pdf")
    assert not s._is_samsung_url("https://evilsamsung.com/")
    assert not s._is_samsung_url("https://samsung.com.evil.io/")
    assert not s._is_samsung_url("http://www.samsung.com/")
    assert s._pdf_url_ok("https://images.samsung.com/a.pdf") and not s._pdf_url_ok("https://evil.io/a.pdf")


def test_safe_filename_model():
    assert s._safe_model("../a/b c") == "_a_b_c"  # leading dots stripped, separators/space replaced
    assert "/" not in s._safe_model(r"a/b\c") and s._safe_model("RF29DB9900QDAA") == "RF29DB9900QDAA"


def test_discover_filters_non_samsung_hosts():
    payload = {"searchResults": [
        {"modelCode": "A1", "pdpURL": "https://evil.io/x/", "productDisplayName": "bad"},
        {"modelCode": "B2", "pdpURL": "/us/refrigerators/french-door/x/", "productDisplayName": "ok"}], "hasMoreResults": False}

    class R:
        def raise_for_status(self): pass
        def json(self): return payload

    real = s.requests.post
    s.requests.post = lambda *a, **k: R()
    s._clear_page_cache()
    try:
        got = s.discover("french_door", limit=5)
    finally:
        s.requests.post = real
        s._clear_page_cache()
    assert [c.model_number for c in got] == ["B2"]


_BIG = "x" * 300


def _html(extra=""):
    return f'<html><script id="__NEXT_DATA__">{{}}</script>{extra}{_BIG}</html>'


class _Resp:
    def __init__(self, text, status=200, url="https://www.samsung.com/us/x/"):
        self.text, self.status_code, self.url = text, status, url


def test_next_data_page_not_blocked_even_with_captcha_word():
    assert s._page_ok(200, _html("<script>captcha-lib</script>")) is True
    assert s._page_ok(403, _html()) is False
    assert s._page_ok(200, "<html>captcha " + _BIG + "</html>") is False  # no __NEXT_DATA__


def _patch_browser(calls, results):
    real = s._playwright_html
    def fake(url, headless):
        calls.append(headless)
        r = results[headless]
        if isinstance(r, Exception):
            raise r
        return r
    s._playwright_html = fake
    return real


def test_load_html_modes_and_no_permanent_headed():
    import os
    real_get, old = s.requests.get, os.environ.get("FRIDGE_BROWSER_MODE")
    blocked = (403, "denied")
    good = (200, _html())
    try:
        # auto: requests blocked -> headless blocked -> visible works
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        s.requests.get = lambda *a, **k: _Resp("denied", 403)
        calls = []; real = _patch_browser(calls, {True: blocked, False: good})
        assert s._load_html("https://www.samsung.com/us/x/") == good[1] and calls == [True, False]
        # next call: cheap requests path is tried again (no permanent headed state)
        s.requests.get = lambda *a, **k: _Resp(_html())
        calls.clear()
        assert s._load_html("https://www.samsung.com/us/x/") == _html() and calls == []
        # headless mode never goes visible
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        s.requests.get = lambda *a, **k: _Resp("denied", 403)
        calls.clear(); _patch_browser(calls, {True: blocked, False: good})
        try:
            s._load_html("https://www.samsung.com/us/x/"); raise AssertionError("expected error")
        except s.SamsungPageError:
            pass
        assert calls == [True]
        # visible mode skips requests and headless
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        s.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("requests used"))
        calls.clear(); _patch_browser(calls, {True: blocked, False: good})
        assert s._load_html("https://www.samsung.com/us/x/") == good[1] and calls == [False]
    finally:
        s.requests.get, s._playwright_html = real_get, real
        if old is None: os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else: os.environ["FRIDGE_BROWSER_MODE"] = old


def test_load_html_rejects_offsite_redirect():
    import os
    real_get = s.requests.get
    os.environ["FRIDGE_BROWSER_MODE"] = "headless"
    s.requests.get = lambda *a, **k: _Resp(_html(), url="https://evil.io/x")
    calls = []; real = _patch_browser(calls, {True: (200, _html()), False: (200, _html())})
    try:
        os.environ["FRIDGE_BROWSER_MODE"] = "auto"
        try:
            s._load_html("https://www.samsung.com/us/x/"); raise AssertionError("expected error")
        except s.SamsungPageError:
            pass
    finally:
        s.requests.get, s._playwright_html = real_get, real
        os.environ.pop("FRIDGE_BROWSER_MODE", None)


# ---------------------------------------------------------------- multi-category (laundry / cooking)
_CODES = {"fridge": "08030000", "laundry": "08010000", "cooking": "08080000", "microwave": "08110000"}


def _search_counts(tag):
    payload = json.loads(_load(f"pf_search_{tag}.json"))
    return {sub: len(s.parse_search(payload, sub)) for sub, (_, codes) in s.SUB_SOURCES.items()
            if codes == [_CODES[tag]]}


def test_supported_subcategories_and_codes():
    import catalog
    assert s.SUPPORTED_SUBCATEGORIES == {"french_door", "side_by_side", "top_freezer", "top_load", "front_load",
                                         "dryer", "laundry_center", "microwave", "otr", "sco", "gas_oven",
                                         "gas_cooktop", "electric_oven", "induction", "radiant"}
    assert s.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys())
    assert all(catalog.major_of(sub) == major for sub, (major, _) in s.SUB_SOURCES.items())


def test_search_classification_counts_per_sub_key():
    assert _search_counts("fridge") == {"french_door": 28, "side_by_side": 6, "top_freezer": 4}  # 2 upright freezers dropped
    assert _search_counts("laundry") == {"top_load": 7, "front_load": 7, "dryer": 15, "laundry_center": 4}  # 6 F- packages dropped
    assert _search_counts("cooking") == {"sco": 3, "gas_oven": 8, "gas_cooktop": 3, "electric_oven": 6, "induction": 7, "radiant": 10}  # hoods dropped
    assert _search_counts("microwave") == {"microwave": 1, "otr": 9}


def test_cooking_fixture_has_no_model_under_two_subs():
    payload = json.loads(_load("pf_search_cooking.json"))
    by_sub = {sub: {c.model_number for c in s.parse_search(payload, sub)}
              for sub, (major, _) in s.SUB_SOURCES.items() if major == "cooking" and sub not in ("microwave", "otr")}
    flat = [m for v in by_sub.values() for m in v]
    assert len(flat) == len(set(flat))
    assert {"NQ70CG700DSRAA", "NQ70CG600DSRAA", "NQ70T5511DSAA"} == by_sub["sco"]  # combi wall ovens, not electric_oven
    assert not by_sub["sco"] & by_sub["electric_oven"]
    assert "NSG90H60SRAA" in by_sub["gas_oven"] and "NX60A6711SSAA" in by_sub["gas_oven"]
    assert "NSE80H63XRAA" in by_sub["radiant"] and "NZ30K7570RSAA" in by_sub["radiant"]  # electric range + cooktop
    assert {"NA30N6555TSAA", "NA30R5310FSAA"} <= by_sub["gas_cooktop"] and not by_sub["gas_cooktop"] & by_sub["gas_oven"]
    assert "NSE80H63XRAA" not in by_sub["gas_oven"] and "NSI6DG9550SRAA" in by_sub["induction"]


def test_search_candidates_carry_major_and_sub():
    c = s.parse_search(json.loads(_load("pf_search_laundry.json")), "dryer")
    assert all(x.category == "washer" and x.subcategory == "dryer" and "/laundry/dryers/" in x.url for x in c)
    c = s.parse_search(json.loads(_load("pf_search_cooking.json")), "induction")
    assert {x.model_number for x in c} >= {"NSI6DG9300SRAA", "NZ30K7880USAA"}  # '/' stripped from model codes
    assert all(x.category == "cooking" and x.subcategory == "induction" for x in c)


def test_classify_edge_cases():
    assert s.classify("laundry/washers", "Bespoke AI Front Load Washer", "08010104") == "front_load"
    assert s.classify("laundry/washers", "Top Load Washer", "08010103") == "top_load"
    assert s.classify("laundry/washers", "Compact Front Load Washer", "08010102") == "front_load"
    assert s.classify("laundry/washers", "Washer", "") is None
    assert s.classify("laundry/washer-and-dryer-sets", "Set", "", "F-1695113026532") is None
    assert s.classify("laundry/washer-and-dryer-sets", "Laundry Hub", "08010204", "WH46DBH100GWA3") == "laundry_center"
    assert s.classify("cooking-appliances/ranges", "Slide-in Gas Range", "08080201") == "gas_oven"
    assert s.classify("cooking-appliances/ranges", "Freestanding Range", "08080201") == "gas_oven"  # by leaf code
    assert s.classify("cooking-appliances/ranges", "Slide-in Dual Fuel Range", "") == "gas_oven"
    assert s.classify("cooking-appliances/ranges", "Freestanding Electric Range", "08080202") == "radiant"
    assert s.classify("cooking-appliances/ranges", "Electric Range", "") == "radiant"
    assert s.classify("cooking-appliances/ranges", "Induction Range", "08080202") == "induction"  # induction beats electric
    assert s.classify("cooking-appliances/wall-ovens", "Bespoke Combi Wall Oven", "08080103") == "sco"
    assert s.classify("cooking-appliances/wall-ovens", "Microwave Combination Wall Oven", "") == "sco"
    assert s.classify("cooking-appliances/wall-ovens", "Double Wall Oven", "08080102") == "electric_oven"
    assert s.classify("cooking-appliances/ranges", "Slide-in Induction Range", "08080204") == "induction"
    assert s.classify("cooking-appliances/cooktops", "Gas Cooktop", "08080301") == "gas_cooktop"
    assert s.classify("cooking-appliances/cooktops", "Gas Cooktop 30in", "") == "gas_cooktop"  # by name
    assert s.classify("cooking-appliances/range-hoods", "Hood", "08080401") is None
    assert s.classify("refrigerators/one-door", "Upright", "") is None
    assert s.classify("dishwashers/built-in", "Dishwasher") is None


def test_discover_unsupported_sub_keys_raise():
    for sub in ("bottom_freezer", "built_in", "compact", "scr", "refrigerator", "bogus"):
        try:
            s.discover(sub, 5)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {sub}")


def _discover_with(tag, sub, limit):
    payload = json.loads(_load(f"pf_search_{tag}.json"))
    posted = []

    class R:
        def raise_for_status(self): pass
        def json(self): return payload

    real_post, real_sleep = s.requests.post, s.time.sleep
    s.requests.post = lambda *a, **k: (posted.append(k["data"]["category_code"]), R())[1]
    s.time.sleep = lambda _: None
    s._clear_page_cache()
    try:
        return s.discover(sub, limit), posted
    finally:
        s.requests.post, s.time.sleep = real_post, real_sleep
        s._clear_page_cache()


def test_discover_uses_category_code_and_classifies():
    got, posted = _discover_with("laundry", "dryer", 30)
    assert posted == ["08010000"] and len(got) == 15
    assert all((c.category, c.subcategory) == ("washer", "dryer") for c in got)
    got, posted = _discover_with("cooking", "electric_oven", 4)
    assert posted == ["08080000"] and len(got) == 4 and all(c.subcategory == "electric_oven" for c in got)
    got, posted = _discover_with("microwave", "otr", 30)
    assert posted == ["08110000"] and len(got) == 9
    got, posted = _discover_with("fridge", "side_by_side", 30)
    assert posted == ["08030000"] and len(got) == 6 and {c.subcategory for c in got} == {"side_by_side"}


def _rec(tag):
    nd = s.parse_next_data(_load(f"pdp_{tag}.html"))
    rows, spec_url, supports = s.parse_bridge(json.loads(_load(f"bridge_{tag}.json")), nd["product"]["modelCode"])
    return s.build_record(f"https://www.samsung.com/us/x/{tag}/", nd, rows, spec_url, supports)[0]


def test_fridge_records_have_category_and_full_spec_table():
    p, _ = _build("fd", "https://www.samsung.com/us/refrigerators/french-door/x/")
    assert (p.category, p.subcategory) == ("refrigerator", "french_door")
    assert p.extra_specs and all(" > " in k for k in p.extra_specs)  # full table only (no curated fridge labels)
    p, _ = _build("tf", "https://www.samsung.com/us/refrigerators/top-freezer/x/")
    assert (p.category, p.subcategory) == ("refrigerator", "top_freezer")


def test_front_load_washer_record():
    p = _rec("wf")
    assert (p.category, p.subcategory, p.model_number) == ("washer", "front_load", "WF53BB8700AVUS")
    assert p.capacity_total_cuft == 5.3 and p.energy_kwh_year == 103.0 and p.energy_star is True
    assert (p.width_in, p.height_in, p.depth_in) == (27.0, 38.75, 34.5)
    assert p.weight_lb == 224.9  # 'Product Weight (lbs.)', not 'Net Weight = 102 lb' (kg mislabelled)
    assert (p.voltage_v, p.amps, p.frequency_hz) == ("120", 15.0, 60.0)
    assert p.wifi_supported is True and "Wi-Fi" in p.wifi_evidence
    # fridge-only fields stay None for laundry
    assert (p.door_style, p.capacity_fridge_cuft, p.capacity_freezer_cuft, p.ice_maker, p.water_dispenser) == (None,) * 5
    x = p.extra_specs
    assert x["Max spin speed (rpm)"] == "1200" and x["Preset wash cycles"] == "24" and x["Steam"] == "Yes"
    assert x["Spin speed settings"] == "7" and x["Motor"] == "Digital Inverter Motor"
    assert x["AI features"] == "AI Pattern; AI Smart Dial; Bespoke AI"
    assert "Burners/elements" not in x and all(v for v in x.values())
    assert "5.3 cu.ft." in p.pod_features and "Total Capacity (cu. ft.)" not in p.pod_features


def test_top_load_washer_missing_voltage_is_none():
    p = _rec("wa")
    assert (p.category, p.subcategory) == ("washer", "top_load")
    assert p.capacity_total_cuft == 5.5 and p.weight_lb == 134.5 and p.amps == 15.0
    assert p.voltage_v is None and p.frequency_hz is None  # not published -> None, never invented
    assert p.extra_specs["Max spin speed (rpm)"] == "750" and "Steam" not in p.extra_specs


def test_dryer_record():
    p = _rec("dv")
    assert (p.category, p.subcategory) == ("washer", "dryer")
    assert p.capacity_total_cuft == 7.5 and p.energy_kwh_year == 644.0 and p.weight_lb == 123.0
    assert (p.voltage_v, p.amps, p.frequency_hz) == ("240", 30.0, 60.0)  # largest of 120V / 240V
    assert p.wifi_supported is None
    x = p.extra_specs
    assert x["Dryer capacity (cu ft)"] == "7.5" and x["Preset drying cycles"] == "10" and x["Sensor dry"] == "Yes"
    assert "Washer capacity (cu ft)" not in x and "Max spin speed (rpm)" not in x


def test_laundry_hub_record_has_both_capacities():
    p = _rec("hub")
    assert (p.category, p.subcategory) == ("washer", "laundry_center")
    assert p.capacity_total_cuft == 4.6 and p.height_in == 74.44 and p.weight_lb == 319.7
    assert p.voltage_v == "240" and p.amps == 30.0
    assert p.extra_specs["Washer capacity (cu ft)"] == "4.6" and p.extra_specs["Dryer capacity (cu ft)"] == "7.6"
    assert p.extra_specs["Power source / dryer type"] == "Gas"


def test_electric_range_record():
    p = _rec("rng")
    assert (p.category, p.subcategory) == ("cooking", "radiant") and p.capacity_total_cuft == 6.3
    assert (p.voltage_v, p.amps, p.frequency_hz) == ("240", 40.0, 60.0)
    assert p.weight_lb == 157.2 and p.wifi_supported is True and p.energy_kwh_year is None
    x = p.extra_specs
    assert x["Burners/elements"] == "5" and x["Convection"] == "Yes" and x["Air fry"] == "Yes"
    assert x["Oven capacity (cu ft)"] == "6.3" and x["Bake element (W)"] == "3000" and x["Configuration"] == "Slide-In"
    assert 'Left Front: 6"/9" 3,000W' in x["Burner/element detail"]
    assert "Cavity capacity (cu ft)" not in x and p.door_style is None


def test_gas_range_burners_and_induction_range():
    g = _rec("gas")
    assert (g.subcategory, g.voltage_v, g.amps) == ("gas_oven", "120", 20.0)
    assert g.extra_specs["Fuel type"] == "Gas" and g.extra_specs["Burners/elements"] == "5"
    assert "Center: 10K BTU" in g.extra_specs["Burner/element detail"]
    i = _rec("ind")
    assert (i.subcategory, i.extra_specs["Fuel type"], i.extra_specs["Burners/elements"]) == ("induction", "Induction", "4")


def test_microwave_and_otr_records():
    o = _rec("otr")
    assert (o.category, o.subcategory) == ("cooking", "otr") and o.capacity_total_cuft == 2.1
    assert (o.voltage_v, o.amps) == ("120", 15.0)
    x = o.extra_specs
    assert x["Cavity capacity (cu ft)"] == "2.1" and x["Vent power (CFM)"] == "400" and x["Microwave output (W)"] == "1000"
    assert "Oven capacity (cu ft)" not in x and "Burners/elements" not in x and "Convection" not in x
    m = _rec("mw")
    assert m.subcategory == "microwave" and m.capacity_total_cuft == 1.9 and m.wifi_supported is None
    assert m.extra_specs["Microwave output (W)"] == "950"


def test_wall_oven_and_radiant_cooktop_records():
    w = _rec("wo")
    assert (w.subcategory, w.capacity_total_cuft) == ("electric_oven", 5.1)
    assert w.weight_lb is None and w.voltage_v is None  # silent in data
    assert w.extra_specs["Convection"] == "Yes" and w.extra_specs["Air fry"] == "Yes"
    assert "Burners/elements" not in w.extra_specs
    c = _rec("ct")
    assert (c.subcategory, c.capacity_total_cuft, c.weight_lb) == ("radiant", None, 35.7)
    assert c.wifi_supported is True and c.extra_specs["Burners/elements"] == "5"
    assert "Oven capacity (cu ft)" not in c.extra_specs and "Convection" not in c.extra_specs


def test_unsupported_product_group_raises():
    nd = s.parse_next_data(_load("pdp_wf.html").replace("home-appliances/laundry/washers/bespoke",
                                                         "home-appliances/vacuum-cleaners/robot/x"))
    try:
        s.build_record("https://www.samsung.com/us/x/", nd, [], None, [])
    except s.SamsungPageError:
        return
    raise AssertionError("expected SamsungPageError")


def test_scrape_sets_category_and_keeps_full_raw_spec_table():
    nd = s.parse_next_data(_load("pdp_rng.html"))
    bridge = s.parse_bridge(json.loads(_load("bridge_rng.json")), nd["product"]["modelCode"])
    real = (s._load_html, s.fetch_bridge, s.download_pdf)
    s._load_html = lambda url: _load("pdp_rng.html")
    s.fetch_bridge = lambda gid, code: bridge
    s.download_pdf = lambda *a, **k: None
    try:
        p, docs, raw = s.scrape("https://www.samsung.com/us/cooking-appliances/ranges/x/")
    finally:
        s._load_html, s.fetch_bridge, s.download_pdf = real
    assert (p.category, p.subcategory) == ("cooking", "radiant") and docs == []
    assert len(raw) == len(bridge[0]) > 50 and {r.source for r in raw} == {"web"}
    assert any(r.key == "Air Fry" for r in raw)


def test_scrape_rejects_non_samsung_url_before_any_request():
    real = s.requests.get
    s.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("request made"))
    try:
        s.scrape("https://evil.io/us/laundry/washers/x/")
    except s.SamsungPageError:
        return
    finally:
        s.requests.get = real
    raise AssertionError("expected SamsungPageError")


def _fake_post(payload, calls):
    class R:
        def raise_for_status(self): pass
        def json(self): return payload

    return lambda *a, **k: (calls.append((k["data"]["category_code"], k["data"]["startIndex"])), R())[1]


def test_discover_caches_pf_search_pages_across_fridge_sub_keys():
    payload = {**json.loads(_load("pf_search_fridge.json")), "hasMoreResults": False}
    calls = []
    s._clear_page_cache()
    real_post, real_sleep = s.requests.post, s.time.sleep
    s.requests.post, s.time.sleep = _fake_post(payload, calls), (lambda _: None)
    try:
        per_sub = {sub: {c.model_number for c in s.discover(sub, 30)}
                   for sub in ("french_door", "side_by_side", "top_freezer")}
    finally:
        s.requests.post, s.time.sleep = real_post, real_sleep
        s._clear_page_cache()
    assert calls == [("08030000", 0)]  # one request, not three
    assert not (per_sub["french_door"] & per_sub["side_by_side"]) and not (per_sub["side_by_side"] & per_sub["top_freezer"])
    assert not (per_sub["french_door"] & per_sub["top_freezer"])


def test_page_cache_expires():
    calls = []
    s._clear_page_cache()
    real_post, real_mono = s.requests.post, s.time.monotonic
    s.requests.post = _fake_post({"searchResults": [], "hasMoreResults": False}, calls)
    now = [1000.0]
    s.time.monotonic = lambda: now[0]
    try:
        s._search_page("08030000", 0)
        s._search_page("08030000", 0)
        now[0] += s.PAGE_CACHE_TTL_S + 1
        s._search_page("08030000", 0)
    finally:
        s.requests.post, s.time.monotonic = real_post, real_mono
        s._clear_page_cache()
    assert len(calls) == 2


def test_discover_logs_unclassified_and_other_sub_counts():
    import io
    from contextlib import redirect_stderr
    payload = {"searchResults": [
        {"modelCode": "A1", "pdpURL": "/us/refrigerators/french-door/a/", "productDisplayName": "FD"},
        {"modelCode": "B2", "pdpURL": "/us/refrigerators/side-by-side/b/", "productDisplayName": "SxS"},
        {"modelCode": "C3", "pdpURL": "/us/refrigerators/mystery-type/c/", "productDisplayName": "?"},
        {"modelCode": "D4", "pdpURL": "https://evil.io/x/", "productDisplayName": "bad"}], "hasMoreResults": False}
    s._clear_page_cache()
    real_post, real_sleep = s.requests.post, s.time.sleep
    s.requests.post, s.time.sleep = _fake_post(payload, []), (lambda _: None)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            got = s.discover("french_door", 30)
    finally:
        s.requests.post, s.time.sleep = real_post, real_sleep
        s._clear_page_cache()
    assert [c.model_number for c in got] == ["A1"]
    msg = err.getvalue()
    assert "unclassified 1" in msg and "other sub keys 1" in msg and "off-domain 1" in msg


def test_classify_is_a_function_of_one_product_so_subs_partition():
    payload = json.loads(_load("pf_search_fridge.json"))
    seen = {}
    for sub in ("french_door", "side_by_side", "top_freezer"):
        for c in s.parse_search(payload, sub):
            seen.setdefault(c.model_number, set()).add(sub)
    assert seen and all(len(v) == 1 for v in seen.values())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
