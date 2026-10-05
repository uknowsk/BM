"""Plain-assert tests (pytest not installed). Run: python tests/test_features.py
Feature flags derived from the FULL spec table (not only curated extra_specs keys)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import features
from schema import ProductRecord, RawSpec

GE_MODES = ("Convection Bake | Convection Roast | Bake | Broil | Proof | Warm | Self Clean | No Preheat Air Fry")


def test_air_fry_from_oven_cooking_modes():
    flags = features.derive_flags("cooking", {"Oven Cooking Modes": GE_MODES}, [])
    assert flags["Air fry"] == "Yes (No Preheat Air Fry)", flags
    assert flags["Convection"].startswith("Yes (")
    assert flags["Self clean"].startswith("Yes (")
    assert flags["Proof mode"].startswith("Yes (")


def test_section_prefixed_key_and_raw_rows():
    flags = features.derive_flags("cooking", {"Specs > Oven Cooking Modes": "Bake | Air Fry"}, [])
    assert flags["Air fry"] == "Yes (Air Fry)"
    raws = [RawSpec(brand="GE", model_number="M", source="web", section="Features", key="Cooking Modes",
                    value="Sabbath Mode | Warming Drawer | Temperature Probe")]
    flags = features.derive_flags("cooking", {}, raws)
    assert flags["Sabbath mode"].startswith("Yes") and flags["Warming drawer"].startswith("Yes")
    assert flags["Temperature probe"].startswith("Yes")


def test_key_hit_affirmative_and_explicit_no():
    assert features.derive_flags("cooking", {"Air Fry": "Yes"}, [])["Air fry"] == "Yes (Air Fry)"
    assert features.derive_flags("cooking", {"Air Fry": "No"}, [])["Air fry"] == "No"
    # positive evidence elsewhere beats a curated/explicit No
    flags = features.derive_flags("cooking", {"Air fry": "No", "Modes": "Bake | Air Fry"}, [])
    assert flags["Air fry"] == "Yes (Air Fry)"


def test_absent_when_no_evidence_and_word_boundaries():
    flags = features.derive_flags("cooking", {"Width": "29.875 in", "Finish": "Stainless"}, [])
    assert "Air fry" not in flags and "Convection" not in flags
    assert "Proof mode" not in features.derive_flags("cooking", {"Door": "Dust-proof seal, waterproof"}, [])
    assert "Air fry" not in features.derive_flags("cooking", {"Note": "fairfry hairfryers"}, [])  # no word boundary
    assert "Air fry" in features.derive_flags("cooking", {"Note": "Includes an Air Fryer basket"}, [])


def test_category_groups():
    w = features.derive_flags("washer", {"Cycles": "Steam | Sanitize | Allergen", "Dispenser": "Auto Dispense"}, [])
    assert {"Steam", "Sanitize", "Allergen", "Auto dispense"} <= set(w)
    assert "Air fry" not in w
    f = features.derive_flags("refrigerator", {"Features": "Door-in-Door | Sabbath Mode"}, [])
    assert "Door-in-door" in f and "Sabbath mode" in f
    assert features.derive_flags("Refrigerator", {"F": "Sabbath Mode"}, [])  # legacy label accepted
    assert features.derive_flags(None, {"F": "Air Fry"}, []) == {}  # unknown category: nothing guessed


def test_apply_flags_overwrites_dash_and_no_keeps_evidence():
    p = ProductRecord(brand="GE", model_number="PTS9200SNSS", product_name="n", product_url="https://x/",
                      category="cooking", extra_specs={"Air fry": "–", "Convection": "No", "Oven Cooking Modes": GE_MODES})
    features.apply_flags(p, [])
    assert p.extra_specs["Air fry"] == "Yes (No Preheat Air Fry)"
    assert p.extra_specs["Convection"].startswith("Yes (")
    q = ProductRecord(brand="GE", model_number="X", product_name="n", product_url="https://x/", category="cooking",
                      extra_specs={"Air fry": "Yes"})
    features.apply_flags(q, [RawSpec(brand="GE", model_number="X", source="web", key="Modes", value="Air Fry")])
    assert q.extra_specs["Air fry"] == "Yes"  # already positive: untouched
    z = ProductRecord(brand="GE", model_number="Z", product_name="n", product_url="https://x/", category="cooking",
                      extra_specs={"Air fry": "No"})
    features.apply_flags(z, [])
    assert z.extra_specs["Air fry"] == "No"  # no evidence: curated value stays
    features.apply_flags(p, [])  # idempotent
    assert p.extra_specs["Air fry"] == "Yes (No Preheat Air Fry)"


def test_dedupe_specs_prefers_sectioned_keys():
    x = {"Width": "29.9 in", "Dimensions > Width": "29.9 in", "Oven racks": "3", "Racks > Number of racks": "3",
         "Sabbath mode": "Yes", "Features > Self Clean": "Yes", "Color": "White"}
    out = features.dedupe_specs(x)
    assert "Width" not in out and "Oven racks" not in out and "Dimensions > Width" in out
    assert "Sabbath mode" in out and "Color" in out  # bare Yes only dedupes against the same label
    assert "Self clean" not in out and features.dedupe_specs({"Sabbath mode": "Yes", "F > Sabbath mode": "Yes"}) == {"F > Sabbath mode": "Yes"}


def test_every_derived_flag_label_maps_to_the_canonical_seed():
    import canon
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cz = canon.Canonicalizer(d, offline=True, persist=False)
        unmapped = []
        for major in ("cooking", "washer", "refrigerator"):
            for label in features.flag_labels(major):
                if cz.canonicalize_item(label, category=major).method != "seed":
                    unmapped.append((major, label))
        # only genuinely new features may be missing from the seed (they become long-tail rows)
        assert set(unmapped) <= {("refrigerator", "Door-in-door"), ("refrigerator", "Dual evaporator"), ("washer", "Sanitize")} | set(), unmapped


def test_accessory_detection_is_conservative():
    yes = ['UXWORXR30 - 30" Never Scrub Roller Rack', "Optional- JXAFTRAY1VSS", "JXAFTRAY1VSS", "Trim Kit - 30 in", "Installation Kit",
           "Stainless Cleaner WX10X300 (cleaner)", "Optional Pedestal", "Filler Panel $59.99"]
    no = ["Convection Bake", "Air Fry", "Water Filter", "Self-Clean", "Delay Time", "Sabbath Mode", "Cover Glass Door", "1 Standard Rack",
          "Embossed Rack Positions (Both Ovens)", "Stainless Steel", "Door Lock", "Custom Temp Drawer"]
    assert all(features.is_accessory_item(t) for t in yes), [t for t in yes if not features.is_accessory_item(t)]
    assert not any(features.is_accessory_item(t) for t in no), [t for t in no if features.is_accessory_item(t)]
    assert features.is_accessory_spec("Accessories", "Air Fry Basket", "x") and features.is_accessory_spec("", "Optional Riser", "x")
    assert features.is_accessory_spec("Features", "Air Fry Basket", "Optional- JXAFTRAY1VSS")
    assert not features.is_accessory_spec("Features", "Oven Cooking Modes", "Bake | Air Fry")


def test_air_fry_from_an_optional_basket_alone_is_labelled_an_accessory():
    only = features.derive_flags("cooking", {"Accessories > Air Fry Basket": "Optional- JXAFTRAY1VSS"}, [])
    assert only["Air fry"] == "Yes (옵션(액세서리): Air Fry Basket)", only
    both = features.derive_flags("cooking", {"Accessories > Air Fry Basket": "Optional- JXAFTRAY1VSS",
                                             "Features > Oven Cooking Modes": "Bake | No Preheat Air Fry"}, [])
    assert both["Air fry"] == "Yes (No Preheat Air Fry)", both  # built-in evidence wins; the accessory never claims built-in


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
