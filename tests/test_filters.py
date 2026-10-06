"""Plain-assert tests. Run: python tests/test_filters.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filters
from catalog import Candidate
from schema import ProductRecord

U = filters.UNKNOWN


def cand(name, brand="Samsung", sub="french_door", price=None, major="refrigerator", **kw):
    return Candidate(brand=brand, model_number=name[:8], name=name, url=f"https://x/{abs(hash((name, brand)))}",
                     price_usd=price, category=major, subcategory=sub, **kw)


FR = [
    cand('Samsung 28 cu. ft. 36" Smart 4-Door French Door Stainless Steel ENERGY STAR', price=3000),
    cand("LG 25.5 cu ft 33-inch French Door Refrigerator Black Stainless", brand="LG", price=2500),
    cand("GE 22 cu ft Counter-Depth French Door Wi-Fi White", brand="GE", price=1800),
    cand("Whirlpool French Door Refrigerator", brand="Whirlpool"),  # nothing derivable
]


def test_schema_levels_and_lookup():
    keys = [g["key"] for g in filters.groups_for("french_door")]
    assert keys[:2] == ["brand", "price"] and "door_type" not in keys  # door_type is redundant under a sub
    assert "door_type" in [g["key"] for g in filters.groups_for("refrigerator")]
    for sub in ("front_load", "induction", "microwave"):
        assert filters.groups_for(sub)
    assert "burners" not in [g["key"] for g in filters.groups_for("microwave")]
    assert "burners" in [g["key"] for g in filters.groups_for("induction")]
    sco = [g["key"] for g in filters.groups_for("sco")]
    assert {"oven_capacity", "microwave_power", "convection", "smart", "width_class"} <= set(sco)
    assert "burners" not in sco and "fuel" not in sco and "microwave_power" not in [g["key"] for g in filters.groups_for("gas_oven")]
    for sub in ("electric_oven", "microwave", "otr", "sco"):  # no burners and a fixed heat source on these
        keys = [g["key"] for g in filters.groups_for(sub)]
        assert "burners" not in keys and "fuel" not in keys, sub
    gc = [g["key"] for g in filters.groups_for("gas_cooktop")]  # fuel is fixed (gas); burners/width stay, no oven
    assert "burners" in gc and "width_class" in gc and "fuel" not in gc and "oven_capacity" not in gc
    assert {"burners", "fuel"} <= {g["key"] for g in filters.groups_for("gas_oven")}  # mixed-fuel groups keep them
    cook_types = [v["key"] for g in filters.groups_for("cooking") if g["key"] == "cook_type" for v in g["values"]]
    assert "sco" in cook_types and "gas_cooktop" in cook_types
    assert "scr" not in [v["key"] for g in filters.groups_for("cooking") if g["key"] == "cook_type" for v in g["values"]]
    try:
        filters.groups_for("scr")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    cap = {g["key"]: g for g in filters.groups_for("compact")}["capacity_l"]
    assert cap["values"][0]["key"] == "lt100"  # sub override of the capacity buckets
    cap = {g["key"]: g for g in filters.groups_for("french_door")}["capacity_l"]
    assert cap["level"] == "listing" and cap["display_units"] == ["L", "cu ft"] and cap["values"][0]["key"] == "lt400"
    assert all(g["level"] in ("listing", "spec") and g["type"] in ("multi", "range", "boolean", "text")
               for sub in ("french_door", "dryer", "radiant") for g in filters.groups_for(sub))
    assert "capacity_kg" in [g["key"] for g in filters.groups_for("front_load")]
    # region-restricted groups are dropped for other regions
    assert all(g["key"] != "kr_grade" for g in filters.groups_for("french_door", regions=["na"]))


def test_name_facts_extraction():
    f = filters.name_facts(FR[0].name)
    assert f["width_in"] == 36 and f["capacity_total_cuft"] == 28 and f["energy_star"] is True
    assert f["wifi"] is True and f["finish"] == "stainless" and f["door_type"] == "four_door"
    f = filters.name_facts(FR[1].name)
    assert f["width_in"] == 33 and f["capacity_total_cuft"] == 25.5 and f["finish"] == "black_stainless"
    assert filters.name_facts(FR[2].name)["wifi"] is True and filters.name_facts(FR[2].name)["finish"] == "white"
    assert filters.name_facts(FR[3].name) == {"door_type": "french_door"}
    assert "width_in" not in filters.name_facts('2 in 1 Refrigerator')  # implausible widths ignored
    assert filters.name_facts("Gas Range 5 Burner Convection Air Fry 30-inch")["burners"] == 5
    assert filters.name_facts("Dual Fuel Range")["fuel"] == "dual_fuel"
    assert filters.name_facts("Speed Oven 1000W Microwave Convection")["microwave_watts"] == 1000
    assert filters.name_facts("Combi 1,100 watts Speed Cook")["microwave_watts"] == 1100
    assert "microwave_watts" not in filters.name_facts("Gas Range 30-inch")


def test_gas_cooktop_facts_fixed_gas_and_burners_from_name():
    c = cand('Gas Cooktop 36" 5-Burner', brand="LG", sub="gas_cooktop", major="cooking")
    f = filters.facts(c)
    assert f["fuel"] == "gas" and f["cook_type"] == "gas_cooktop" and f["burners"] == 5 and f["width_in"] == 36
    out = filters.filter_candidates([c, cand("Electric Cooktop 30-inch", sub="radiant", major="cooking")],
                                    {"cook_type": ["gas_cooktop"], "burners": ["5"]})
    assert [x.name for x in out] == ['Gas Cooktop 36" 5-Burner']


def test_enrich_marks_source():
    e = filters.enrich(FR[0])
    assert e.attrs["capacity_total_cuft"] == 28 and e.attrs_src["capacity_total_cuft"] == "name"
    assert FR[0].attrs == {}  # input untouched
    c = cand("Samsung 28 cu. ft.", attrs={"capacity_total_cuft": 27.4}, attrs_src={"capacity_total_cuft": "listing"})
    e = filters.enrich(c)
    assert e.attrs["capacity_total_cuft"] == 27.4 and e.attrs_src["capacity_total_cuft"] == "listing"  # listing wins


def test_filter_and_or_semantics_and_unknown():
    out = filters.filter_candidates(FR, {"brand": ["Samsung", "LG"]})
    assert [c.brand for c in out] == ["Samsung", "LG"]  # OR within a group, input order kept
    # 28 cu ft = 793 L, 25.5 = 722 L, 22 = 623 L
    out = filters.filter_candidates(FR, {"capacity_l": ["600_700"]})
    assert [c.brand for c in out] == ["GE"]
    out = filters.filter_candidates(FR, {"capacity_l": ["700_plus"]})
    assert [c.brand for c in out] == ["Samsung", "LG"]
    # unknown excluded by default, included on request (global, per-group dict, or "__unknown__" value)
    assert [c.brand for c in filters.filter_candidates(FR, {"capacity_l": ["600_700"]}, include_unknown=True)] == ["GE", "Whirlpool"]
    assert [c.brand for c in filters.filter_candidates(FR, {"capacity_l": ["600_700"]}, include_unknown={"capacity_l": True})] == ["GE", "Whirlpool"]
    assert [c.brand for c in filters.filter_candidates(FR, {"capacity_l": ["600_700", U]})] == ["GE", "Whirlpool"]
    assert [c.brand for c in filters.filter_candidates(FR, {"capacity_l": ["600_700"]}, include_unknown={"brand": True})] == ["GE"]
    # groups AND together
    out = filters.filter_candidates(FR, {"capacity_l": ["700_plus"], "smart": ["wifi"]})
    assert [c.brand for c in out] == ["Samsung"]
    assert filters.filter_candidates(FR, {}) == FR and filters.filter_candidates(FR, None) == FR


def test_input_not_mutated_and_stable():
    before = [c.model_copy(deep=True) for c in FR]
    filters.filter_candidates(FR, {"brand": ["LG"]})
    assert FR == before


def test_boolean_range_keyword_groups():
    out = filters.filter_candidates(FR, {"price": {"min": 2000, "max": 2600}})
    assert [c.brand for c in out] == ["LG"]
    assert [c.brand for c in filters.filter_candidates(FR, {"price": {"max": 2500}})] == ["LG", "GE"]
    assert [c.brand for c in filters.filter_candidates(FR, {"price": {"min": 2000}}, include_unknown=True)] == ["Samsung", "LG", "Whirlpool"]
    assert [c.brand for c in filters.filter_candidates(FR, {"keyword": "counter"})] == ["GE"]
    assert [c.brand for c in filters.filter_candidates(FR, {"keyword": "SAMSUNG"})] == ["Samsung"]
    assert [c.brand for c in filters.filter_candidates(FR, {"energy": ["energy_star"]})] == ["Samsung"]
    wash = [cand("Samsung Front Load Steam Washer", sub="front_load", major="washer"),
            cand("LG Front Load Washer", brand="LG", sub="front_load", major="washer")]
    assert [c.brand for c in filters.filter_candidates(wash, {"steam": True})] == ["Samsung"]


def test_width_and_finish_and_door_classes():
    assert [c.brand for c in filters.filter_candidates(FR, {"width_class": ["w36"]})] == ["Samsung"]
    assert [c.brand for c in filters.filter_candidates(FR, {"width_class": ["w33"]})] == ["LG"]
    assert [c.brand for c in filters.filter_candidates(FR, {"finish": ["stainless", "white"]})] == ["Samsung", "GE"]
    assert [c.brand for c in filters.filter_candidates(FR, {"door_type": ["four_door"]})] == ["Samsung"]


def test_products_and_attrs_override_name():
    p = ProductRecord(brand="Whirlpool", model_number="W", product_name="n", product_url=FR[3].url,
                      capacity_total_cuft=30.0, width_in=36.0, energy_star=True, wifi_supported=True,
                      finish_color="Stainless Steel")
    out = filters.filter_candidates(FR, {"capacity_l": ["700_plus"], "smart": ["wifi"], "finish": ["stainless"]},
                                    products={FR[3].url: p})
    assert [c.brand for c in out] == ["Samsung", "Whirlpool"]
    c = cand("Samsung 20 cu ft", attrs={"capacity_total_cuft": 30.0})  # attrs beat the name
    assert filters.filter_candidates([c], {"capacity_l": ["700_plus"]}) == [c]


def test_validation_errors():
    for bad in ({"nope": ["x"]}, {"brand": "Samsung"}, {"capacity_l": ["bogus"]}, {"price": {"min": "a"}},
                {"price": {"min": -1}}, {"price": {"min": 5, "max": 1}}, {"price": ["x"]}, {"steam": "yes"},
                {"brand": ["x" * 65]}, {"brand": [str(i) for i in range(51)]}):
        try:
            filters.validate_selections(bad, ["french_door"], ["na"])
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
        try:
            filters.filter_candidates(FR, bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    many = {f"g{i}": ["a"] for i in range(21)}
    try:
        filters.validate_selections(many, ["french_door"], ["na"])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    ok = {"brand": ["Anything"], "capacity_l": ["600_700", U], "price": {"min": 0, "max": 1e9}, "keyword": "x"}
    assert filters.validate_selections(ok, ["french_door"], ["na"]) == ok
    # a washer group is not valid for a fridge-only search
    try:
        filters.validate_selections({"capacity_kg": ["lt9"]}, ["french_door"], ["na"])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert filters.validate_selections({"capacity_kg": ["lt9"]}, ["french_door", "front_load"], ["na"])


def test_facet_counts_disjunctive_with_unknown():
    sel = {"brand": ["Samsung"], "capacity_l": ["700_plus"]}
    f = filters.facet_counts(FR, sel, ["brand", "capacity_l"])
    # brand counts ignore the brand selection but honour capacity (700_plus -> Samsung, LG)
    assert f["brand"] == {"Samsung": 1, "LG": 1, "GE": 0, "Whirlpool": 0, U: 0}
    # capacity counts honour the brand selection (Samsung only)
    assert f["capacity_l"]["700_plus"] == 1 and f["capacity_l"]["600_700"] == 0 and f["capacity_l"][U] == 0
    f = filters.facet_counts(FR, {}, ["capacity_l", "smart"])
    assert f["capacity_l"]["700_plus"] == 2 and f["capacity_l"]["600_700"] == 1 and f["capacity_l"][U] == 1
    assert f["smart"]["wifi"] == 2 and f["smart"][U] == 2
    assert sum(v for k, v in f["capacity_l"].items() if k != U) + f["capacity_l"][U] == len(FR)  # one bucket each
    f = filters.facet_counts(FR, {"keyword": "french"}, ["steam"])
    assert f["steam"] == {"true": 0, U: 4}


def test_facet_counts_unknown_selection_and_price():
    f = filters.facet_counts(FR, {"capacity_l": ["600_700", U]}, ["brand"])
    assert f["brand"]["Whirlpool"] == 1 and f["brand"]["GE"] == 1 and f["brand"]["Samsung"] == 0
    f = filters.facet_counts(FR, {}, ["price"])
    assert f["price"] == {U: 1}


def test_candidate_sub_selects_bucket_definition():
    c = cand("Whirlpool 4 cu ft Compact", brand="Whirlpool", sub="compact")  # 113 L
    assert filters.filter_candidates([c], {"capacity_l": ["100_200"]}) == [c]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
