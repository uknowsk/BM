"""Plain-assert offline tests (saved fixtures). Run: python tests/test_electrolux_de.py
electrolux_de: no web shop -> category pages (SSR __NEXT_DATA__ productList), recommended prices in EUR, German pages."""
import contextlib
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import catalog
import electrolux_de as e

FX = Path(__file__).parent / "fixtures" / "electrolux_de"
PDP = "https://www.electrolux.de/kitchen/cooking/"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


class FakeSsr:
    """text(url) answers category pages from fixtures by path; rendered() answers a product page."""

    def __init__(self, pages=None, html=""):
        self.pages, self.html, self.calls = pages or {}, html, []

    def text(self, url):
        self.calls.append(url)
        for key, val in self.pages.items():
            if key in url:
                return val
        raise RuntimeError(f"unexpected fetch {url}")

    def json(self, url):
        raise AssertionError("electrolux_de has no shop API")

    def rendered(self, url, ready, timeout_ms=0):
        self.calls.append(url)
        return self.html


@contextlib.contextmanager
def fake_session(sess):
    orig_s, orig_sleep = ec.session, ec.time.sleep
    ec.session = lambda *a, **k: contextlib.nullcontext(sess)
    ec.time.sleep = lambda s: None
    try:
        yield
    finally:
        ec.session, ec.time.sleep = orig_s, orig_sleep


def test_supported_subcategories():
    assert e.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "electric_oven", "induction", "radiant"}
    assert e.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert (e.BRAND, e.COUNTRY, e.REGION, e.CURRENCY) == ("Electrolux", "de", "eu", "EUR")
    assert catalog.module_name("Electrolux", "de") == "electrolux_de"
    for bad in ("gas_cooktop", "gas_oven", "otr", "french_door"):
        try:
            e.discover(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_ssr_listing_parser():
    prods, total = ec.parse_ssr_listing(_t("category_induction.html"))
    assert total == 10 and [p["modelId"] for p in prods] == ["LIV834I", "EIV634I", "LIT8140M"]
    try:
        ec.parse_ssr_listing('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"pageData":'
                             '{"components":[{"name":"X"}]}}}}</script>')
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_discover_induction_from_ssr_category_page():
    sess = FakeSsr({"hobs/induction-hob": _t("category_induction.html"), "hobs/combohob": _t("category_induction.html")})
    with fake_session(sess):
        got = e.discover("induction", 3)
    assert [c.model_number for c in got] == ["LIV834I", "EIV634I", "LIT8140M"]
    c = got[0]
    assert c.brand == "Electrolux" and (c.category, c.subcategory) == ("cooking", "induction")
    assert (c.region, c.country, c.currency, c.price_local, c.price_usd) == ("eu", "de", "EUR", 1017.0, None)
    assert c.url == "https://www.electrolux.de/kitchen/cooking/hobs/induction-hob/liv834i/"
    assert c.attrs == {"width_in": 31.5, "fuel": "induction"}
    assert sess.calls[0] == "https://www.electrolux.de/kitchen/cooking/hobs/induction-hob/"


def test_discover_splits_ovens_into_sco_and_electric_oven():
    pages = {"ovens/oven": _t("category_ovens.html"), "pyrolytic-oven": _t("category_ovens.html"),
             "pizza-oven": _t("category_ovens.html")}
    with fake_session(FakeSsr(pages)):
        sco = e.discover("sco", 5)
        plain = e.discover("electric_oven", 5)
    assert [c.model_number for c in sco] == ["KVLFE46X"]  # 'Einbau-Kompaktbackofen / Mikrowelle'
    assert [c.model_number for c in plain] == ["KEHLH00BX", "KOHLH00BX"]
    assert sco[0].price_local == 1199.0 and sco[0].attrs == {"fuel": "electric"}


def test_pagination_requests_more_items_until_total_is_reached():
    html = _t("category_ovens.html")  # 3 listed of 22 total -> asks ?page=2, ?page=3 ... (15 items per page step)
    sess = FakeSsr({"ovens/oven": html, "pyrolytic-oven": html, "pizza-oven": html})
    with fake_session(sess):
        e.discover("electric_oven", 30)
    urls = [u for u in sess.calls if u.endswith("ovens/oven/") or "ovens/oven/?page=" in u]
    assert urls[:2] == ["https://www.electrolux.de/kitchen/cooking/ovens/oven/",
                        "https://www.electrolux.de/kitchen/cooking/ovens/oven/?page=2"] and len(urls) <= ec.MAX_PAGES


def _pdp(name, path):
    pdp = ec.parse_pdp(_t(f"pdp_{name}.html"))
    pdp["category_path"] = path
    return pdp


def test_german_pages_become_english_records():
    rec, raw = e.parse_product("EOF6P46Z", PDP + "ovens/pyrolytic-oven/eof6p46z/",
                               _pdp("EOF6P46Z", "kitchen/cooking/ovens/pyrolytic-oven"), 939.0)
    assert rec.subcategory == "electric_oven" and (rec.country, rec.currency, rec.price_local) == ("de", "EUR", 939.0)
    assert (rec.height_in, rec.width_in, rec.depth_in) == (23.4, 23.5, 22.4) and rec.weight_lb == 68.6
    assert rec.extra_specs["Energy > EU energy class"] == "EU class A+"
    assert rec.extra_specs["Energy values > Energy efficiency class"] == "A+"
    assert any(r.key == "Energieeffizienzklasse" and r.section == "Energiewerte" for r in raw)  # German source kept
    assert rec.image_url.startswith("https://electrolux.bynder.com/transform/WS_ZO2000/")
    rec, _ = e.parse_product("LIV834I", PDP + "hobs/induction-hob/liv834i/", _pdp("LIV834I", "kitchen/cooking/hobs/induction-hob"), 1017.0)
    assert rec.subcategory == "induction" and rec.width_in == 30.3 and rec.voltage_v == "220-240/400V2N"
    # without a shop price the schema.org price (recommended retail price) is used
    rec, _ = e.parse_product("LIV834I", "u", _pdp("LIV834I", "kitchen/cooking/hobs/induction-hob"))
    assert rec.price_local == 1017.0


def test_image_urls_map_to_the_brand_cdn_and_foreign_hosts_are_dropped():
    site = e.SITE
    assert ec.eu_main_image(site, {"images": ["https://www.electrolux.de/services/eml/transform/WS_ZO2000/a-b/1-eps"]}) == \
        "https://electrolux.bynder.com/transform/WS_ZO2000/a-b/1-eps"
    assert ec.eu_main_image(site, {"images": ["/services/eml/transform/PV/a-b/1-eps"]}) == \
        "https://electrolux.bynder.com/transform/PV/a-b/1-eps"
    assert ec.eu_main_image(site, {"images": ["https://evil.example.com/a.png", "http://www.electrolux.de/a.png"]}) is None


def test_scrape_flow_and_url_validation():
    html = _t("pdp_EOF6P46Z.html")
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append(wanted), [])[1]
    try:
        with fake_session(FakeSsr(html=html)):
            rec, docs, raw = e.scrape(PDP + "ovens/pyrolytic-oven/eof6p46z/")
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "EOF6P46Z" and rec.price_local == 939.0 and rec.subcategory == "electric_oven" and raw
    assert [t for t, _ in seen[0]] == ["Manual", "EnergyGuide"]
    for bad in ("https://www.aeg.de/kitchen/cooking/ovens/oven/eof6p46z/", "http://www.electrolux.de/kitchen/cooking/a/bbbb/",
                "https://www.electrolux.de/kitchen/cooling/refrigerators/x/cccc/"):
        try:
            e.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
