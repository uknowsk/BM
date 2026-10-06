"""Plain-assert offline tests (saved fixtures). Run: python tests/test_amana_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import amana_us as am
import catalog

FX = Path(__file__).parent / "fixtures" / "amana_us"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _rec(m):
    prod = _j(f"prod_{m}.json")
    return am.parse_product(m, am.BASE + prod["url"], prod)


def test_contract_and_scope():
    assert (am.COUNTRY, am.REGION, am.CURRENCY, am.BRAND) == ("us", "na", "USD", "Amana")
    assert catalog.brand_slug(am.BRAND) + "_us" == "amana_us"
    assert am.SUPPORTED_SUBCATEGORIES == {"otr", "gas_oven", "radiant"}  # Amana sells no wall ovens / cooktops / countertop
    assert am.SITE.occ == "/ws/v2/amana-us"
    for bad in ("microwave", "sco", "gas_cooktop", "induction", "electric_oven", "french_door", "top_load", "nope"):
        try:
            am.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_search_by_sub():
    data = _j("search_all.json")
    got = {s: [c.model_number for c in am.parse_search(data, s)] for s in am.SUPPORTED_SUBCATEGORIES}
    assert got["otr"] == ["AMMS2230TW", "AMV2307PFW"]  # UMV1170LS is Unbranded: not an Amana product
    assert got["gas_oven"] == ["AGR6603SMS", "AGG222VDW", "AGR6303MMS", "AGR4203MNS", "AGR5330BAB", "AGR6603SFB"]
    assert {"AER6303MMS", "AFCS2530TS", "ACR4303MFW"} <= set(got["radiant"]) and not set(got["radiant"]) & set(got["gas_oven"])
    everything = {c for v in got.values() for c in v}
    for hood in ("WVW75UC6DS", "UXW7324BSS", "UXD8630DYS"):  # Whirlpool / Unbranded hoods listed on amana.com
        assert hood not in everything
    assert "ART104TFDB" not in everything and "NTW4519JW" not in everything  # fridge / washer: out of scope
    c = {x.model_number: x for x in am.parse_search(data, "gas_oven")}["AGR6603SMS"]
    assert (c.brand, c.category, c.subcategory, c.price_usd) == ("Amana", "cooking", "gas_oven", 779.0)
    assert c.url.startswith("https://www.amana.com/cooking/ranges/")


def test_records():
    r, raw = _rec("AGR6603SMS")
    assert (r.brand, r.category, r.subcategory, r.price_usd, r.capacity_total_cuft) == ("Amana", "cooking", "gas_oven", 779.0, 5.0)
    assert (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (29.875, 46.25, 27.2, 163.0)
    assert len(r.extra_specs) == len(raw) == 80 and all(" > " in k for k in r.extra_specs)
    assert r.image_url.startswith("https://www.amana.com/is/image/")
    r, _ = _rec("AER6303MMS")
    assert (r.subcategory, r.capacity_total_cuft, r.voltage_v) == ("radiant", 4.8, "120")
    r, _ = _rec("AMMS2230TW")
    assert (r.subcategory, r.capacity_total_cuft, r.finish_color) == ("otr", 1.7, "White")
    listing = {c.model_number: c.subcategory for s in am.SUPPORTED_SUBCATEGORIES
               for c in am.parse_search(_j("search_all.json"), s)}
    for m in ("AGR6603SMS", "AER6303MMS", "AMMS2230TW"):
        assert _rec(m)[0].subcategory == listing[m], m


def test_docs_model_url_hosts():
    docs = am.parse_docs(_j("docs_AGR6603SMS.json"))
    assert [t for t, _ in docs] == ["Manual", "SpecSheet", "Installation", "QuickSpecs"]
    assert all(u.startswith("https://www.amana.com/") for _, u in docs)
    assert am.model_from_url("https://www.amana.com/cooking/ranges/p.30-inch-gas-range.agr6603sms.html") == "AGR6603SMS"
    for bad in ("https://www.whirlpool.com/p.a.agr6603sms.html", "http://www.amana.com/p.a.agr6603sms.html"):
        try:
            am.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert callable(am.reset_browser_mode) and am._modes() in ([True, False], [False, True])


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
