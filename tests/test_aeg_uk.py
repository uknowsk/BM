"""Plain-assert offline tests (saved fixtures). Run: python tests/test_aeg_uk.py
aeg_uk: English product pages (no translation), GBP prices, gas hobs and microwave-combination ovens."""
import contextlib
import json
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import aeg_uk as a
import catalog

FX = Path(__file__).parent / "fixtures" / "aeg_uk"
PDP = "https://www.aeg.co.uk/kitchen/cooking/"
EMPTY = {"products": [], "pagination": {"totalPages": 0, "totalResults": 0}}


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


class FakeApi:
    def __init__(self, by_key=None, html=""):
        self.by_key, self.html, self.calls = by_key or {}, html, []

    def json(self, url):
        self.calls.append(url)
        for key, val in self.by_key.items():
            if key in url:
                return val
        return EMPTY

    def rendered(self, url, ready, timeout_ms=0):
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
    assert a.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "electric_oven", "gas_cooktop", "induction", "radiant"}
    assert a.SUPPORTED_SUBCATEGORIES <= set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert (a.BRAND, a.COUNTRY, a.REGION, a.CURRENCY) == ("AEG", "uk", "eu", "GBP")
    assert catalog.module_name("AEG", "uk") == "aeg_uk"
    try:
        a.discover("otr")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_gas_hobs_and_induction_hobs_in_gbp():
    sess = FakeApi({"hobs%2Fgas-hob": _j("listing_gas.json")})
    with fake_session(sess):
        got = a.discover("gas_cooktop", 5)
    assert [c.model_number for c in got] == ["HGE64200SM", "HKB95820NB", "HKB75820NB"]
    c = got[0]
    assert (c.country, c.currency, c.price_local, c.price_usd) == ("uk", "GBP", 189.99, None)
    assert c.attrs == {"fuel": "gas", "width_in": 23.6} and c.subcategory == "gas_cooktop"
    assert "occ/GBR-AEG/products/search/" in sess.calls[0]
    with fake_session(FakeApi({"hobs%2Finduction-hob": _j("listing_induction.json")})):
        got = a.discover("induction", 5)
    assert [c.model_number for c in got] == ["TO64IB00FZ", "TN64IA00FB", "IKX84443CB"]


def test_microwave_combination_ovens_are_sco_and_plain_microwaves_stay_microwave():
    with fake_session(FakeApi({"cooking%2Fovens": _j("listing_ovens.json")})):
        sco = a.discover("sco", 10)
        ovens = a.discover("electric_oven", 10)
    assert [c.model_number for c in sco] == ["OK6NK40K", "OK6NK40M"]
    assert [c.model_number for c in ovens] == ["BEX335011B", "BPX535061B"]
    assert not {c.model_number for c in sco} & {c.model_number for c in ovens}
    with fake_session(FakeApi({"cooking%2Fmicrowaves": _j("listing_microwaves.json")})):
        mw = a.discover("microwave", 10)
    assert [c.model_number for c in mw] == ["OB6SM171DB", "OB6SM261UB"]  # OK6NK40K is a combination oven (sco)


def _pdp(name, path):
    pdp = ec.parse_pdp(_t(f"pdp_{name}.html"))
    pdp["category_path"] = path
    return pdp


def test_english_page_needs_no_translation():
    rec, raw = a.parse_product("BEX335011B", PDP + "ovens/oven/bex335011b/",
                               _pdp("BEX335011B", "kitchen/cooking/ovens/oven"), 329.99)
    assert rec.subcategory == "electric_oven" and (rec.country, rec.currency, rec.price_local) == ("uk", "GBP", 329.99)
    assert rec.wifi_supported is False and rec.wifi_evidence == "Connectivity: No"
    assert (rec.height_in, rec.width_in, rec.depth_in) == (23.4, 23.4, 22.4) and rec.weight_lb == 61.5  # 27.9 kg net
    assert rec.finish_color == "Black"
    assert rec.extra_specs["Performance > Main Oven - Internal Capacity (L)"] == "72"
    assert rec.extra_specs["Energy > Energy Rating"] == "A" and rec.extra_specs["Energy > EU energy class"] == "EU class A"
    assert rec.extra_specs["Installation > Built-in Dimensions (mm) (HxWxD)"] == "590x560x550"
    assert len(raw) == 36 and raw[0].section and raw[0].source == "web"
    assert rec.image_url.startswith("https://electrolux.bynder.com/transform/WS_PN/")
    assert rec.pod_features[0] == "Electronic touch controls."


def test_gas_hob_and_combination_oven_pages():
    rec, _ = a.parse_product("HGE64200SM", PDP + "hobs/gas-hob/hge64200sm/",
                             _pdp("HGE64200SM", "kitchen/cooking/hobs/gas-hob"), 189.99)
    assert rec.subcategory == "gas_cooktop" and rec.width_in == 23.4 and rec.depth_in == 20.1 and rec.weight_lb == 22.5
    assert rec.extra_specs["Performance > Number of Cooking Zones"] == "4"
    # same inputs as the listing (category 'ovens/compact-oven' + name) -> sco, whatever the url path says
    rec, _ = a.parse_product("OK6NK40K", PDP + "microwaves/microwave-oven/ok6nk40k/",
                             _pdp("OK6NK40K", "kitchen/cooking/ovens/compact-oven"), 649.99)
    assert rec.subcategory == "sco" and rec.product_name == "6000 CombiQuick Microwave and Built-in Oven"


def test_scrape_flow_with_shop_item():
    item = _j("listing_microwaves.json")["products"][1]  # OK6NK40K, category ovens/compact-oven
    pdp_html = _t("pdp_OK6NK40K.html")
    pnc = ec.parse_pdp(pdp_html)["pnc"]
    sess = FakeApi({f"relevance:code:{pnc}": {"products": [item], "pagination": {"totalPages": 1}}}, pdp_html)
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: []
    try:
        with fake_session(sess):
            rec, docs, raw = a.scrape(PDP + "microwaves/microwave-oven/ok6nk40k/")
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "OK6NK40K" and rec.subcategory == "sco" and rec.price_local == 649.99 and raw
    for bad in ("https://www.aeg.de/kitchen/cooking/ovens/oven/bex335011b/", "https://www.aeg.co.uk/laundry/x/yyyy/"):
        try:
            a.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
