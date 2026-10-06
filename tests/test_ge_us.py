"""Plain-assert offline tests (saved fixtures). Run: python tests/test_ge_us.py  (or pytest)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import ge_us as ge

FX = Path(__file__).parent / "fixtures" / "ge"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _po(name):
    return _j(f"po_{name}.json")


def _rec(name):
    po = _po(name)
    return ge.parse_product(f"{ge.BASE}/appliance/Thing-{po['sku']}", po)


def test_supported_subcategories_are_catalog_keys():
    assert ge.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys())
    assert "built_in" not in ge.SUPPORTED_SUBCATEGORIES and "scr" not in ge.SUPPORTED_SUBCATEGORIES
    assert {"sco", "gas_oven", "gas_cooktop", "radiant", "induction", "electric_oven", "microwave", "otr"} <= ge.SUPPORTED_SUBCATEGORIES
    for sub, (major, _) in ge.SUB_SOURCES.items():
        assert catalog.major_of(sub) == major


def test_parse_search_candidates():
    pairs = ge.parse_search(_j("ss_french_door.json"), "french_door")
    c = pairs[0][0]
    assert (c.brand, c.category, c.subcategory) == ("GE", "refrigerator", "french_door")
    assert c.model_number == "PVD28HYYFS" and c.price_usd == 3499.0
    assert c.url.startswith("https://www.geappliances.com/appliance/")
    assert "™" in c.name or "Profile" in c.name


def test_parse_search_drops_offdomain_and_accessories():
    data = _j("ss_washer.json")
    r0 = dict(data["results"][0], sku="EVIL1", custom_url="https://evil.example.com/appliance/x-EVIL1")
    r1 = dict(data["results"][0], sku="ACC1", item_commercial_category2="Washer Accessories")
    r2 = dict(data["results"][0], sku="NOPRICE", price=None)
    data["results"] = [r0, r1, r2]
    got = ge.parse_search(data, "front_load")
    assert [c.model_number for c, _ in got] == ["NOPRICE"] and got[0][0].price_usd is None
    try:
        ge.parse_search({"nope": 1}, "front_load")
    except ge.GEPageError:
        return
    raise AssertionError("expected GEPageError")


def test_discover_dedupes_filters_and_limits(monkeypatch=None):
    fd = _j("ss_french_door.json")
    bf = json.loads(json.dumps(fd))
    # bottom-freezer listing re-lists the French-door model plus a non-French model
    plain = dict(fd["results"][0], sku="PLAIN1", name="GE 20 Cu. Ft. Bottom Freezer Refrigerator",
                 spec_features_configuration="Bottom Freezer", custom_url="/appliance/GE-Plain-PLAIN1",
                 categories_hierarchy=["GE Appliances&gt;Kitchen&gt;Refrigerators&gt;Bottom Freezer Refrigerators"])
    bf["results"] = [fd["results"][0], plain]
    pages = {ge.SUB_SOURCES["french_door"][1][0][0]: fd, ge.SUB_SOURCES["french_door"][1][1][0]: bf}
    orig_page, orig_sleep = ge._search_page, ge.time.sleep
    ge._search_page = lambda path, n: pages[path]
    ge.time.sleep = lambda s: None
    try:
        got = ge.discover("french_door", limit=50)
        models = [c.model_number for c in got]
        assert len(models) == len(set(models)) and "PLAIN1" not in models  # French predicate excludes plain one
        assert len(ge.discover("french_door", limit=2)) == 2
        pages[ge.SUB_SOURCES["bottom_freezer"][1][0][0]] = bf
        bottom = [c.model_number for c in ge.discover("bottom_freezer", limit=50)]
        assert bottom == ["PLAIN1"]
        try:
            ge.discover("built_in")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    finally:
        ge._search_page, ge.time.sleep = orig_page, orig_sleep


def test_french_door_fixture_html_record():
    po = ge.extract_product((FX / "pdp_french_door.html").read_text(encoding="utf-8"))
    r, raw, docs = ge.parse_product("https://www.geappliances.com/appliance/X-PVD28HYYFS", po)
    assert (r.category, r.subcategory, r.door_style) == ("refrigerator", "french_door", "French Door")
    assert r.price_usd == 3499.0 and r.finish_color == "Fingerprint Resistant Stainless Steel"
    assert (r.capacity_total_cuft, r.capacity_fridge_cuft, r.capacity_freezer_cuft) == (27.89, 15.73, 8.55)
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (35.75, 69.875, 36.75, 388.0)
    assert (r.voltage_v, r.amps, r.frequency_hz, r.energy_kwh_year) == ("120", 15.0, 60.0, 758.0)
    assert r.energy_star is True and r.ice_maker is True and r.water_dispenser is True
    assert r.wifi_supported is True and "WiFi Connect = Built-In" in r.wifi_evidence
    assert "Scan-to-List" in r.pod_features and len(r.pod_features) == len(set(r.pod_features))
    assert r.extra_specs["Defrost type"] == "Frost Guard"
    assert r.product_name.startswith("GE Profile") and "|^|" not in r.product_name
    assert any(x.key == "Net Weight" and x.value == "388 lb" and x.section == "Weights & Dimensions" for x in raw)
    assert any(x.key == "Dispenser" and x.value.startswith("External Water; Cubes") for x in raw)  # _n merged
    assert [t for t, _ in docs] == ["EnergyGuide", "Manual", "QuickSpecs", "Installation"]
    assert all(u.startswith("https://images.salsify.com/") for _, u in docs)


def test_top_freezer_flags():
    r, _, _ = _rec("fridge_top")
    assert r.subcategory == "top_freezer" and r.door_style == "Top Freezer"
    assert r.ice_maker is False  # "Optional (IM4D Ready)" = not included
    assert r.water_dispenser is None and r.wifi_supported is None and r.wifi_evidence is None
    assert r.energy_star is False and r.energy_kwh_year == 451.0


def test_washer_extra_specs():
    r, raw, _ = _rec("washer")
    assert (r.category, r.subcategory) == ("washer", "front_load")
    assert r.ice_maker is None and r.capacity_fridge_cuft is None and r.door_style is None
    assert r.capacity_total_cuft == 5.5 and r.energy_kwh_year == 136.0 and r.price_usd == 1199.0
    assert r.extra_specs["Max spin speed (rpm)"] == "1300" and r.extra_specs["Cycles"] == "12"
    assert r.extra_specs["Steam"] == "Yes" and r.wifi_supported is True
    assert len(raw) > 40


def test_dryer_inferred_via_sub_path():
    r, _, _ = _rec("dryer")
    assert (r.category, r.subcategory) == ("washer", "dryer")
    assert r.voltage_v == "240" and r.amps == 24.0 and r.extra_specs["Fuel type"] == "Electric"


def test_range_and_cooking_specs():
    r, _, _ = _rec("range")
    assert (r.category, r.subcategory) == ("cooking", "gas_oven")  # PGF700AYFS is a gas range
    assert r.extra_specs["Burners/elements"] == "5" and r.extra_specs["Convection"] == "Yes"
    assert r.extra_specs["Oven capacity (cu ft)"] == "5.3" and r.amps == 13.0
    r, _, _ = _rec("otr")
    assert r.subcategory == "otr" and r.extra_specs["Microwave wattage (W)"] == "1000"
    assert "Burners/elements" not in r.extra_specs and r.wifi_supported is None
    r, _, _ = _rec("induct")
    assert r.subcategory == "induction" and r.extra_specs["Burners/elements"] == "4"
    r, _, _ = _rec("wall")
    assert r.subcategory == "electric_oven" and r.voltage_v == "208/240" and r.depth_in == 26.75  # "26 3/4 in"


def test_wall_oven_full_spec_table_and_image():
    r, raw, _ = _rec("wall_double")
    x = r.extra_specs
    assert "No Preheat Air Fry" in x["Features > Oven Cooking Modes"]
    assert x["Features > Oven Cooking Modes"].count(" | ") == 6
    assert x["Features > Oven Rack Features"].startswith("1 Heavy-Duty Offset Oven Rack | 2 Heavy-Duty Roller Racks")
    for k in ("Features > Oven Rack Positions (Single or Upper/Lower)", "Features > Oven Interior",
              "Features > Control Type", "Features > Cooking System", "Power / Ratings > Bake Wattage"):
        assert k in x, k
    assert "Oven capacity (cu ft)" in x and "Convection" in x  # curated labels kept
    assert "Profile™ XL" not in x["Appearance > Oven Door Features"]
    assert len(x) >= len([r_ for r_ in raw if r_.section != "Claims"])  # every table row present
    assert r.image_url == ("https://cdn11.bigcommerce.com/s-pacto3wrn2/images/stencil/1280x1280/products/"
                           "137494/1028681/xyecpznsbxkyoa1eytpm__76578.1776499125.jpg?c=2")


def test_main_image_url_rules():
    img = lambda d: {"main_image": {"data": d}}
    assert ge.main_image_url({}) is None
    assert ge.main_image_url(img("https://evil.example.com/a.jpg")) is None
    assert ge.main_image_url(img("http://cdn11.bigcommerce.com/a.jpg")) is None
    assert ge.main_image_url({"main_image": None, "images": [{"data": "https://cdn11.bigcommerce.com/x/{:size}/a.jpg"}]})         == "https://cdn11.bigcommerce.com/x/1280x1280/a.jpg"


def test_unsupported_product_raises():
    po = _po("washer")
    po["category"] = ["GE Appliances/Kitchen/Dishwashers/Built In Dishwashers"]
    try:
        ge.parse_product("https://www.geappliances.com/appliance/X-ABCD", po)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_helpers():
    assert ge._inches("26 3/4 in") == 26.75 and ge._inches("1/8 in") == 0.125 and ge._inches("35.75 in") == 35.75
    assert ge._first_num("1,000 W") == 1000.0
    assert ge._electrical({"Electrical Requirements": "120V", "Hertz": "60"})["voltage_v"] == "120"


def test_model_from_url():
    base = "https://www.geappliances.com/appliance/"
    assert ge.model_from_url(base + "GE-Profile-27-Smart-PVD28HYYFS") == "PVD28HYYFS"
    assert ge.model_from_url(base + "1-2-Cu-Ft-Over-the-Range-UVM9125DYWW") == "UVM9125DYWW"
    for bad in ("http://www.geappliances.com/appliance/X-ABCD1", "https://example.com/appliance/X-ABCD1",
                "https://geappliances.com.evil.com/appliance/X-ABCD1", "https://www.geappliances.com/cart.php"):
        try:
            ge.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_extract_product_errors():
    for html in ("<html></html>", "<script>window.stencilBootstrap(\"product\", \"{}\", {});</script>"):
        try:
            ge.extract_product(html)
        except ge.GEPageError:
            continue
        raise AssertionError(html)


def test_hosts_and_pdf_urls():
    assert ge._pdf_url_ok("https://images.salsify.com/image/upload/x.pdf")
    assert ge._pdf_url_ok("https://products.geappliances.com/x.pdf")
    assert not ge._pdf_url_ok("http://images.salsify.com/x.pdf")
    assert not ge._pdf_url_ok("https://salsify.com.evil.io/x.pdf")
    try:
        ge._check_final_url("https://evil.example.com/appliance/X")
    except ge.GEPageError:
        pass
    else:
        raise AssertionError("expected GEPageError")


def _with_env(value, fn):
    import os
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    if value is None:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
    else:
        os.environ["FRIDGE_BROWSER_MODE"] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_strategies_read_env_every_call():
    assert _with_env(None, ge._strategies) == ["requests", "headless", "visible"]
    assert _with_env("headless", ge._strategies) == ["headless"]
    assert _with_env("VISIBLE", ge._strategies) == ["visible"]
    assert _with_env("auto", ge._strategies) == ["requests", "headless", "visible"]


def test_fetch_falls_back_in_order_and_honors_modes():
    calls = []

    def fake_req(url):
        calls.append("requests")
        raise ge.GEPageError("blocked")

    def fake_browser(url, headless):
        calls.append("headless" if headless else "visible")
        if headless:
            raise ge.GEPageError("blocked")
        return "<html>ok</html>"

    o_req, o_br = ge._via_requests, ge._via_browser
    ge._via_requests, ge._via_browser = fake_req, fake_browser
    url = "https://www.geappliances.com/appliance/X-ABCD1"
    try:
        assert _with_env("auto", lambda: ge.fetch_pdp_html(url)) == "<html>ok</html>"
        assert calls == ["requests", "headless", "visible"]
        calls.clear()
        assert _with_env("visible", lambda: ge.fetch_pdp_html(url)) == "<html>ok</html>" and calls == ["visible"]
        calls.clear()
        try:
            _with_env("headless", lambda: ge.fetch_pdp_html(url))
        except ge.GEPageError:
            assert calls == ["headless"]
        else:
            raise AssertionError("explicit headless must not fall back")
        try:
            ge.fetch_pdp_html("https://evil.example.com/appliance/X-ABCD1")
        except ge.GEPageError:
            pass
        else:
            raise AssertionError("off-domain url must be refused")
    finally:
        ge._via_requests, ge._via_browser = o_req, o_br


def test_not_found_does_not_fall_back():
    calls = []

    def nf(url):
        calls.append("requests")
        raise ge.GENotFound("HTTP 404")

    o_req, o_br = ge._via_requests, ge._via_browser
    ge._via_requests, ge._via_browser = nf, lambda u, h: calls.append("browser")
    try:
        try:
            _with_env("auto", lambda: ge.fetch_pdp_html("https://www.geappliances.com/appliance/X-ABCD1"))
        except ge.GENotFound:
            assert calls == ["requests"]
            return
        raise AssertionError("expected GENotFound")
    finally:
        ge._via_requests, ge._via_browser = o_req, o_br


def test_scrape_downloads_only_allowed_and_sanitizes_model():
    po = _po("washer")
    po["sku"] = "PFW/955 SPWDS"
    po["custom_fields"].append({"name": "Documents_9_Energy Guide", "value": "http://evil.example.com/e.pdf"})
    got = []
    o_fetch, o_dl, o_sleep = ge.fetch_pdp_html, ge.common.download_pdf, ge.time.sleep
    ge.fetch_pdp_html = lambda url: "<script>" + ge.BOOTSTRAP + json.dumps(json.dumps({"productObj": po})) + ", {});</script>"
    ge.common.download_pdf = lambda brand, model, dtype, url: got.append((brand, model, dtype, url)) or None
    ge.time.sleep = lambda s: None
    try:
        rec, docs, raw = ge.scrape("https://www.geappliances.com/appliance/GE-Washer-PFW955SPWDS")
    finally:
        ge.fetch_pdp_html, ge.common.download_pdf, ge.time.sleep = o_fetch, o_dl, o_sleep
    assert docs == [] and got and all(m == "PFW_955_SPWDS" and u.startswith("https://images.salsify.com/")
                                      for _, m, _, u in got)
    assert rec.subcategory == "front_load" and raw


def test_discover_french_door_listed_also_under_bottom_freezer_is_not_duplicated():
    fd = _j("ss_french_door.json")
    both = dict(fd["results"][0], sku="BOTH1", name="GE 28 Cu. Ft. Refrigerator", spec_features_configuration="",
                custom_url="/appliance/GE-Both-BOTH1")  # in both categories, no 'french' in its text
    fd = dict(fd, results=[both])
    pages = {p: fd for p, _ in ge.SUB_SOURCES["french_door"][1]}
    pages[ge.SUB_SOURCES["bottom_freezer"][1][0][0]] = fd
    orig = (ge._search_page, ge.time.sleep)
    ge._search_page, ge.time.sleep = (lambda path, n: pages[path]), (lambda s: None)
    try:
        fdoor = {c.model_number for c in ge.discover("french_door", limit=50)}
        bottom = {c.model_number for c in ge.discover("bottom_freezer", limit=50)}
    finally:
        ge._search_page, ge.time.sleep = orig
    assert fdoor == {"BOTH1"} and bottom == set()


def test_laundry_center_beats_front_load_when_both_categories_present():
    po = {"category": ["GE Appliances/Laundry/Washers/Front Loading Washers",
                       "GE Appliances/Laundry/Stacked Washer Dryer Units"]}
    assert ge.infer_subcategory(po, {"name": "GE Unitized Spacemaker"}) == "laundry_center"


def _resp(status, loc=None, text=""):
    import types
    return types.SimpleNamespace(status_code=status, headers={"Location": loc} if loc else {}, url="",
                                 content=text.encode(), history=[])


def test_via_requests_checks_every_redirect_hop_before_requesting_it():
    calls = []
    start = "https://www.geappliances.com/appliance/A-B1234"
    seq = {start: _resp(302, "https://evil.example.com/x"), "https://evil.example.com/x": _resp(200, text="secret")}

    def fake_get(url, **kw):
        calls.append((url, kw.get("allow_redirects")))
        return seq[url]

    orig = ge.requests.get
    ge.requests.get = fake_get
    try:
        ge._via_requests(start)
    except ge.GEPageError:
        pass
    else:
        raise AssertionError("expected GEPageError")
    finally:
        ge.requests.get = orig
    assert calls == [(start, False)]  # evil host never requested


def test_via_requests_follows_same_host_redirect_and_rejects_http_downgrade():
    body = "<html>" + ge.BOOTSTRAP + "</html>" + "<p>product page text</p>" * 60
    pages = {"https://www.geappliances.com/a": _resp(301, "/appliance/b"),
             "https://www.geappliances.com/appliance/b": _resp(200, text=body),
             "https://www.geappliances.com/d": _resp(302, "http://www.geappliances.com/e")}
    calls = []
    orig = ge.requests.get
    ge.requests.get = lambda url, **kw: (calls.append(url), pages[url])[1]
    try:
        assert ge._via_requests("https://www.geappliances.com/a") == body
        try:
            ge._via_requests("https://www.geappliances.com/d")
        except ge.GEPageError:
            pass
        else:
            raise AssertionError("expected GEPageError")
    finally:
        ge.requests.get = orig
    assert "http://www.geappliances.com/e" not in calls



def test_cooking_overlap_one_sub_per_model():
    from html import unescape
    data = _j("ss_cooking_overlap.json")
    got = {}
    for item in data["results"]:
        cats = ge._item_categories(item)
        it = dict(item, name=unescape(item["name"]))
        got[item["sku"]] = ge._classify(cats, it)
    assert got["PT9900SWSS"] == "sco" and got["JT3800SHSS"] == "sco" and got["PK7800SKSS"] == "sco"  # combo wall ovens
    assert got["PSB9120SVSS"] == "sco" and got["PSA9120SPSS"] == "sco"  # Advantium wall oven / OTR speed oven
    assert got["PTD9000SZSS"] == "electric_oven" and got["PKS7000SZSS"] == "electric_oven"
    assert got["PGF700AYFS"] == "gas_oven" and got["PGB965BPTS"] == "gas_oven" and got["P2S930YPFS"] == "gas_oven"
    assert got["PB965YPFS"] == "radiant" and got["PRF700AYFS"] == "radiant"  # electric double-oven range / electric range
    assert got["PHS700AYFS"] == "induction"  # also listed under Electric Ranges
    assert got["UVM9125DYWW"] == "otr" and got["PCWK22U1WSS"] == "microwave"
    assert sorted(got.values(), key=str).count("radiant") >= 3  # + the electric cooktop
    # discover's filter (parse_search + classify == sub) puts every classified model under exactly one sub key
    by_sub = {}
    for sub in ("sco", "gas_oven", "gas_cooktop", "radiant", "induction", "electric_oven", "otr", "microwave"):
        by_sub[sub] = {c.model_number for c, it in ge.parse_search(data, sub)
                       if ge._classify(ge._item_categories(it), dict(it, name=unescape(it["name"]))) == sub}
    flat = [m for v in by_sub.values() for m in v]
    assert len(flat) == len(set(flat)) and set(flat) == {k for k, v in got.items() if v}
    gas_cooktop = [i for i in data["results"] if "Gas Cooktops" in " ".join(i["categories_hierarchy"])][0]
    assert got[gas_cooktop["sku"]] == "gas_cooktop"  # oven-less gas cooktop (not an oven, not electric)
    assert gas_cooktop["sku"] in by_sub["gas_cooktop"]
    assert ge.infer_subcategory({"category": ["GE Appliances/Kitchen/Wall Ovens/Microwave Oven Combination",
                                              "GE Appliances/Kitchen/Wall Ovens/Double Wall Ovens"]},
                                {"name": "Combination Double Wall Oven"}) == "sco"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
