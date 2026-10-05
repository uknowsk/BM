"""Plain-assert offline tests (saved fixtures). Run: python tests/test_bosch_us.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bosch_us as b

FX = Path(__file__).parent / "fixtures" / "bosch_us"
PDP = "https://www.bosch-home.com/us/en/product/"
URLS = {
    "B36CT80SNS": PDP + "refrigerators/fridge-freezers/freestanding/B36CT80SNS",
    "WGB24600UC": PDP + "washers-and-dryers/compact-washers/compact-washers/WGB24600UC",
    "HGS8055UC": PDP + "cooking-baking/ranges/gas-ranges/HGS8055UC",
    "HMV8045U": PDP + "cooking-baking/microwaves/over-the-range-microwaves/HMV8045U",
}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(m):
    model, root = b.model_from_url(URLS[m])
    flight = b.flight_text(_t(f"pdp_{m}.html"))
    rec, raw = b.parse_product(model, URLS[m], root, flight)
    return rec, raw, flight


def test_full_spec_table_and_image_for_every_fixture():
    imgs = {"B36CT80SNS": "MCSA02714469_i8483_2058246_B36CT80SNS_PAA_straight_front_closed_def.webp",
            "HGS8055UC": "17703228_HGS8055UC_Front-Facing-NO-CROP_def.webp",
            "HMV8045U": "23562043_HMV8045U-Bosch-microwave-black_stainless_steel-front_def.webp",
            "WGB24600UC": "22108889_WGB24600UC_STP_def.webp"}
    for m, img in imgs.items():
        rec, raw, flight = _parse(m)
        x = rec.extra_specs
        assert all(f"{b._noise(r.section)} > {b._noise(r.key)}" in x for r in raw), m  # every row incl. the typed-field ones
        assert len(x) >= len(raw)
        assert rec.image_url == "https://media3.bsh-group.com/Product_Shots/" + img, m
    rec, raw, _ = _parse("HGS8055UC")
    assert rec.extra_specs["General > Number of Gas Burners"] == "5" and "Number of Gas Burners" in rec.extra_specs


def test_main_image_and_noise_rules():
    f = b.main_image_url
    ld = lambda img: 'x,"productJsonLd":{"@type":"Product","image":' + img + "}"
    assert f("nothing") is None and f(ld("[]")) is None and f(ld("null")) is None
    assert f(ld('["https://evil.example.com/a.webp"]')) is None and f(ld('["http://media3.bsh-group.com/a.webp"]')) is None
    assert f(ld('["//media3.bsh-group.com/a.webp"]')) == "https://media3.bsh-group.com/a.webp"
    assert f(ld('"https://media3.bsh-group.com/a.webp"')) == "https://media3.bsh-group.com/a.webp"
    assert b._noise("A" + chr(0xAE) + chr(10) + "B" + chr(0x2122)) == "A | B"
    secs = [{"name": "S", "specifications": [{"name": {"text": "K"}, "value": {"text": "a" + chr(10) + "b"}, "unit": "in"}]}]
    assert b.full_spec_table(secs) == {"S > K": "a | b in"}


def test_supported_subcategories_only_real_ones():
    assert b.SUPPORTED_SUBCATEGORIES == set(b.SUB_SOURCES)
    assert not {"side_by_side", "top_freezer", "compact", "top_load", "laundry_center", "scr"} & b.SUPPORTED_SUBCATEGORIES
    assert {"sco", "gas_oven", "radiant", "induction", "electric_oven", "microwave", "otr"} <= b.SUPPORTED_SUBCATEGORIES


def test_discover_rejects_unknown_sub():
    try:
        b.discover("top_load")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_listing_parse_and_candidates():
    items, total = b.parse_listing(_t("listing_french_door_p1.html"), "refrigerators")
    assert total == 13 and len(items) == 12
    items2, _ = b.parse_listing(_t("listing_french_door_p2.html"), "refrigerators")
    cands = [b.item_to_candidate(i, "refrigerator", "french_door", "refrigerators") for i in items + items2]
    codes = [c.model_number for c in cands if c]
    assert len(codes) == len(set(codes)) == 13 and "B36CT80SNS" in codes
    c = next(c for c in cands if c.model_number == "B36CT80SNS")
    assert (c.brand, c.category, c.subcategory, c.price_usd) == ("Bosch", "refrigerator", "french_door", 3999.0)
    assert c.url == URLS["B36CT80SNS"] and c.name.startswith("800 Series French Door") and "36 in" in c.name


def test_candidate_filters():
    ok = {"productCode": "X123", "urlPath": "/product/cooking-baking/ranges/gas-ranges/X123", "productName": ["a"]}
    assert b.item_to_candidate(ok, "cooking", "gas_oven", "cooking-baking").price_usd is None
    for bad in ({**ok, "urlPath": "/product/cooking-baking/cooking-and-baking-accessories/knobs/X123"},
                {**ok, "urlPath": "/product/refrigerators/x/X123"},          # other root
                {**ok, "urlPath": "//evil.com/product/cooking-baking/x"},
                {**ok, "productCode": "../../etc"}, {**ok, "productCode": None}):
        assert b.item_to_candidate(bad, "cooking", "gas_oven", "cooking-baking") is None


def test_missing_markers_raise():
    for fn in (lambda: b.flight_text("<html>nothing</html>"),
               lambda: b.parse_listing('<script>self.__next_f.push([1,"{}"])</script>', "x"),
               lambda: b.parse_product("M1", URLS["B36CT80SNS"], "refrigerators",
                                       '"specifications":[{"name":"G","specifications":[]}]')):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_fridge_record():
    r, raw, _ = _parse("B36CT80SNS")
    assert (r.category, r.subcategory, r.door_style) == ("refrigerator", "french_door", "French Door Bottom Mount")
    assert r.price_usd == 3999.0 and r.finish_color == "Stainless Steel"
    assert (r.capacity_total_cuft, r.capacity_fridge_cuft, r.capacity_freezer_cuft) == (20.8, 14.8, 6.1)
    assert (r.height_in, r.width_in, r.depth_in) == (72.0, 35.625, 27 + 13 / 16)
    assert r.weight_lb == 357.0 and "gross" in r.extra_specs["Weight basis"]  # web only has gross weight
    assert r.energy_star is True and r.ice_maker is True and r.wifi_supported is True
    assert "Access Recipes" in r.wifi_evidence and r.pod_features
    assert r.energy_kwh_year is None and r.water_dispenser is None  # PDF-only
    assert "Sabbath Mode" in r.extra_specs and "Ice Maker" not in r.extra_specs
    assert any(x.key == "Overall Appliance Dimensions (HxWxD) (in)" and x.source == "web" for x in raw)


def test_washer_record_has_no_fridge_fields():
    r, raw, _ = _parse("WGB24600UC")
    assert (r.category, r.subcategory) == ("washer", "front_load")
    assert r.price_usd == 1599.0 and r.finish_color == "White"
    assert (r.height_in, r.width_in, r.depth_in, r.weight_lb) == (33.25, 23.5, 25.5, 182.0)
    assert r.energy_kwh_year == 105.0 and r.energy_star is True and r.wifi_supported is True
    assert r.capacity_total_cuft is None and r.door_style is None and r.ice_maker is None
    assert r.extra_specs["Capacity"] == "2.4 Cu Ft" and r.extra_specs["Maximum spin speed"] == "1600 rpm"
    assert r.extra_specs["i-Dos"] == "No" and "Weight basis" not in r.extra_specs
    assert len(raw) > 30


def test_cooking_record():
    r, _, _ = _parse("HGS8055UC")
    assert (r.category, r.subcategory) == ("cooking", "gas_oven")
    assert r.voltage_v == "120" and r.wifi_supported is False and r.energy_star is None
    assert (r.height_in, r.width_in, r.depth_in, r.weight_lb) == (36.0, 29 + 15 / 16, 24 + 15 / 16, None)
    assert r.extra_specs["Number of Gas Burners"] == "5" and r.extra_specs["Cavity Capacity"] == "3.6 Cu Ft"
    assert _parse("HMV8045U")[0].subcategory == "otr"


def test_pick_docs_dedupes_and_prefers_non_pregenerated_sheet():
    d = b._json_after(_parse("WGB24600UC")[2], '"technicalDocuments":[')
    picked = dict(b.pick_docs(d))
    assert picked["SpecSheet"].endswith("24375664_WGB24600UC_Spec_Sheet.pdf")
    assert picked["EnergyGuide"].endswith(".pdf") and picked["Manual"].endswith("9001852660_N.pdf")
    assert "Installation" not in picked or picked["Installation"] != picked["Manual"]  # same PDF listed once
    assert len(set(picked.values())) == len(picked)
    d.append({"titleKey": "user-manuals", "url": "https://evil.example.com/x.pdf"})
    d.insert(0, {"titleKey": "product-specification", "url": "http://media3.bsh-group.com/a.pdf"})
    assert all("evil" not in u and u.startswith("https://") for _, u in b.pick_docs(d))
    fr = b._json_after(_parse("B36CT80SNS")[2], '"technicalDocuments":[')
    assert dict(b.pick_docs(fr))["SpecSheet"].endswith("MCDOC03224079_B36CT80SNS.pdf")


def test_spec_sheet_and_energy_guide_parsing():
    s = b.parse_spec_sheet(_t("spec_B36CT80SNS.txt"))
    assert (s["voltage_v"], s["amps"], s["frequency_hz"], s["weight_lb"]) == ("115", 15.0, 60.0, 350.0)
    assert s["water_dispenser"] is True and s["energy_kwh_year"] == 570.0
    w = b.parse_spec_sheet(_t("spec_WGB24600UC.txt"))
    assert (w["voltage_v"], w["amps"], w["weight_lb"]) == ("208/220-240", 10.5, 182.0)
    c = b.parse_spec_sheet(_t("spec_HGS8055UC.txt"))
    assert c["voltage_v"] == "120" and c["amps"] is None and c["weight_lb"] == 193.0
    assert b.parse_energy_guide_kwh(_t("energy_guide_B36CT80SNS.txt")) == 570.0
    assert b.parse_energy_guide_kwh("nothing") is None


def test_model_from_url_validation():
    assert b.model_from_url(URLS["B36CT80SNS"]) == ("B36CT80SNS", "refrigerators")
    for bad in ("http://www.bosch-home.com/us/en/product/refrigerators/a/B36CT80SNS",
                "https://evil.com/us/en/product/refrigerators/a/B36CT80SNS",
                "https://www.bosch-home.com.evil.com/us/en/product/refrigerators/a/B36CT80SNS",
                PDP + "dishwashers/front-controls/SHPM88Z75N", PDP + "refrigerators/a/..%2f.."):
        try:
            b.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_browser_modes_read_env_each_call():
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for val, want in (("headless", [True]), ("visible", [False]), ("auto", [True, False]), ("", [True, False])):
            os.environ["FRIDGE_BROWSER_MODE"] = val
            assert b._modes() == want
    finally:
        if old is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_host_checks():
    assert b._host_in("https://media3.bsh-group.com/Documents/a.pdf", b.PDF_HOSTS)
    assert not b._host_in("https://bosch-home.com.evil.com/a.pdf", b.PDF_HOSTS)
    b._check_final_url("https://www.bosch-home.com/us/en")
    for bad in ("https://evil.com/", "http://www.bosch-home.com/"):
        try:
            b._check_final_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def _item(code, path, kind, size="36_IN"):
    return {"productCode": code, "urlPath": path, "productName": ["800 Series", kind, size, "Steel", ""],
            "price": {"amount": 1000}}


_OVERLAP = [
    _item("B36BD", "/product/refrigerators/built-in/french-door/B36BD", "Built-in French Door Bottom Mount Refrigerator"),
    _item("B36FD", "/product/refrigerators/fridge-freezers/freestanding/B36FD", "French Door Bottom Mount Refrigerator"),
    _item("B24BF", "/product/refrigerators/fridge-freezers/freestanding/B24BF", "Bottom Freezer Refrigerator", "24_IN"),
    _item("B30BIB", "/product/refrigerators/bottom-freezer/built-in/B30BIB", "Built-in Bottom Freezer Refrigerator"),
]


def test_classify_fridge_built_in_beats_french_door():
    c = b.classify_fridge
    assert c("/product/refrigerators/built-in/french-door/X", "Built-in French Door Bottom Mount Refrigerator") == "built_in"
    assert c("/product/refrigerators/a/X", "Built-in French Door Refrigerator") == "built_in"
    assert c("/product/refrigerators/a/built-in/X", "French Door Refrigerator") == "built_in"
    assert c("/product/refrigerators/a/X", "French Door Bottom Mount Refrigerator") == "french_door"
    assert c("/product/refrigerators/a/X", "Bottom Freezer Refrigerator") == "bottom_freezer"
    assert c("/product/refrigerators/a/X", "Side by side") is None


def test_discover_never_files_a_model_under_two_sub_keys():
    from contextlib import contextmanager
    got_by_sub = {}
    orig = (b._session, b.parse_listing, b.time.sleep)

    class S:
        def html(self, url): return url

    @contextmanager
    def fake_session(url):
        yield S()

    b._session = fake_session
    b.parse_listing = lambda html, root: (list(_OVERLAP), len(_OVERLAP))  # every category page lists everything
    b.time.sleep = lambda _: None
    try:
        for sub in ("french_door", "bottom_freezer", "built_in"):
            got_by_sub[sub] = {c.model_number for c in b.discover(sub, limit=50)}
    finally:
        b._session, b.parse_listing, b.time.sleep = orig
    assert got_by_sub == {"french_door": {"B36FD"}, "bottom_freezer": {"B24BF"}, "built_in": {"B36BD", "B30BIB"}}
    for sub, models in got_by_sub.items():  # scrape's rule agrees for every discovered model
        for it in _OVERLAP:
            if it["productCode"] in models:
                assert b.classify_fridge(it["urlPath"], " ".join(it["productName"])) == sub


def test_cooking_sub_keys_are_exclusive_and_match_scrape_rule():
    items = json.loads((FX / "listing_cooking_items.json").read_text(encoding="utf-8"))
    cooking_subs = [k for k, (m, _) in b.SUB_SOURCES.items() if m == "cooking"]
    got = {}
    for it in items:  # every category page lists everything: a model must still land under exactly one sub key
        for sub in cooking_subs:
            c = b.item_to_candidate(it, "cooking", sub, "cooking-baking")
            if c:
                assert it["productCode"] not in got, (it["productCode"], got[it["productCode"]], sub)
                got[it["productCode"]] = sub
                assert b.classify_cooking(it["urlPath"], " ".join(it["productName"])) == sub  # scrape's rule
    want = {"HGS8657UC": "gas_oven", "HDS5057U": "gas_oven", "HGI8056UC": "gas_oven",
            "HIS5057U": "induction", "HEF3050MU": "radiant", "HEI8056U": "radiant",
            "HBLP752UC": "sco", "HBL8753UC": "sco", "HMC80252UC": "sco", "HMC54151UC": "sco",  # combination / speed ovens
            "HBLP651UC": "electric_oven", "HBL8454UC": "electric_oven",
            "HMB50152UC": "microwave", "HMD8054UC": "microwave", "NET8062UC": "radiant", "NEM5066UC": "radiant"}
    assert got == want


def test_classify_cooking_rules():
    c = b.classify_cooking
    p = "/product/cooking-baking/"
    assert c(p + "wall-ovens/doubleovens/X", "800 Series Combination Oven 30''") == "sco"
    assert c(p + "wall-ovens/speed-ovens/X", "500 Series 24''") == "sco"
    assert c(p + "wall-ovens/doubleovens/X", "800 Series Double Wall Oven 30''") == "electric_oven"
    assert c(p + "microwaves/over-the-range-microwaves/X", "Over-the-range microwave") == "otr"
    assert c(p + "microwaves/built-in-microwaves/X", "Built-In Microwave Oven") == "microwave"
    assert c(p + "ranges/gas-ranges/X", "Gas Slide-in Range") == "gas_oven"
    assert c(p + "ranges/dual-fuel-ranges/X", "Dual Fuel Range") == "gas_oven"
    assert c(p + "ranges/electric-slide-in-ranges/X", "Electric Slide-in Range") == "radiant"
    assert c(p + "ranges/induction-ranges/X", "Induction Range") == "induction"
    assert c(p + "induction-electric-cooktops/electric-cooktops/X", "Electric Cooktop") == "radiant"
    assert c(p + "induction-electric-cooktops/induction-cooktops/X", "Induction Cooktop") == "induction"
    assert c(p + "cooking-and-baking-accessories/x/X", "Knob") is None


def test_title_json_decoding_handles_escaped_quotes():
    flight = 'x"title":{"valueClass":"800 Series","headline":"36\\" French Door \\"Pro\\" Refrigerator"},"y":1'
    assert b._title(flight) == ("800 Series", '36" French Door "Pro" Refrigerator')
    try:
        b._title('"nothing":1')
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")

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
    PwError, Good, Bad = _fake_browsers(b.BASE)
    seq = [PwError("launch failed"), Bad(), Good()]

    def launch(*a, **k):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    names = [(b.common, 'launch_browser')]
    orig = [(o, n, getattr(o, n)) for o, n in names]
    for o, n in names:
        setattr(o, n, launch)
    patches = [(b, "_modes", lambda: [True, True, False]), (b, "_dismiss_consent", lambda page: None),
               (b.common, "looks_blocked", lambda *a: False)]
    orig += [(o, n, getattr(o, n)) for o, n, _ in patches]
    for o, n, v in patches:
        setattr(o, n, v)
    try:
        browser, page = b._connect(None, b.BASE + "/category/x")
    finally:
        for o, n, v in orig:
            setattr(o, n, v)
    assert isinstance(browser, Good) and not seq


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
