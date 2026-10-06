"""Offline tests for beko_uk (trimmed saved beko.co.uk HTML; no network). Run: python tests/test_beko_uk.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import beko_uk as b
import catalog

FIX = Path(__file__).parent / "fixtures" / "beko_uk"
read = lambda n: (FIX / n).read_text(encoding="utf-8")
URL = "https://www.beko.co.uk/appliances/cooking/range-cookers/product/x"


def subs_of(fixture: str) -> dict[str, list[str]]:
    items, _ = b.parse_listing(read(fixture))
    out: dict[str, list[str]] = {}
    for it in items:
        for sub in b.SUPPORTED_SUBCATEGORIES:
            if b.item_to_candidate(it, sub):
                out.setdefault(sub, []).append(it["sku"])
    return out


def test_contract():
    assert (b.COUNTRY, b.REGION, b.CURRENCY) == ("uk", "eu", "GBP")
    assert b.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"}
    assert b.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.module_name("Beko", "uk") == "beko_uk" and catalog.supported("Beko", "uk") == b.SUPPORTED_SUBCATEGORIES
    for s, paths in b.SUB_SOURCES.items():
        assert paths and all(p.startswith("appliances/") for p in paths), s
    try:
        b.discover("french_door")
    except ValueError:
        pass
    else:
        raise AssertionError("fridge keys must raise ValueError")


def test_classify():
    c = b.classify
    assert c("Built-in Combination Microwave Oven with Grill BMCB25433X") == "sco"
    assert c("45cm Built-In Compact Multi-Function Oven Microwave BBCW17401") == "sco"
    assert c("Built-in Microwave with Grill BMGB25332BG") == "microwave"
    assert c("900W 25 Litre Digital Combination Microwave MCF25310") == "microwave"
    assert c("60cm Double Oven Electric Cooker EDC6731") == "radiant"
    assert c("50cm Freestanding Gas Twin Cavity Cooker EDG507") == "gas_oven"
    assert c("90cm Double Oven Range Cooker with 5 Burner Gas Hob KDVF90") == "gas_oven"
    assert c("100cm Double Oven Range Cooker with 5 Zone Ceramic Hob KDVC100") == "radiant"
    assert c("90cm Double Oven Range Cooker with 5 Zone Induction Hob KDVI90") == "induction"
    assert c("60cm Gas Hob with Side Knob Controls CIHYG21") == "gas_cooktop"
    assert c("30cm Induction Hob with Touch Controls HDMI32401DT") == "induction"
    assert c("60cm Sealed Plate Hob with Side Knob Controls HIBE64101") == "radiant"
    assert c("60cm Built-In Single Fan Oven with AeroPerfect BBIF12311") == "electric_oven"
    assert c("60cm Cooker Hood") is None and c("Freestanding Fridge Freezer") is None


def test_listings_are_exclusive_and_correct():
    seen: dict[str, str] = {}
    for fx in ("list_microwaves.html", "list_integrated_microwaves.html", "list_ovens.html", "list_hobs.html",
               "list_freestanding.html", "list_range_cookers.html"):
        for sub, skus in subs_of(fx).items():
            for s in skus:
                assert seen.setdefault(s, sub) == sub, (s, sub, seen[s])   # one product, one sub key
    assert seen["BMCB25433X"] == "sco" and seen["BBCW18411"] == "sco" and seen["MOC20240"] == "microwave"
    assert seen["KDVF90"] == "gas_oven" and seen["KDVI90"] == "induction" and seen["HIAW64225S"] == "gas_cooktop"
    assert seen["EDG507"] == "gas_oven" and seen["BBIF12311"] == "electric_oven"
    items, total = b.parse_listing(read("list_ovens.html"))
    assert total == 43 and len(items) == 12
    c = b.item_to_candidate(next(i for i in items if i["sku"] == "BBIF12311"), "electric_oven")
    assert c.price_usd is None and c.price_local is None and c.currency == "GBP" and c.country == "uk" and c.region == "eu"
    assert c.attrs["width_in"] == 23.6 and c.attrs["fuel"] == "electric" and c.url.startswith("https://www.beko.co.uk/appliances/")


def test_product_range_cooker():
    rec, raw, docs = b.parse_product(read("pdp_range_cooker.html"), URL)
    assert rec.model_number == "KDVF90" and rec.subcategory == "gas_oven" and rec.price_local is None
    assert (rec.width_in, rec.height_in, rec.depth_in) == (35.43, 35.43, 23.62) and rec.weight_lb == 170.9
    assert rec.capacity_total_cuft is None   # several cavities: not summed
    assert rec.extra_specs["Hob Features > Fuel"] == "Gas" and rec.extra_specs["Energy > EU energy class"] == "EU class A"
    assert any(r.section == "Burners/Heating zones" and r.key == "Left (Front)" and r.value == "3 Kw" for r in raw)
    assert [t for t, _ in docs] == ["EnergyGuide", "SpecSheet"] and all(u.startswith("https://storage.beko.co.uk/") for _, u in docs)
    assert rec.image_url.startswith("https://storage.beko.co.uk/")


def test_product_microwaves_and_hob():
    rec, raw, docs = b.parse_product(read("pdp_oven_microwave.html"), URL)
    assert rec.subcategory == "sco" and rec.model_number == "BBCW17401" and rec.capacity_total_cuft == 1.7
    mw, _, mdocs = b.parse_product(read("pdp_microwave.html"), URL)
    assert mw.subcategory == "microwave" and mw.extra_specs["Microwave Features > Microwave Power ( watts)"] == "800"
    assert mw.capacity_total_cuft == 0.71 and mdocs == []   # manual lives on a non-allowed host
    hob, _, _ = b.parse_product(read("pdp_gas_hob.html"), URL)
    assert hob.subcategory == "gas_cooktop" and hob.width_in == 22.83 and hob.extra_specs["Hob Features > Fuel"] == "Gas"


def test_unit_parsing():
    assert b._mm("1790") == 1790 and b._mm("203.5 centimeters") == 2035 and b._mm("hX560X490") == 560
    assert b._mm("") is None


def test_security():
    for bad in ("http://www.beko.co.uk/appliances/a/product/x", "https://evil.com/appliances/a/product/x",
                "https://www.beko.co.uk/about"):
        try:
            b._check_product_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    page = '<a href="https://evil.com/x.pdf">a</a><a href="https://bekoplc.blob.core.windows.net/m.pdf">m</a>'
    assert b.doc_links(page) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
