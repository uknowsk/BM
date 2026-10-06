"""Offline tests for the Monogram US adapter. Run: python tests/test_monogram_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import monogram_us as mono

FX = Path(__file__).parent / "fixtures" / "monogram_us"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _detail(sku):
    p = _j(f"detail_{sku}.json")
    return p, mono.parse_product(f"https://www.monogram.com/product/x/{p['id']}", p["fields"], mono._list_price(p["fields"]))


def test_supported():
    assert mono.SUPPORTED_SUBCATEGORIES == {"sco", "electric_oven", "microwave", "induction", "gas_oven", "gas_cooktop"}
    assert mono.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.supported("Monogram", "us") == mono.SUPPORTED_SUBCATEGORIES
    for sub, (major, cats) in mono.SUB_SOURCES.items():
        assert catalog.major_of(sub) == major and all(c in mono.CATEGORIES for c in cats)


def test_classify():
    f = lambda name, typ, cats="Monogram>Cooking>X", **kw: {  # noqa: E731
        "BWC_Product_Marketing_Description__c": name, "BWC_Product_Type__c": typ, "BWC_Product_Category__c": cats,
        "Is_Accessory__c": "No", **kw}
    assert mono.classify(f('Monogram 36" All Gas Professional Range', "Professional Range")) == "gas_oven"
    assert mono.classify(f('Monogram 30" Dual-Fuel Professional Range', "Professional Range")) == "gas_oven"
    assert mono.classify(f('Monogram 36" Induction Professional Range', "Professional Range")) == "induction"
    assert mono.classify(f('Monogram 30" Induction Cooktop', "Cooktop")) == "induction"
    assert mono.classify(f('Monogram 36" Professional Gas Rangetop', "Rangetop")) == "gas_cooktop"
    assert mono.classify(f('Monogram 30" Statement Single Wall Oven', "Single Oven")) == "electric_oven"
    assert mono.classify(f('Monogram 30" Five-in-One Wall Oven with 120V Advantium', "Built In Ovens")) == "sco"
    assert mono.classify(f("Monogram Statement Above-the-Cooktop Speedcooking Oven", "Above the Cooktop Ovens")) == "sco"
    assert mono.classify(f("Monogram Built-In Microwave", "Built-In Microwave")) == "microwave"
    assert mono.classify(f("Monogram 27\" Warming Drawer", "Warming Drawers")) is None
    assert mono.classify(f("Range Hood", "Ventilation")) is None
    assert mono.classify(f("Monogram Gas Rangetop", "Rangetop", Is_Accessory__c="Yes")) is None
    assert mono.classify(f("Monogram Counter-Depth French-Door Refrigerator", "French-Door Refrigerator", "Monogram>Refrigeration>X")) is None


def test_listing_filters_on_status_and_visibility():
    assert mono._listed({"Status__c": "Active", "BWC_PLPIsVisible__c": "true", "BWC_Brand__c": "Monogram"})
    assert mono._listed({"Status__c": "Active", "BWC_PLPIsVisible__c": "false", "MGM_PLPIsVisible__c": "true"})
    assert not mono._listed({"Status__c": "Obsolete", "BWC_PLPIsVisible__c": "true"})
    assert not mono._listed({"Status__c": "Active", "BWC_PLPIsVisible__c": "false", "MGM_PLPIsVisible__c": "false"})
    assert mono._list_price({"UMRP__c": "8500.0"}) == 8500.0
    assert mono._list_price({"UMRP__c": None}) is None and mono._list_price({"UMRP__c": "0"}) is None


def test_discover_with_fixture_api():
    cat, page = _j("category_pro_ranges.json"), _j("products_page.json")
    calls = []

    def fake_get(path, params):
        calls.append((path, params))
        if path == "/search/products":  # same product ids for every category; only the category name follows the request
            name = {v: k for k, v in mono.CATEGORIES.items()}[params["categoryId"]]
            return dict(cat, categories={"category": {"name": name}})
        return page

    orig = mono._get
    mono._get = fake_get
    try:
        got = mono.discover("gas_oven", limit=50)
        assert got and all((c.brand, c.category, c.subcategory) == ("Monogram", "cooking", "gas_oven") for c in got)
        assert all(c.url.startswith("https://www.monogram.com/product/monogram") for c in got)
        assert len({c.model_number for c in got}) == len(got) and any(c.price_usd for c in got)
        assert len(mono.discover("gas_oven", limit=1)) == 1
        ind = mono.discover("induction", limit=50)
        assert all(c.subcategory == "induction" for c in ind)
        # requests ask only for the explicit customer-facing field list, never internal cost fields
        fields = [p[1]["fields"] for p in calls if p[0] == "/products"]
        assert fields and all("Cost" not in f and "DAC__c" not in f for f in fields)
        try:
            mono.discover("french_door")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    finally:
        mono._get = orig


def test_category_name_is_verified():
    orig = mono._get
    mono._get = lambda path, params: {"categories": {"category": {"name": "Something Else"}}, "productsPage": {"products": [], "total": 0}}
    try:
        mono._category_product_ids("Professional Ranges")
    except mono.MonogramError:
        return
    finally:
        mono._get = orig
    raise AssertionError("expected MonogramError")


def test_gas_range_record():
    _, (r, raw, docs) = _detail("ZGP366NTSS")
    assert (r.brand, r.category, r.subcategory, r.price_usd) == ("Monogram", "cooking", "gas_oven", 8500.0)
    assert (r.width_in, r.height_in, r.depth_in, r.capacity_total_cuft) == (35.875, 35.25, 28.5, 6.2)
    assert r.image_url.startswith("https://products-salsify.geappliances.com/")
    assert len(r.extra_specs) > 50 and all(x.brand == "Monogram" for x in raw)
    assert any(k.startswith("Appearance > ") for k in r.extra_specs)
    assert [t for t, _ in docs] == ["Manual", "QuickSpecs", "Installation"]
    assert not any("Cost" in k or "DAC" in k for k in r.extra_specs)


def test_other_records():
    p, (r, _, _) = _detail("ZHU36RSTSS")
    assert r.subcategory == "induction" and r.price_usd == 3600.0
    assert mono._attrs(p["fields"]) == {"finish": "Silver", "wifi": True}
    p, (r, _, _) = _detail("ZGU366NTSS")
    assert r.subcategory == "gas_cooktop" and mono._attrs(p["fields"])["wifi"] is False  # 'Not Connectable'
    _, (r, _, _) = _detail("ZSB9132VSS")
    assert r.subcategory == "sco" and r.capacity_total_cuft == 1.7


def test_product_id_from_url():
    assert mono.product_id_from_url("https://www.monogram.com/product/monogramzgp366ntss/01t4P00000BX6NSQA1") == "01t4P00000BX6NSQA1"
    for bad in ("http://www.monogram.com/product/x/01t4P00000BX6NSQA1", "https://evil.example.com/product/x/01t4P00000BX6NSQA1",
                "https://www.monogram.com/category/cooking/0ZGKe000000XZBtOAO", "https://www.monogram.com/product/x/short"):
        try:
            mono.product_id_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_get_rejects_offsite_and_redirect():
    class R:
        def __init__(self, url, status):
            self.url, self.status_code, self.text = url, status, ""

    orig = mono.requests.get
    mono.time.sleep = lambda s: None
    try:
        for resp in (R("https://evil.example.com/x", 200), R("https://www.monogram.com/x", 302)):
            mono.requests.get = lambda *a, **k: resp
            try:
                mono._get("/products", {})
            except mono.MonogramError:
                continue
            raise AssertionError("expected MonogramError")
    finally:
        mono.requests.get = orig


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
