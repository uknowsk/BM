"""Plain-assert offline tests for gaggenau_us (shared Gaggenau core). Run: python tests/test_gaggenau_us.py"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gaggenau_us as g
import thermador_us as t

FX = Path(__file__).parent / "fixtures" / "gaggenau_us"
URLS = {
    "CG280212CA": "https://www.gaggenau.com/us/en/mkt-product/cooktops/200-series/200-series/gas/CG280212CA",
    "GO470720": "https://www.gaggenau.com/us/en/mkt-product/ovens/ovensexpressiveseries/ovensexpressiveseriesovens/GO470720",
}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


class FakeSession:
    site = g.SITE

    def text(self, url):
        assert url == "https://www.gaggenau.com/us/sitemap.xml"
        return _t("sitemap.xml")

    html = text


def _rec(m):
    return g.build_record(g.SITE, m, URLS[m], t.flight_text(_t(f"pdp_{m}.html")), g.facts_en,
                          dict(region="na", country="us", currency="USD"))


def test_supported_subcategories_cooking_only_and_discover_rejects_others():
    assert g.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "gas_cooktop", "electric_oven", "induction"}
    for sub in ("built_in", "french_door", "front_load", "gas_oven", "otr", "radiant"):
        try:
            g.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


def test_classify_prefix_beats_stale_path():
    c = g.classify
    assert c("/us/en/mkt-product/cooktops/400-series/400-series/cooktops400seriesfullsurfaceinductioncooktops/CG280212CA") == "gas_cooktop"
    assert c("/us/en/mkt-product/cooktops/200-series/200-series/induction/CI282602") == "induction"
    assert c("/us/en/mkt-product/cooktops/400-series/vario-400-series/gas/VG415211CA") == "gas_cooktop"
    assert c("/us/en/mkt-product/cooktops/200-series/vario-200-series/cooktops200varioserieselectricgrill/VR230620") is None
    assert c("/us/en/mkt-product/cooktops/200-series/200-series/knobs/CA230100") is None
    assert c("/us/en/mkt-product/ovens/x/ovensexpressiveseriesovens/GO470720") == "electric_oven"
    assert c("/us/en/mkt-product/ovens/x/ovensexpressiveseriescombisteamovens/GS460720") == "electric_oven"
    assert c("/us/en/mkt-product/ovens/x/ovensexpressiveseriescombimicrowaveovens/GM450720") == "sco"
    assert c("/us/en/mkt-product/ovens/400-series/combi-microwave-ovens/MW420621") == "microwave"  # MW = plain microwave
    assert c("/us/en/mkt-product/ovens/400-series/combi-microwave-ovens/BM450710") == "sco"
    assert c("/us/en/mkt-product/ovens/x/ovensexpressiveserieswarmingdrawers/GW451720") is None
    assert c("/us/en/mkt-product/ovens/x/ovensexpressiveseriesvacuumingdrawer/GV451720") is None
    assert c("/us/en/mkt-product/refrigeration/200-series/200-series/fridge-freezers/RB280703") is None
    assert c("/us/en/mkt-product/ventilation-systems/200-series/island-hoods/AI230700") is None


def test_sitemap_filters_and_candidates():
    urls = g.sitemap_urls(FakeSession())
    assert len(urls) == 13 and len(set(urls)) == 13  # duplicate dropped; category, de, http and foreign hosts dropped
    assert all(u.startswith("https://www.gaggenau.com/us/en/mkt-product/") for u in urls)
    got = {s: [c.model_number for c in g.candidates_from_urls(urls, g.SITE, s, "cooking", 30, "na", "us", "USD")]
           for s in g.SUPPORTED_SUBCATEGORIES}
    assert got == {"gas_cooktop": ["CG280212CA", "VG415211CA"], "induction": ["CI282602", "CX482611"],
                   "electric_oven": ["GO470720", "GS460720"], "sco": ["GM450720"], "microwave": ["MW420621"]}
    c = g.candidates_from_urls(urls, g.SITE, "gas_cooktop", "cooking", 1, "na", "us", "USD")
    assert len(c) == 1 and (c[0].brand, c[0].category, c[0].subcategory, c[0].country) == ("Gaggenau", "cooking", "gas_cooktop", "us")
    assert c[0].price_usd is None and c[0].price_local is None and c[0].name == "Gas cooktop CG280212CA"
    assert g.candidate_name(URLS["GO470720"].replace("ovensexpressiveseriesovens", "combisteamovens"), "electric_oven").startswith("Combi-steam")


def test_gas_cooktop_record():
    r, raw = _rec("CG280212CA")
    assert (r.brand, r.category, r.subcategory, r.country, r.currency) == ("Gaggenau", "cooking", "gas_cooktop", "us", "USD")
    assert r.price_usd is None and r.width_in == 30.0 and r.weight_lb == 39.7  # 18 kg listed
    assert r.voltage_v == "120" and r.frequency_hz == 60.0
    assert r.extra_specs["Technical data > Burners"] == "5" and "BTU" in r.extra_specs["Technical data > Total rating"]
    assert r.extra_specs["Additional information > Features"].count(" | ") == len(raw) - 1 == 25
    assert r.image_url.startswith("https://media3.bsh-group.com/Product_Shots/") and r.pod_features[0].startswith("Control panel")
    assert raw[0].section == "Additional information" and raw[0].key == "Feature 1" and raw[0].source == "web"


def test_oven_record_capacity_and_wifi():
    r, _ = _rec("GO470720")
    assert r.subcategory == "electric_oven" and r.width_in == 24.0 and r.wifi_supported is True
    assert r.extra_specs["Technical data > Cavity capacity (cu ft)"] == "2.72"
    assert r.extra_specs["Technical data > Total rating"].endswith("3.0 KW")


def test_url_validation():
    assert g.model_from_url(URLS["CG280212CA"]) == "CG280212CA"
    for bad in ("http://www.gaggenau.com/us/en/mkt-product/cooktops/x/gas/CG1234",
                "https://evil.example.com/us/en/mkt-product/cooktops/x/gas/CG1234",
                "https://www.gaggenau.com/de/de/mkt-product/kochfelder/x/gas/CG1234",
                "https://www.gaggenau.com/us/en/product/accessories/x/CG1234",
                "https://www.gaggenau.com/us/en/mkt-product/cooktops/../CG1234"):
        try:
            g.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_scrape_without_network():
    @contextmanager
    def fake_session(site, url):
        yield type("S", (), {"html": lambda self, u: _t("pdp_GO470720.html")})()

    s0, d0 = g.session, g.download_docs
    g.session, g.download_docs = fake_session, lambda *a, **k: []
    try:
        rec, docs, raw = g.scrape(URLS["GO470720"])
    finally:
        g.session, g.download_docs = s0, d0
    assert rec.model_number == "GO470720" and docs == [] and len(raw) == 74


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
