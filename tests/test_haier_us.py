"""Offline tests for the Haier US adapter (haier_us on the cafe_us core). Run: python tests/test_haier_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cafe_us as core
import catalog
import haier_us as haier

FX = Path(__file__).parent / "fixtures" / "haier_us"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_supported():
    assert haier.SUPPORTED_SUBCATEGORIES == {"otr", "gas_oven", "radiant"}
    assert haier.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.supported("Haier", "us") == haier.SUPPORTED_SUBCATEGORIES
    assert (haier.COUNTRY, haier.REGION, haier.CURRENCY) == ("us", "na", "USD")


def test_discover_with_fixture_pages():
    pages = {"Haier>Kitchen>Cooking>Ranges>Gas Ranges": _j("ss_gas_ranges.json"),
             "Haier>Kitchen>Cooking>Microwaves>Over-the-Range Microwave Ovens": _j("ss_otr.json")}
    orig_page, orig_sleep = core._search_page, core.time.sleep
    core._search_page = lambda site, path, n: pages[path]
    core.time.sleep = lambda s: None
    try:
        gas = haier.discover("gas_oven", limit=10)
        assert gas and all((c.brand, c.category, c.subcategory) == ("Haier", "cooking", "gas_oven") for c in gas)
        assert all(c.url.startswith("https://www.haierappliances.com/appliance/") for c in gas)
        otr = haier.discover("otr", limit=10)
        assert {c.model_number for c in otr} >= {"HMV1472BHS"} and all(c.price_usd for c in otr)
        try:
            haier.discover("french_door")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    finally:
        core._search_page, core.time.sleep = orig_page, orig_sleep


def test_parse_records():
    r, raw, docs = core.parse_product(haier.HAIER, "https://www.haierappliances.com/appliance/X-QGAS740RMSS", _j("po_QGAS740RMSS.json"))
    assert (r.brand, r.category, r.subcategory, r.price_usd) == ("Haier", "cooking", "gas_oven", 1449.0)
    assert r.capacity_total_cuft == 2.9 and r.width_in == 24.0
    assert all(x.brand == "Haier" for x in raw) and len(r.extra_specs) > 40
    assert [t for t, _ in docs] == ["Manual", "QuickSpecs", "Installation"]
    r = core.parse_product(haier.HAIER, "https://www.haierappliances.com/appliance/X-HMV1472BHS", _j("po_HMV1472BHS.json"))[0]
    assert r.subcategory == "otr" and r.price_usd == 499.0 and r.capacity_total_cuft == 1.4
    # a French-door fridge page is outside the cooking scope
    po = dict(_j("po_QGAS740RMSS.json"), category=["Haier/Kitchen/Refrigeration/Refrigerators/French Door Refrigerators"])
    try:
        core.parse_product(haier.HAIER, "https://www.haierappliances.com/appliance/X-QJS15HYRFS", po)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_url_check():
    assert core.model_from_url(haier.HAIER, "https://www.haierappliances.com/appliance/24-Range-QGAS740RMSS") == "QGAS740RMSS"
    try:
        core.model_from_url(haier.HAIER, "https://www.cafeappliances.com/appliance/24-Range-QGAS740RMSS")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
