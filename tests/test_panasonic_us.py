"""Offline tests for panasonic_us (trimmed saved Shopify JSON/HTML; no network). Run: python tests/test_panasonic_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import panasonic_us as p

FIX = Path(__file__).parent / "fixtures" / "panasonic_us"
read = lambda n: (FIX / n).read_text(encoding="utf-8")
URL = "https://shop.panasonic.com/products/x"


def test_contract():
    assert (p.COUNTRY, p.REGION, p.CURRENCY) == ("us", "na", "USD")
    assert p.SUPPORTED_SUBCATEGORIES == {"microwave", "sco"} <= set(catalog.sub_keys("cooking"))
    assert catalog.module_name("Panasonic", "us") == "panasonic_us"
    try:
        p.discover("otr")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported key must raise")


def test_classify():
    c = p.classify
    assert c("1.3 cu. ft. Countertop Microwave Oven, 1100W - NN-SU696S") == "microwave"
    assert c("HomeCHEF® Connect 4-in-1 Multi-oven, 1.2 cu. ft., 1000W") == "sco"
    assert c("1.3 Cu. Ft. HomeMADE Countertop Inverter Microwave Multi-Oven with Convection Air Frying") == "sco"
    assert c("Microwave Trim Kit 30 in") is None and c("Cordless Telephone") is None
    assert p.model_of("1.3 cu. ft. Microwave Oven, 1100W - NN-SU696S", "X") == "NN-SU696S" and p.model_of("none", "SKU1") == "SKU1"


def test_candidates():
    prods = json.loads(read("collection_microwave_ovens.json"))["products"]
    mw = [c for x in prods if (c := p.product_to_candidate(x, "microwave"))]
    sco = [c for x in prods if (c := p.product_to_candidate(x, "sco"))]
    assert len(mw) == 25 and [c.model_number for c in sco] == ["NN-CV88QS", "NN-CD66NS", "NN-CD65NS"]
    c = next(x for x in mw if x.model_number == "NN-SU696S")
    assert c.price_usd == 134.95 and c.attrs["capacity_total_cuft"] == 1.3 and c.url.startswith("https://shop.panasonic.com/products/")
    assert c.category == "cooking" and c.subcategory == "microwave" and c.country == "us" and c.currency == "USD"
    assert p.product_to_candidate({"title": "Microwave Oven", "handle": "../x", "variants": [{}]}, "microwave") is None


def test_product():
    rec, raw, manual = p.parse_product(read("pdp_microwave.html"), URL)
    assert rec.model_number == "NN-SU696S" and rec.subcategory == "microwave" and rec.price_usd == 134.95
    assert (rec.width_in, rec.height_in, rec.depth_in) == (20 + 7 / 16, 12 + 3 / 8, 16 + 1 / 8) and rec.weight_lb == 35.3
    assert rec.capacity_total_cuft == 1.3 and rec.extra_specs["Specifications > Microwave power (W)"] == "1100"
    assert rec.image_url.startswith("https://shop.panasonic.com/") and rec.pod_features and not rec.pod_features[0].startswith("IMPORTANT")
    assert manual.startswith("https://help.na.panasonic.com/") and raw and all(r.source == "web" for r in raw)
    rec2, _, _ = p.parse_product(read("pdp_multi_oven.html"), URL)
    assert rec2.subcategory == "sco" and rec2.model_number == "NN-CV88QS" and rec2.weight_lb == 39.1


def test_security():
    for bad in ("http://shop.panasonic.com/products/x", "https://evil.com/products/x", "https://shop.panasonic.com/cart"):
        try:
            p._check_product_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert p._https("http://shop.panasonic.com/a.jpg") == "https://shop.panasonic.com/a.jpg"
    assert p._https("https://evil.com/a.jpg") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
