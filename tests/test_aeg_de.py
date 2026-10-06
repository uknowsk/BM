"""Plain-assert offline tests (saved fixtures). Run: python tests/test_aeg_de.py
Covers aeg_de and the EU half of _electrolux_common (OCC listing, product-page parser, classification, German -> English)."""
import contextlib
import json
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"  # offline: glossary + rules only, never the local LLM
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import aeg_de as a
import catalog

FX = Path(__file__).parent / "fixtures" / "aeg_de"
PDP = "https://www.aeg.de/kitchen/cooking/"
EMPTY = {"products": [], "pagination": {"totalPages": 0, "totalResults": 0}}


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


class FakeApi:
    """OCC search answers by substring of the (quoted) category / code query; page html for rendered()."""

    def __init__(self, by_key=None, html=None):
        self.by_key, self.html, self.calls = by_key or {}, html or "", []

    def json(self, url):
        self.calls.append(url)
        for key, val in self.by_key.items():
            if key in url:
                return val
        return EMPTY

    def text(self, url):
        raise AssertionError("aeg_de lists through the API, not SSR pages")

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


def test_supported_subcategories_are_the_real_ones():
    assert a.SUPPORTED_SUBCATEGORIES == {"microwave", "electric_oven", "induction", "radiant"}
    assert a.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert (a.BRAND, a.COUNTRY, a.REGION, a.CURRENCY) == ("AEG", "de", "eu", "EUR")
    assert catalog.module_name("AEG", "de") == "aeg_de"
    for bad in ("french_door", "gas_oven", "otr", "sco", "gas_cooktop"):
        try:
            a.discover(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_classify_rules():
    c = a.classify
    assert c("kitchen/cooking/microwaves/microwave-oven", "60cm Einbau-Mikrowelle / Touch") == "microwave"
    assert c("kitchen/cooking/compact-built-in-range/built-in-microwaves", "Freistehende Mikrowelle / Grill") == "microwave"
    assert c("kitchen/cooking/ovens/oven", "Einbaubackofen / Pyrolyse") == "electric_oven"
    assert c("kitchen/cooking/ovens/steam-oven", "Dampfbackofen") == "electric_oven"
    assert c("kitchen/cooking/ovens/oven", "Einbau-Kompaktbackofen / Mikrowelle") == "sco"
    assert c("kitchen/cooking/microwaves/microwave-oven", "Microwave and Built-in Oven") == "sco"
    assert c("kitchen/cooking/hobs/induction-hob", "Induktionskochfeld") == "induction"
    assert c("kitchen/cooking/hobs/electric-hob", "Glaskeramikkochfeld") == "radiant"
    assert c("kitchen/cooking/hobs/electric-hob", "Induktionskochfeld") == "induction"
    assert c("kitchen/cooking/hobs/combohob", "Induktionskochfeld mit integriertem Abzug") == "induction"
    assert c("kitchen/cooking/hobs/combohob", "Kochfeld mit Abzug") == "radiant"
    assert c("kitchen/cooking/hobs/gas-hob", "Gaskochfeld") == "gas_cooktop"
    assert c("kitchen/cooking/cookers/electric-cooker", "Standherd mit Glaskeramik-Kochfeld") == "radiant"
    assert c("kitchen/cooking/cookers/electric-cooker", "Standherd mit Induktionskochfeld") == "induction"
    for none in ("kitchen/cooking/cooker-hoods/chimney-hood", "kitchen/cooling/refrigerators/x", "kitchen/dishwashing/x",
                 "kitchen/cooking/warming-drawers/warming-drawer", "laundry/laundry/washing-machines/x", ""):
        assert c(none, "Name") is None, none
    assert "gas_oven" not in a.SUPPORTED_SUBCATEGORIES and "otr" not in a.SUPPORTED_SUBCATEGORIES


def test_discover_uses_the_shop_api_and_prices_in_eur():
    sess = FakeApi({"hobs%2Finduction-hob": _j("listing_induction.json")})
    with fake_session(sess):
        got = a.discover("induction", 2)
    assert [c.model_number for c in got] == ["TK85IM0FRB", "TI84IF0FRB"]
    c = got[0]
    assert c.brand == "AEG" and (c.category, c.subcategory) == ("cooking", "induction")
    assert (c.region, c.country, c.currency) == ("eu", "de", "EUR") and c.price_usd is None
    assert c.price_local == 740.0  # shown selling price (offerPrice), not the 799 list price nor the 1598 UVP
    assert c.url == "https://www.aeg.de/kitchen/cooking/hobs/induction-hob/tk85im0frb/"
    assert c.attrs["fuel"] == "induction" and c.attrs["width_in"] == 32.7  # '83 cm' in the name
    q = sess.calls[0]
    assert "/external/commerce/ccv2/occ/DEU-AEG/products/search/" in q and "d2cSellable" in q
    assert "category:kitchen%2Fcooking%2Fhobs%2Finduction-hob" in q


def test_discover_splits_ovens_and_microwaves_by_the_shared_classifier():
    sess = FakeApi({"cooking%2Fovens": _j("listing_ovens.json")})
    with fake_session(sess):
        got = a.discover("electric_oven", 10)
    assert [c.model_number for c in got] == ["BPE53516AB", "V8SBP731AB"]
    assert "eu_class" not in got[0].attrs  # 'A+' belongs to the old scale: no EU class A..G fact from it
    sess = FakeApi({"cooking%2Fmicrowaves": _j("listing_microwaves.json")})
    with fake_session(sess):
        got = a.discover("microwave", 10)
    assert [c.model_number for c in got] == ["TB6SM171DB", "MFB295DB", "MFB252DB"]
    assert got[0].url.startswith("https://www.aeg.de/") and got[0].attrs.get("width_in") == 23.6
    with fake_session(FakeApi()):
        assert a.discover("radiant", 5) == []  # nothing listed -> empty, never an error


def test_candidate_rules():
    item = {"modelId": "XYZ123", "name": "Backofen 60 cm", "description": "Backofen / 59 x 56 cm / Weiß / A",
            "productURL": "https://www.aeg.de/kitchen/cooking/ovens/oven/xyz123/", "price": {"value": 500.0},
            "offerPrice": {"value": 0}, "categoryFallBack": {"categoryFallBackCode": "kitchen/cooking/ovens/oven"}}
    c = ec.eu_candidate(a.SITE, item, "electric_oven")
    assert c.price_local == 500.0 and c.attrs["eu_class"] == "A" and c.attrs["width_in"] == 23.6
    for patch in ({"productURL": "https://evil.example.com/x/"}, {"productURL": "http://www.aeg.de/x/"},
                  {"modelId": "bad id!"}, {"name": ""}):
        assert ec.eu_candidate(a.SITE, dict(item, **patch), "electric_oven") is None
    assert ec.eu_candidate(a.SITE, item, "induction") is None  # classified under another sub key
    assert ec.eu_price({"price": {"value": 5}, "offerPrice": {"value": 3}}) == 3.0
    assert ec.eu_price({"price": {"value": 0}}) is None


def _pdp(name, path):
    pdp = ec.parse_pdp(_t(f"pdp_{name}.html"))
    pdp["category_path"] = path
    return pdp


def test_oven_page_is_translated_and_complete():
    pdp = _pdp("BPE53516AB", "kitchen/cooking/ovens/oven")
    assert pdp["model"] == "BPE53516AB" and pdp["pnc"] == "944068463" and pdp["manual_url"].startswith("https://")
    rec, raw = a.parse_product("BPE53516AB", PDP + "ovens/oven/bpe53516ab/", pdp, 400.0)
    assert rec.subcategory == "electric_oven" and (rec.country, rec.currency, rec.price_local) == ("de", "EUR", 400.0)
    assert rec.price_usd is None and rec.region == "eu"
    assert (rec.height_in, rec.width_in, rec.depth_in) == (23.5, 23.4, 22.0)  # 598 x 594 x 560 mm
    assert rec.weight_lb == 70.3  # 31.9 kg net
    assert rec.finish_color == "Black"
    assert rec.extra_specs["Energy > EU energy class"] == "EU class A+"
    assert rec.extra_specs["Energy values > Energy efficiency class"] == "A+"  # German section/label -> English
    assert "Performance > Cleaning" in rec.extra_specs  # label translated (the value is not in the glossary: kept as is)
    assert rec.extra_specs["Technical data > Colour"] == "Black"
    assert len(raw) == 44 and len(rec.extra_specs) >= len(raw) - 2
    r = next(x for x in raw if x.key == "Energieeffizienzklasse")  # RawSpec keeps the German source text
    assert r.section == "Energiewerte" and r.value == "A+" and r.source == "web"
    assert rec.image_url == "https://electrolux.bynder.com/transform/WS_ZO2000/7925010f-cb6b-4639-973e-e812b813af1e/2024949145-eps"
    assert rec.pod_features and rec.energy_kwh_year is None  # kWh per cycle is not a yearly figure


def test_hob_and_microwave_pages():
    rec, raw = a.parse_product("TK85IM0FRB", PDP + "hobs/induction-hob/tk85im0frb/",
                               _pdp("TK85IM0FRB", "kitchen/cooking/hobs/induction-hob"), 740.0)
    assert rec.subcategory == "induction" and rec.width_in == 31.3 and rec.depth_in == 20.3 and rec.height_in is None
    assert rec.voltage_v == "220-240/400V2N" and rec.weight_lb == 27.8
    assert any(k.startswith("Performance > Kochzone") or "zone" in k.lower() for k in rec.extra_specs)
    rec, _ = a.parse_product("TB6SM171DB", PDP + "microwaves/microwave-oven/tb6sm171db/",
                             _pdp("TB6SM171DB", "kitchen/cooking/microwaves/microwave-oven"), 557.0)
    assert rec.subcategory == "microwave" and rec.width_in == 23.4 and rec.height_in == 14.6


def test_page_parsers_reject_other_pages():
    for bad in ("<html></html>", '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":'
                '{"pageData":{"components":[{"name":"PLPContainer"}]}}}}</script>'):
        try:
            ec.parse_pdp(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    pdp = _pdp("TB6SM171DB", "kitchen/cooking/microwaves/microwave-oven")
    try:
        a.parse_product("TB6SM171DB", "u", dict(pdp, sections=[]), None)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    secs = ec.parse_spec_sections(_t("pdp_BPE53516AB.html") + _t("pdp_BPE53516AB.html"))
    assert [t for t, _ in secs].count("Leistung") == 1  # desktop + mobile copies are merged


def test_model_from_url_validates_host_and_shape():
    assert ec.eu_model_from_url(a.SITE, PDP + "ovens/oven/bpe53516ab/") == ("BPE53516AB", "kitchen/cooking/ovens/oven")
    for bad in ("http://www.aeg.de/kitchen/cooking/ovens/oven/x1234/", "https://www.aeg.de.evil.com/kitchen/cooking/x/yyy/",
                "https://www.electrolux.de/kitchen/cooking/ovens/oven/bpe53516ab/", "https://www.aeg.de/laundry/x/yyyy/",
                "https://www.aeg.de/kitchen/cooking/"):
        try:
            a.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_scrape_takes_price_and_class_from_the_shop_item():
    listing = _j("listing_ovens.json")["products"][0]
    pdp_html = _t("pdp_BPE53516AB.html")
    sess = FakeApi({"relevance:code:944068463": {"products": [listing], "pagination": {"totalPages": 1}}}, pdp_html)
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append(wanted), [])[1]
    try:
        with fake_session(sess):
            rec, docs, raw = a.scrape(PDP + "ovens/oven/bpe53516ab/")
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "BPE53516AB" and rec.price_local == 400.0 and rec.subcategory == "electric_oven"
    assert [t for t, _ in seen[0]] == ["Manual", "EnergyGuide"] and all(u.startswith("https://") for _, u in seen[0])
    assert any("relevance:code:944068463" in c for c in sess.calls)
    # a page that shows another model is an error, not a mislabelled record
    with fake_session(FakeApi({}, pdp_html)):
        try:
            a.scrape(PDP + "ovens/oven/xyz12345/")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
