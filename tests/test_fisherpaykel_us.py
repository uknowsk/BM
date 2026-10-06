"""Offline tests for the Fisher & Paykel US adapter. Run: python tests/test_fisherpaykel_us.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import fisherpaykel_us as fp

FX = Path(__file__).parent / "fixtures" / "fisherpaykel_us"
B = "https://www.fisherpaykel.com/us/cooking/"
URLS = {
    "OR30SCG4X1": B + "ranges/classic-ranges/30in-series-7-classic-4-burner-gas-range-or30scg4x1-81712.html",
    "OS30SMUNB3": B + "ovens/all-ovens/30in-series-11-minimal-handleless-combi-steam-oven-os30smunb3-84994.html",
    "CI152DTTB1": B + "cooktops/minimal-cooktops/15in-series-11-2-zone-induction-cooktop-smartzone-ci152dttb1-82829.html",
}


def _html(sku):
    return (FX / f"pdp_{sku}.html").read_text(encoding="utf-8")


def test_supported():
    assert fp.SUPPORTED_SUBCATEGORIES == {"gas_oven", "induction", "gas_cooktop", "radiant", "electric_oven", "sco", "microwave", "otr"}
    assert fp.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.supported("Fisher & Paykel", "us") == fp.SUPPORTED_SUBCATEGORIES


def test_classify_paths():
    c = fp.classify
    assert c(B + "ranges/classic-ranges/30in-series-7-classic-4-burner-gas-range-or30scg4x1-81712.html") == "gas_oven"
    assert c(B + "ranges/professional-ranges/48in-series-9-professional-dual-fuel-6-burner-range-rdv3-486gd-n-82977.html") == "gas_oven"
    assert c(B + "ranges/professional-ranges/48in-series-11-professional-4-burner-%2B-4-zone-hybrid-self-cleaning-range-ng-rhv3-484-n-82792.html") == "gas_oven"
    assert c(B + "ranges/classic-ranges/30in-series-9-classic-4-zone-induction-self-cleaning-range-or30sci6x1-81961.html") == "induction"
    assert c(B + "ranges/contemporary-ranges/30in-series-7-4-element-electric-range-ceramic-radiant-or30sde6x1-81721.html") == "radiant"
    assert c(B + "cooktops/contemporary-cooktops/36in-series-7-5-burner-gas-cooktop-lpg-cg365dlpx1_n-81456.html") == "gas_cooktop"
    assert c(B + "cooktops/professional-cooktops/48in-series-9-8-burner-gas-rangetop-lpg-cpv3-488-l-82982.html") == "gas_cooktop"
    assert c(B + "cooktops/minimal-cooktops/36in-series-5-5-element-electric-cooktop-ce365dtb1-81955.html") == "radiant"
    assert c(B + "cooktops/minimal-cooktops/15in-series-11-auxiliary-teppanyaki-cooktop-cit152dx1-82833.html") is None
    assert c(B + "ovens/all-ovens/30in-series-11-minimal-combi-steam-oven-os30smub3-84992.html") == "electric_oven"
    assert c(B + "ovens/professional-ovens/30in-series-9-professional-compact-convection-speed-oven-om30npux3-84983.html") == "sco"
    assert c(B + "ovens/contemporary-ovens/24in-series-7-contemporary-microwave-drawer-omd24sdb1-82992.html") == "microwave"
    assert c(B + "ovens/contemporary-ovens/30in-series-5-contemporary-over-the-range-microwave-moh30sb1-71638.html") == "otr"
    assert c("https://www.fisherpaykel.com/us/cooling/bottom-freezers/some-fridge-rf1-1.html") is None


def test_sitemap_discovery(monkeypatch=None):
    pages = {fp.SITEMAP_INDEX: (FX / "sitemap_index.xml").read_text(encoding="utf-8")}
    sm = (FX / "sitemap_1.xml").read_text(encoding="utf-8")
    orig = fp.fetch
    fp.fetch = lambda url, need: pages.get(url, sm)
    fp._sitemap_cache = None
    try:
        urls = fp.product_urls()
        assert urls and all(fp.classify(u) for u in urls)  # teppanyaki and fridge pages are dropped
        assert not any("teppanyaki" in u or "/cooling/" in u for u in urls)
        by_page = {u: _html(k) for k, u in URLS.items()}
        fp.fetch = lambda url, need: by_page[url]
        fp._sitemap_cache = (fp.time.monotonic(), list(URLS.values()))
        got = fp.discover("gas_oven", limit=5)
        assert [c.model_number for c in got] == ["OR30SCG4X1"]
        c = got[0]
        assert (c.brand, c.category, c.subcategory, c.price_usd) == ("Fisher & Paykel", "cooking", "gas_oven", 5499.0)
        assert c.attrs["width_in"] == 30.0 and c.attrs["fuel"] == "gas" and c.url == URLS["OR30SCG4X1"]
        for bad in ("french_door", "microwave_x"):
            try:
                fp.discover(bad)
            except ValueError:
                continue
            raise AssertionError("expected ValueError")
    finally:
        fp.fetch = orig
        fp._sitemap_cache = None


def test_gas_range_page():
    r, raw, docs = fp.parse_product(URLS["OR30SCG4X1"], _html("OR30SCG4X1"))
    assert (r.brand, r.model_number, r.category, r.subcategory, r.price_usd) == ("Fisher & Paykel", "OR30SCG4X1", "cooking", "gas_oven", 5499.0)
    assert r.product_name == '30" Series 7 Classic 4 Burner Gas Range'
    assert (r.width_in, r.height_in, r.depth_in) == (29.875, 35.75, 25.25)  # '29 7/8', '35 3/4 - 37 5/8', '25 1/4'
    assert r.capacity_total_cuft == 3.5 and (r.voltage_v, r.amps, r.frequency_hz) == ("120V", 15.0, 60.0)
    assert r.pod_features[0] == "Four burner gas cooktop with two dual wok burners"
    assert r.extra_specs["Burner ratings > Maximum burner power"] == "18000 BTU" and r.extra_specs["Fuel"] == "Gas"
    assert not any(k.startswith("Accessories") for k in r.extra_specs)
    assert r.image_url.startswith("https://dam.fisherpaykel.com/")
    assert any(x.section == "Gas Requirements" and x.key.startswith("Supply Pressure") for x in raw)
    assert [t for t, _ in docs] == ["Installation", "Manual"] and all(u.startswith("https://dam.fisherpaykel.com/") for _, u in docs)


def test_other_pages():
    r = fp.parse_product(URLS["OS30SMUNB3"], _html("OS30SMUNB3"))[0]
    assert r.subcategory == "electric_oven" and r.price_usd == 8299.0 and r.capacity_total_cuft == 3.0 and r.wifi_supported is True
    r = fp.parse_product(URLS["CI152DTTB1"], _html("CI152DTTB1"))[0]
    assert r.subcategory == "induction" and r.price_usd == 2649.0 and r.extra_specs["Fuel"] == "Induction"
    try:
        fp.parse_product("https://www.fisherpaykel.com/us/cooling/bottom-freezers/some-fridge-rf1-1.html", _html("OR30SCG4X1"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_url_check_and_docs():
    for bad in ("http://www.fisherpaykel.com/us/cooking/ranges/x/y-range-1.html", "https://evil.example.com/us/cooking/ranges/x/y-gas-range-1.html",
                "https://www.fisherpaykel.com/us/cooling/bottom-freezers/some-fridge-rf1-1.html"):
        try:
            fp.check_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
    assert fp._doc_type("https://dam.fisherpaykel.com/a/FP-UserGuide-fr-X.pdf") is None  # only English documents
    assert fp._doc_type("https://dam.fisherpaykel.com/a/FP-UserGuide-en-X.pdf") == "Manual"
    assert fp._doc_type("https://dam.fisherpaykel.com/a/FP-Proposition65-CaliforniaWarning-en.pdf") is None


def test_new_badge_only_when_site_flags_it():
    html = _html("OS30SMUNB3")
    assert fp.parse_page(URLS["OS30SMUNB3"], html)["is_new"] is None and fp.parse_product(URLS["OS30SMUNB3"], html)[0].is_new is None
    flagged = html + '<div class="badges-container"> <ul class="product-badges"> <li>NEW</li> </ul> </div>'
    assert flagged != html and fp.parse_product(URLS["OS30SMUNB3"], flagged)[0].is_new is True
    arriving = html + '<div class="badges-container"> <ul class="product-badges"> <li>ARRIVING NOV 2026</li> </ul> </div>'
    assert fp.parse_page(URLS["OS30SMUNB3"], arriving)["is_new"] is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
