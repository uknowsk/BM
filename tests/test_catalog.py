"""Plain-assert tests (pytest-compatible). Run: python tests/test_catalog.py"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
from catalog import Candidate


def test_tree_keys_and_labels():
    t = catalog.CATEGORY_TREE
    assert list(t) == ["refrigerator", "washer", "cooking"]
    assert t["refrigerator"]["label_ko"] == "냉장고"
    assert list(t["refrigerator"]["children"]) == ["french_door", "side_by_side", "top_freezer",
                                                   "bottom_freezer", "built_in", "compact"]
    assert list(t["washer"]["children"]) == ["top_load", "front_load", "dryer", "laundry_center"]
    assert list(t["cooking"]["children"]) == ["microwave", "sco", "otr", "gas_oven", "gas_cooktop",
                                              "electric_oven", "induction", "radiant"]
    assert t["cooking"]["children"]["sco"] == catalog.SCO_LABEL_KO == "SCO (스피드쿡 오븐)"  # defined in one place
    assert not hasattr(catalog, "SCR_LABEL_KO") and "scr" not in t["cooking"]["children"]
    keys = catalog.sub_keys()
    assert len(keys) == len(set(keys)) == 18


def test_lookups():
    assert catalog.major_of("induction") == "cooking" and catalog.major_of("nope") is None
    assert catalog.label_ko("dryer") == "건조기" and catalog.label_ko("washer") == "세탁기"
    assert catalog.is_major("washer") and not catalog.is_major("dryer")
    assert catalog.is_sub("dryer") and not catalog.is_sub("washer")
    assert catalog.expand("washer") == ["top_load", "front_load", "dryer", "laundry_center"]
    assert catalog.expand("sco") == ["sco"]
    assert not catalog.is_sub("scr") and catalog.major_of("scr") is None
    try:
        catalog.expand("scr")  # retired key: unknown now
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        catalog.expand("vacuum")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_normalize_major():
    n = catalog.normalize_major
    assert n("Refrigerator") == "refrigerator" and n("washer") == "washer"
    assert n("냉장고") == "refrigerator" and n("Washing Machine") == "washer" and n("조리기기") == "cooking"
    assert n("") is None and n(None) is None and n("vacuum") is None


def test_supported_reads_module_attr_and_defaults():
    mod = types.ModuleType("fake_sup_mod")
    mod.SUPPORTED_SUBCATEGORIES = {"front_load", "induction", "bogus"}
    plain = types.ModuleType("fake_sup_plain")
    sys.modules["fake_sup_mod"], sys.modules["fake_sup_plain"] = mod, plain
    catalog.ADAPTERS["FakeS"], catalog.ADAPTERS["FakeP"] = "fake_sup_mod", "fake_sup_plain"
    try:
        assert catalog.supported("FakeS") == {"front_load", "induction"}  # unknown keys dropped
        assert catalog.supported("FakeP") == {"french_door", "side_by_side", "top_freezer", "bottom_freezer", "built_in"}
        assert catalog.supported("NoSuchBrand") == set()
        catalog.ADAPTERS["FakeM"] = "module_that_does_not_exist_xyz"
        assert catalog.supported("FakeM") == set()  # import failure => brand not usable
    finally:
        for b in ("FakeS", "FakeP", "FakeM"):
            catalog.ADAPTERS.pop(b, None)
        sys.modules.pop("fake_sup_mod"), sys.modules.pop("fake_sup_plain")


def test_adapters_registry_has_six_brands():
    assert {"Samsung": "samsung_us", "LG": "lg_us", "KitchenAid": "kitchenaid_us", "GE": "ge_us",
            "Whirlpool": "whirlpool_us", "Bosch": "bosch_us"}.items() <= catalog.ADAPTERS.items()


def test_candidate_defaults():
    c = Candidate(brand="X", model_number="M", name="n", url="u")
    assert c.category == "refrigerator" and c.subcategory is None
    c2 = Candidate(brand="X", model_number="M", name="n", url="u", category="washer", subcategory="dryer")
    assert Candidate.model_validate_json(c2.model_dump_json()) == c2


def test_major_aliases_cover_cooktops_microwaves_dryers_stove():
    for word in ("cooktop", "Cooktops", "microwave", "microwaves", "stove"):
        assert catalog.normalize_major(word) == "cooking", word
    for word in ("dryer", "Dryers"):
        assert catalog.normalize_major(word) == "washer", word


def test_regions_and_countries():
    assert list(catalog.REGIONS) == ["kr", "na", "eu", "sa", "me", "as", "oc"]
    assert catalog.DEFAULT_REGION == "na" and catalog.REGIONS["na"]["default"] and not catalog.REGIONS["kr"].get("default")
    assert catalog.REGIONS["na"]["countries"][:2] == ["us", "ca"] and catalog.REGIONS["eu"]["countries"][:2] == ["de", "uk"]
    for region, node in catalog.REGIONS.items():
        for cc in node["countries"]:
            assert catalog.COUNTRIES[cc]["region"] == region, cc
    assert catalog.region_of("kr") == "kr" and catalog.region_of("uk") == "eu" and catalog.region_of("zz") is None
    assert catalog.currency_of("de") == "EUR" and catalog.currency_of("us") == "USD"
    assert catalog.is_region("eu") and not catalog.is_region("xx") and not catalog.is_region(None)


def test_candidate_region_defaults_and_attrs():
    c = Candidate(brand="LG", model_number="M", name="n", url="https://www.lg.com/us/x")
    assert (c.region, c.country, c.currency, c.price_local, c.attrs, c.attrs_src) == ("na", "us", "USD", None, {}, {})
    c.attrs["x"] = 1  # pydantic copies mutable defaults: no sharing between instances
    assert Candidate(brand="LG", model_number="M", name="n", url="u").attrs == {}
    k = Candidate(brand="LG", model_number="M", name="n", url="u", region="kr", country="kr", currency="KRW",
                  price_local=2_990_000, attrs={"width_in": 35.8}, attrs_src={"width_in": "listing"})
    assert Candidate.model_validate_json(k.model_dump_json()) == k


def test_adapter_registry_by_country_and_auto_discovery():
    import tempfile
    assert catalog.module_name("Samsung") == "samsung_us" and catalog.module_name("Samsung", "us") == "samsung_us"
    # Hermetic: uses Samsung in Germany ('de'), for which no real adapter exists; other brands' real EU adapters
    # (miele_de, siemens_de ...) may exist, so only Samsung's entry is asserted.
    assert catalog.module_name("Samsung", "de") is None and catalog.module_name("Nope", "de") is None
    assert catalog.supported("Samsung", "de") == set() and "Samsung" not in catalog.region_support("eu")
    assert "Samsung" in catalog.region_support("na")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "samsung_de.py").write_text(
            "SUPPORTED_SUBCATEGORIES = {'french_door', 'bogus'}" + chr(10) + "COUNTRY = 'de'" + chr(10)
            + "def discover(subcategory, limit=30):" + chr(10) + "    return []" + chr(10), encoding="utf-8")
        sys.path.insert(0, d)
        try:
            import importlib
            importlib.invalidate_caches()
            assert catalog.module_name("Samsung", "de") == "samsung_de"
            assert catalog.supported("Samsung", "de") == {"french_door"}  # unknown sub keys dropped
            assert catalog.supported("LG", "de") == set()  # lg_de does not exist
            assert catalog.region_support("eu")["Samsung"] == {"french_door"}
        finally:
            sys.path.remove(d)
            sys.modules.pop("samsung_de", None)
            importlib.invalidate_caches()
    assert catalog.module_name("Samsung", "de") is None
    # the real Korean adapters are discovered by the same mechanism
    assert catalog.module_name("Samsung", "kr") == "samsung_kr" and catalog.module_name("LG", "kr") == "lg_kr"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
