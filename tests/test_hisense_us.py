"""Offline tests for hisense_us (saved, trimmed Wix warmup payloads; no network). Run: python tests/test_hisense_us.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import hisense_us as h

FIX = Path(__file__).parent / "fixtures" / "hisense_us"
read = lambda n: (FIX / n).read_text(encoding="utf-8")
URL = "https://www.hisense-usa.com/product-page/x"


def test_contract():
    assert (h.COUNTRY, h.REGION, h.CURRENCY) == ("us", "na", "USD")
    assert h.SUPPORTED_SUBCATEGORIES == {"microwave", "otr", "gas_oven", "radiant"}
    assert h.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.module_name("Hisense", "us") == "hisense_us"
    try:
        h.discover("french_door")
    except ValueError:
        pass
    else:
        raise AssertionError("fridge keys must raise ValueError")


def test_classify():
    c = h.classify
    assert c("1.7 cu. ft. Over-the-Range Microwave") == "otr"
    assert c("Hisense 0.9 cu. ft. Flatbed Microwave Oven") == "microwave"
    assert c("5.8 Cu. Ft. 6 Burners Slide-in Gas Range") == "gas_oven"
    assert c("6.0 cu. ft. Freestanding Electric Range") == "radiant"
    assert c("30 in Induction Range") == "induction"
    assert c("36 in Gas Cooktop") == "gas_cooktop" and c("30 in Electric Cooktop") == "radiant"
    assert c("Hisense 55 Class U7 TV") is None and c("Gas conversion kit") is None


def test_listing_and_candidates():
    items, total = h.parse_listing(read("list_ranges.html"))
    assert total == 6 and len(items) == 6
    got = {s: [c.model_number for i in items if (c := h.item_to_candidate(i, s))] for s in ("gas_oven", "radiant", "otr")}
    assert got["gas_oven"] == ["HBG3401NAS", "HFG3601CPS", "HBG3601CPS"] and got["otr"] == []
    c = h.item_to_candidate(items[0], "gas_oven")
    assert c.price_usd is None and c.attrs["fuel"] == "gas" and c.attrs["capacity_total_cuft"] == 6.0
    assert c.url.startswith("https://www.hisense-usa.com/product-page/") and c.country == "us" and c.category == "cooking"
    mw, _ = h.parse_listing(read("list_microwaves.html"))
    assert [x.model_number for i in mw if (x := h.item_to_candidate(i, "otr"))] == ["HMVZ173SS"]
    assert h._price({"price": 10.5}) is None and h._price({"price": 0}) is None and h._price({"price": 899}) == 899.0


def test_product_range_and_docs():
    rec, raw, docs = h.parse_product(read("pdp_gas_range.html"), URL)
    assert rec.model_number == "HBG3601CPS" and rec.subcategory == "gas_oven" and rec.price_usd is None
    assert rec.image_url.startswith("https://static.wixstatic.com/media/") and rec.pod_features
    assert any(k.startswith("Key features > ") for k in rec.extra_specs)
    types = [t for t, _ in docs]
    assert "SpecSheet" in types and "Warranty" in types and all(u.startswith("https://www.hisense-usa.com/_files/ugd/") for _, u in docs)
    assert raw and all(r.source == "web" for r in raw)


def test_product_specs_table():
    rec, raw, _ = h.parse_product(read("pdp_with_specs.html"), URL)
    assert (rec.width_in, rec.depth_in, rec.height_in, rec.weight_lb) == (35.9, 33.3, 70.3, 320.0)
    assert rec.extra_specs["Features > Ice Maker"] == "Installed"
    assert any(r.section == "Dimensions" and r.key == "Net weight" for r in raw)


def test_security():
    for bad in ("http://www.hisense-usa.com/product-page/x", "https://evil.com/product-page/x",
                "https://www.hisense-usa.com/category/x"):
        try:
            h._check_product_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert h._wix_doc("wix:document://v1/5e5698_ab.pdf/n.pdf").endswith("/_files/ugd/5e5698_ab.pdf")
    assert h._wix_doc("wix:document://v1/../../x.pdf/n.pdf") is None


def test_ribbon_new_signal():
    items, _ = h.parse_listing(read("list_ranges.html"))
    c = h.item_to_candidate(items[0], "gas_oven")
    assert "is_new" not in c.attrs  # empty ribbon = not flagged
    flagged = dict(items[0], ribbon="New")
    c = h.item_to_candidate(flagged, "gas_oven")
    assert c.attrs["is_new"] is True and c.attrs_src["is_new"] == "listing" and c.attrs_src["fuel"] == "name"
    assert h._ribbon_is_new({"additionalRibbons": [{"text": "NEW ARRIVAL"}]}) and not h._ribbon_is_new({"ribbon": "Sale"})
    html = read("pdp_with_specs.html")
    rec, _raw, _docs = h.parse_product(html, "https://www.hisense-usa.com/product-page/x")
    assert rec.is_new is None and rec.rating is None and rec.review_count is None and rec.release_date is None
    rec2, _raw, _docs = h.parse_product(html.replace('"slug"', '"ribbon": "New", "slug"', 1), "https://www.hisense-usa.com/product-page/x")
    assert rec2.is_new is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
