"""Plain-assert offline tests (saved fixtures). Run: python tests/test_viking_us.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import viking_us as v

FX = Path(__file__).parent / "fixtures" / "viking_us"
B = "https://vikingrange.com/products/cook/"
URLS = {
    "RVDR33025BAN": B + "ranges/model/RVDR3302/sku/RVDR33025BAN",
    "VRT53044BSS": B + "rangetops/model/VRT5304/sku/VRT53044BSS",
    "VMOR506SS": B + "microwaves/model/VMOR506/sku/VMOR506SS",
}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def test_supported_cooking_only_and_crawl_delay():
    import catalog
    assert v.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking")) and len(v.SUPPORTED_SUBCATEGORIES) == 8
    assert v.DELAY_S >= 10  # robots.txt Crawl-delay: 10
    try:
        v.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_classify_rules():
    c = v.classify
    assert c('30"W. Self-Cleaning Dual Fuel Range', "RVDR3302", "ranges") == "gas_oven"
    assert c('48"W. Sealed Burner Gas Range', "VGR7482", "ranges") == "gas_oven"
    assert c('30"W. Induction Range', "X", "ranges") == "induction"
    assert c('30"W. Electric Range', "X", "ranges") == "radiant"
    assert c('36"W. Gas Rangetop', "VRT5364", "rangetops") == "gas_cooktop"
    assert c('36"W. Induction Rangetop', "VIRT5364", "rangetops") == "induction"
    assert c('36"W. Electric Cooktop', "VECU5361", "cooktops") == "radiant"
    assert c('36"W. Panorama Downdraft/Cooktop', "MVIDC636", "cooktops") == "induction"
    assert c('18"W. Undercounter Induction Warmer', "VUIW518", "cooktops") is None
    assert c('DISCONTINUED | 30"W. Gas Rangetop', "VRT530", "rangetops") is None
    assert c('30"W. Convection Microwave Hood', "VMOR506", "microwaves") == "otr"
    assert c('24"W. Conventional Microwave Oven', "VMOS501", "microwaves") == "microwave"
    assert c('30"W. Electric Combi-Speed Oven', "MVMSP6301SS", "microwaves") == "sco"
    assert c('30"W. Electric Double Premiere Oven', "VDOE530", "wall-ovens") == "electric_oven"
    assert c('42"W. Built-in Side-by-Side', "VCSB5424", "") is None
    assert v.section_of("/products/cook/rangetops/model/A/sku/B") == "rangetops" and v.section_of("/products/chill/x") == ""


def test_listings():
    r = v.parse_listing(_t("listing_ranges.html"))
    r = r[18:] + r[:18]  # VGR7482 is the 19th card on the live page
    assert len(r) == 26 and r[0]["model"] == "VGR7482" and r[0]["name"] == '48"W. Sealed Burner Gas Range'
    assert r[0]["path"] == "/products/cook/ranges/model/VGR7482/sku/VGR74826GSS" and r[0]["series"] == "7 Series"
    got = [c for c in r if v.classify(c["name"], c["model"], v.section_of(c["path"])) == "gas_oven"]
    assert len(got) == 20
    cand = v._candidate(r[0], "gas_oven")
    assert cand.url == "https://vikingrange.com" + r[0]["path"] and cand.price_usd is None
    assert cand.attrs == {"width_in": 48.0, "fuel": "gas"} and cand.category == "cooking" and cand.country == "us"
    rt = v.parse_listing(_t("listing_rangetops.html"))
    assert {v.classify(c["name"], c["model"], v.section_of(c["path"])) for c in rt} == {"gas_cooktop", "induction", "radiant", None}
    mw = v.parse_listing(_t("listing_microwaves.html"))
    assert {v.classify(c["name"], c["model"], v.section_of(c["path"])) for c in mw} == {"microwave", "otr", "sco"}
    wo = v.parse_listing(_t("listing_wall-ovens.html"))
    assert {v.classify(c["name"], c["model"], v.section_of(c["path"])) for c in wo} == {"electric_oven", "sco"}


def test_discover_filters_by_sub():
    seen = []
    orig = v.fetch_html
    v.fetch_html = lambda url, probe: seen.append(url) or _t("listing_rangetops.html")
    try:
        got = v.discover("gas_cooktop", limit=2)
    finally:
        v.fetch_html = orig
    assert [c.model_number for c in got] == ["VRT5304", "VRT5484"] and seen == [v.BASE + "/products/cook/rangetops"]


def test_scrape_parse_fixtures():
    exp = {"RVDR33025BAN": ("gas_oven", 7489.0, 29.875, 367.0), "VRT53044BSS": ("gas_cooktop", 5089.0, 29.88, 125.0),
           "VMOR506SS": ("otr", 2569.0, 29.9375, 85.0)}
    for model, (sub, price, w, wt) in exp.items():
        html = _t(f"pdp_{model}.html")
        rec, raw = v.parse_product(URLS[model], html)
        assert rec.subcategory == sub and rec.price_usd == price and rec.width_in == w and rec.weight_lb == wt, model
        assert rec.category == "cooking" and rec.model_number == model and rec.country == "us", model
        assert all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw) and len(raw) >= 6, model
        assert rec.image_url.startswith("https://middleby-cdn.com/"), model
        assert rec.pod_features, model
    rec, _raw = v.parse_product(URLS["RVDR33025BAN"], _t("pdp_RVDR33025BAN.html"))
    assert rec.extra_specs["Specifications > Energy Type"] == "Dual Fuel" and "Specifications > # of Burners" in rec.extra_specs
    docs = dict(v.doc_links(_t("pdp_RVDR33025BAN.html")))
    assert set(docs) == {"SpecSheet", "Installation", "Manual", "Warranty"}
    assert all(u.startswith("https://middleby-cdn.com/") for u in docs.values())


def test_url_validation_and_modes():
    assert v.parse_url(URLS["VMOR506SS"]) == ("microwaves", "VMOR506", "VMOR506SS")
    for bad in ("http://vikingrange.com/products/cook/ranges/model/A1/sku/B1", "https://evil.example.com/products/cook/ranges/model/A1/sku/B1",
                B + "ranges", B + "vent/model/A1/sku/B1", "https://vikingrange.com/products/cook/ranges/model/../sku/B1"):
        try:
            v.parse_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        assert v._strategies() == ["requests", "headless", "visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert v._strategies() == ["visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert v._strategies() == ["headless"]
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
