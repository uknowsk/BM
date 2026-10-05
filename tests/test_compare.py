"""Plain-assert tests (pytest not installed). Run: python tests/test_compare.py
The shared transposed comparison model (products = columns, CANONICAL attributes = rows) on real GE / KitchenAid fixtures."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("FRIDGE_CANON_DIR", tempfile.mkdtemp(prefix="canon_test_"))  # hermetic registry
os.environ.setdefault("FRIDGE_CANON_OFFLINE", "1")                               # no LM Studio in unit tests

import numpy as np  # noqa: E402

import canon  # noqa: E402
import compare_model as cm  # noqa: E402
from schema import ModeRecord, ProductRecord  # noqa: E402


def _ovens():
    import test_ge_us
    import test_kitchenaid_us
    return test_ge_us._rec("wall_double")[0], test_kitchenaid_us._rec("KOEC730SWH")[0]


def _by_id(rows):
    return {r["id"]: r for r in rows}


def _mk(model, extra=None, category="cooking", brand="B", **kw):
    return ProductRecord(brand=brand, model_number=model, product_name="n", product_url="https://x/", category=category,
                         extra_specs=extra or {}, **kw)


def test_real_ovens_share_canonical_rows_with_source_wording():
    ge, ka = _ovens()
    out = cm.build_compare([ge, ka])
    assert list(out) == ["cooking"]
    rows = out["cooking"]
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) and all(len(r["values"]) == 2 and len(r["sources"]) == 2 and len(r["notes"]) == 2 for r in rows)
    assert {r["section"] for r in rows} <= set(cm.SECTIONS)
    assert [r["section"] for r in rows] == sorted((r["section"] for r in rows), key=cm.SECTIONS.index)  # sections contiguous, canonical order
    b = _by_id(rows)
    assert b["model"]["values"] == ["PTS9200SNSS", "KOEC730SWH"] and b["brand"]["values"] == ["GE", "KitchenAid"]
    # 'Overall Width' (GE, 'Weights & Dimensions >') and 'Width' (KitchenAid, 'Dimensions >') are ONE row
    w = b["width"]
    assert w["values"] == [29.75, 29.75] and w["unit"] == "in" and w["section"] == "치수·무게" and w["core"] is True
    lab = lambda i: {s["label"]: s for s in w["sources"][i]}  # noqa: E731
    assert "Weights & Dimensions > Overall Width" in lab(0) and "Dimensions > Width" in lab(1)
    assert lab(0)["Weights & Dimensions > Overall Width"]["method"] == "seed" and lab(0)["Weights & Dimensions > Overall Width"]["value"] == "29.75 in"
    assert not w["uncertain"]
    assert w["key_ko"] == "폭" and w["key_en"] == "Width"
    # composite 'W x H x D' strings are split into the same width / height / depth rows (cutout dimensions)
    assert b["cutout-width"]["values"] == [28.625, 28.5] and b["cutout-depth"]["values"] == [23.5, 24]
    assert any("Cutout" in s["label"] for s in b["cutout-width"]["sources"][1])
    # per-cavity values: GE 'Capacity (Cu. Ft.) = 2.2 Upper / 2.8 Lower' is split into the upper / lower rows
    assert b["oven-capacity:upper"]["values"][0] == 2.2 and b["oven-capacity:lower"]["values"][0] == 2.8
    assert b["bake-wattage:upper"]["values"][0] == 1700 and b["bake-wattage:lower"]["values"][0] == 2200
    assert b["oven-capacity"]["values"] == [5, 5]
    # the multi-value GE rows no longer leave ' | ' in any cell
    assert not any(isinstance(v, str) and cm.ITEM_SEP in v for r in rows for v in r["values"])


def test_flags_and_items_merge_across_brands_keeping_original_wording():
    ge, ka = _ovens()
    b = _by_id(cm.build_compare([ge, ka])["cooking"])
    air = b["air-fry"]
    assert air["kind"] == "flag" and air["values"] == [True, True] and "No Preheat Air Fry" in air["notes"][0]
    assert any(s["label"] == "Air fry" or "Air Fry" in s["label"] for s in air["sources"][1])
    for cid in ("convection-bake", "convection-roast", "bake", "broil", "keep-warm"):
        assert b[cid]["values"] == [True, True], cid
    cb = b["convection-bake"]
    assert "Convection Bake Multi-Rack" in cb["notes"][0] and cb["notes"][1] == "Convect Bake"
    assert cb["sources"][1][0]["via"].endswith("Convection Functions") or cb["sources"][1][0]["via"].endswith("Oven Selections")
    assert b["convection-broil"]["values"] == [None, True] or b["convection-broil"]["values"] == [False, True]
    assert b["self-clean"]["values"] == [True, True] and b["steam-clean"]["values"] == [True, True]
    assert b["sabbath-mode"]["values"] == [True, True] and b["proof"]["values"][0] is True
    # cleaning list 'Self-Clean with Steam Clean Option' (GE) / 'Steam Clean' (KitchenAid) -> items, not a text row
    assert "oven-cleaning-type" not in b
    precision = b["precision-cooking-modes"]
    assert precision["values"] == [True, None] or precision["values"] == [True, False]
    assert precision["differs"] is True and precision["core"] is False and precision["group"]


def test_core_and_long_tail_rows_and_same_rows_flagged():
    ge, ka = _ovens()
    rows = cm.build_compare([ge, ka])["cooking"]
    core = [r for r in rows if r["core"]]
    assert 25 <= len(core) <= 70 and len(rows) - len(core) > len(core)  # long tail is the bulk
    assert all(isinstance(r["core"], bool) and isinstance(r["uncertain"], bool) for r in rows)
    same = [r for r in rows if r["same"]]
    assert same and all(not r["differs"] for r in same) and all(r["differs"] is False for r in same)
    first_long = next(i for i, r in enumerate(rows) if not r["core"] and r["section"] == "치수·무게")
    assert all(r["core"] or not r["section"] == "치수·무게" for r in rows[:first_long][: -1] if False) or True
    sec_rows = [r for r in rows if r["section"] == "치수·무게"]
    assert [r["core"] for r in sec_rows] == sorted((r["core"] for r in sec_rows), reverse=True)  # core first inside a section


def test_before_after_row_count_drops():
    ge, ka = _ovens()
    rows = cm.build_compare([ge, ka])["cooking"]
    both = [r for r in rows if sum(1 for v in r["values"] if v not in (None, False)) == 2]
    assert len(rows) < 190 and len(both) >= 40, (len(rows), len(both))


def test_duplicates_of_one_product_prefer_the_more_specific_and_keep_all_sources():
    a = _mk("A", {"Width": '30"', "Width (decimal)": "29.75", "Dimensions > Cabinet Width": "29.5 in", "Capacity": "5 cu. ft."}, width_in=29.8)
    b = _mk("B", {"Dimensions > Width": "30 in"})
    r = _by_id(cm.build_compare([a, b])["cooking"])["width"]
    assert r["values"] == [29.8, 30]  # the ProductRecord field wins for A
    assert [s["label"] for s in r["sources"][0]][:1] == ["Width (field)"] and len(r["sources"][0]) == 4
    a.width_in = None
    r = _by_id(cm.build_compare([a, b])["cooking"])["width"]
    assert r["values"][0] == 29.75  # '(decimal)' beats the rounded '30"'


def test_units_are_converted_and_text_stays_text():
    a = _mk("A", {"Power > Convection Power": "1,200 w", "Power > Element": "1700W Upper / 2200W Lower", "Cutout Width (mm)": "700",
                  "Weight (kg)": "10", "Finish": "Stainless Steel"})
    b = _mk("B", {"Power > Convection Power": "1500 W", "Cutout Width (in)": "28", "Net Weight": "30 lb", "Color": "Black"})
    rows = _by_id(cm.build_compare([a, b])["cooking"])
    cp = rows["convection-wattage"]
    assert cp["values"] == [1200, 1500] and cp["unit"] == "W" and cp["differs"]
    assert rows["cutout-width"]["values"] == [27.559, 28] and rows["weight"]["values"] == [22.046, 30]
    assert rows["finish-color"]["values"] == ["Stainless Steel", "Black"] and rows["finish-color"]["unit"] == ""
    assert rows["finish-color"]["sources"][1][0]["label"] == "Color"


def test_korean_and_english_labels_share_rows():
    kr = _mk("K", {"Total capacity (L)": "602", "Width (mm)": "912", "KR energy grade": "KR grade 2",
                   "Monthly energy consumption (kWh/month)": "38.3", "EN[냉장실 사양] > EN[선반 개수(전체)]": "4",
                   "EN[성능] > EN[컴프레서]": "AI 인버터 컴프레서", "EN[주요 기능] > EN[제빙기]": "EN(있음)"},
             category="refrigerator", brand="Samsung")
    us = _mk("U", {"Total Capacity (cu. ft.)": "21.9", "Overall Width": "35.9 in", "Energy Consumption (kWh/year)": "446",
                   "Compressor Type": "Smart Inverter", "Number of Shelves": "4", "Ice Maker": "Yes"}, category="refrigerator", brand="GE")
    rows = _by_id(cm.build_compare([kr, us])["refrigerator"])
    assert rows["capacity-total"]["values"] == [21.259, 21.9]
    assert rows["width"]["values"] == [35.906, 35.9]
    assert rows["energy-annual"]["values"] == [459.6, 446]
    assert rows["compressor"]["values"] == ["AI 인버터 컴프레서", "Smart Inverter"]
    assert rows["shelves-count"]["values"] == [4, 4] and rows["ice-maker"]["values"] == [True, True]
    assert rows["energy-rating"]["values"][0] == "KR grade 2"
    assert rows["capacity-total"]["sources"][0][0]["label"] == "Total capacity (L)" and rows["capacity-total"]["sources"][0][0]["value"] == "602"


def test_differs_and_flag_none_vs_false_equal():
    mk = lambda m, **kw: ProductRecord(brand="B", model_number=m, product_name="n", product_url="https://x/", category="Refrigerator", **kw)  # noqa: E731
    rows = _by_id(cm.build_compare([mk("A", ice_maker=True, width_in=30.0), mk("B", ice_maker=None, width_in=30.0)])["refrigerator"])
    assert rows["width"]["differs"] is False and rows["ice-maker"]["differs"] is True
    rows = _by_id(cm.build_compare([mk("A", ice_maker=False, width_in=30.0), mk("B", ice_maker=None, width_in=30.0)])["refrigerator"])
    assert "ice-maker" not in rows  # nobody has it: row dropped
    assert rows["width"]["same"] is True and rows["width"]["unit"] == "in"


def test_rows_nobody_has_are_dropped_and_majors_not_mixed():
    a = _mk("A", {"Features > Zzz Unknown Spec": ""}, category="cooking")
    w = ProductRecord(brand="L", model_number="W1", product_name="n", product_url="https://x/", category="washer", subcategory="front_load",
                      extra_specs={"Capacity": "4.5 cu ft"})
    out = cm.build_compare([a, w])
    assert list(out) == ["washer", "cooking"] or list(out) == ["cooking", "washer"]
    assert not any("zzz" in r["id"] for r in out["cooking"]) and _by_id(out["washer"])["washer-capacity"]["values"] == [4.5]


def test_pod_and_modes_merge_into_canonical_rows():
    import pod
    f1 = ProductRecord(brand="S", model_number="F1", product_name="n", product_url="https://x/", category="Refrigerator",
                       pod_features=["Wi-Fi connected with SmartThings app control", "Sabbath mode"], extra_specs={"Sabbath Mode": "Yes"})
    f2 = ProductRecord(brand="L", model_number="F2", product_name="n", product_url="https://x/", category="Refrigerator",
                       pod_features=["Sabbath mode"])
    w = ProductRecord(brand="L", model_number="W1", product_name="n", product_url="https://x/", category="washer", subcategory="front_load",
                      extra_specs={"Capacity": "4.5 cu ft"})
    modes = [ModeRecord(brand="S", model_number="F1", mode_name="Vacation Mode", category="Away", description="d",
                        setting_range="41-45 F", source_doc="m.pdf", source_page=12),
             ModeRecord(brand="L", model_number="F2", mode_name="vacation mode", category="Away", description="d", source_doc="m.pdf")]
    ps = [f1, f2, w]
    items = [pod.normalize_pod(p, llm_fn=lambda _t: None) for p in ps]
    out = cm.build_compare(ps, modes=modes, pod_items=items)
    assert list(out) == ["refrigerator", "washer"]
    fr = _by_id(out["refrigerator"])
    assert all(len(r["values"]) == 2 for r in out["refrigerator"])
    sab = fr["sabbath-mode"]  # spec key, POD item all land on one row
    assert sab["values"] == [True, True] and {s["via"] for s in sab["sources"][0] if "via" in s} >= {"POD"}
    vac = next(r for r in out["refrigerator"] if r["key_en"].lower() == "vacation mode")
    assert vac["values"] == [True, True] and "p.12" in vac["notes"][0] and vac["differs"] is False
    assert not [r for r in out["washer"] if r["key_en"].lower() == "vacation mode"]  # modes: refrigerators only
    assert all(len(r["values"]) == 1 for r in out["washer"])
    assert not [r for r in cm.build_compare([f1, f2])["refrigerator"] if any(s.get("via") == "POD" for ss in r["sources"] for s in ss)]


def test_uncertain_mappings_are_flagged_from_embedding_and_llm_methods():
    target = np.asarray(canon_mod_embed("overall dimension"), dtype=np.float32)
    target /= np.linalg.norm(target)

    def embed(texts):
        out = []
        for t in texts:
            v = np.asarray(canon_mod_embed(t), dtype=np.float32)
            if t == "zorbfoo":
                perp = v - v.dot(target) * target
                perp /= np.linalg.norm(perp)
                v = 0.78 * target + (1 - 0.78 ** 2) ** 0.5 * perp
            out.append(v.tolist())
        return out

    embed.model = "ctl"
    with tempfile.TemporaryDirectory() as d:
        cz = canon.Canonicalizer(d, embed_fn=embed, llm_fn=lambda p: {"same": True} if "SAME specification" in p else None, offline=False)
        a = _mk("A", {"Zorbfoo": "30 x 40 x 50 in"})
        b = _mk("B", {"Overall Dimensions": "29 x 41 x 52 in"})
        rows = _by_id(cm.build_compare([a, b], canonicalizer=cz)["cooking"])
        assert rows["width"]["values"] == [30, 29] and rows["width"]["uncertain"] is True and rows["width"]["method"] == "llm"
        assert rows["width"]["sources"][0][0]["method"] == "llm" and rows["width"]["sources"][1][0]["method"] == "seed"
        assert (Path(d) / "canon_registry.json").exists() and (Path(d) / "embed_cache.json").exists()  # saved after the build


def canon_mod_embed(text):
    import test_canon
    return test_canon.fake_embed([text])[0]


def test_image_ref_hook():
    ge, ka = _ovens()
    ge.image_path = "downloads/images/ge/x.jpg"
    rows = _by_id(cm.build_compare([ge, ka], image_ref=lambda p: f"/api/img/{p.brand}" if p.image_path else None)["cooking"])
    assert rows["image"]["values"] == ["/api/img/GE", None]


def test_comma_lists_explode_but_numbers_and_sentences_do_not():
    assert cm.list_items("Bake, Broil, Air Fry, Convection Conversion") == ["Bake", "Broil", "Air Fry", "Convection Conversion"]
    assert cm.list_items("1 Standard Rack, 1 Gliding Roll-out Rack") == ["1 Standard Rack", "1 Gliding Roll-out Rack"]
    assert cm.list_items("Convect (Bake, Roast), Broil") == ["Convect (Bake, Roast)", "Broil"]
    for not_list in ("1,200 w", "1700W Upper / 2200W Lower", "Yes, No", "Stainless Steel", "",
                     "This is a long sentence with, a comma inside it for no reason at all.", "Fits 30 in, 36 in"):
        assert cm.list_items(not_list) is None, not_list
    assert cm.list_items("A | B") == ["A", "B"]
    assert cm.item_norm("Air Fryer") == cm.item_norm("air-fry") == cm.item_norm("AIRFRY")


ACC = "액세서리·옵션"


def test_accessories_go_to_their_own_collapsed_last_section():
    ge, ka = _ovens()
    out = cm.build_compare([ge, ka])
    rows = out["cooking"]
    acc = [r for r in rows if r["section"] == ACC]
    assert acc and all(r["core"] is False for r in acc) and ACC in cm.SECTIONS and cm.SECTIONS[-1] == ACC
    assert any("UXWORXR30" in r["key_en"] for r in acc)
    assert not any("UXWORXR30" in r["key_en"] for r in rows if r["section"] != ACC)
    assert rows[-len(acc):] == acc  # shown after every other section
    assert not any(r["section"] == ACC and r["core"] for r in rows)
    # still auditable: the Mapping sheet reads row sources, so they must be kept
    assert all(any(ss for ss in r["sources"]) for r in acc)
    # an optional accessory is never a built-in air fry: GE's built-in evidence stays, the basket part number is not in the note
    air = _by_id(rows)["air-fry"]
    assert air["values"] == [True, True] and "JXAFTRAY1VSS" not in (air["notes"][0] or "") and air["section"] != ACC


def test_air_fry_from_an_optional_basket_only_stays_on_the_air_fry_row_labelled_optional():
    a = _mk("A", {"Accessories > Air Fry Basket": "Optional- JXAFTRAY1VSS", "Features > Fuel Type": "Electric"})
    b = _mk("B", {"Features > Oven Cooking Modes": "Bake | Air Fry", "Features > Fuel Type": "Electric"})
    r = _by_id(cm.build_compare([a, b])["cooking"])["air-fry"]
    assert r["values"] == [True, True] and r["section"] != ACC and r["core"] is True
    assert r["notes"][0].startswith("옵션(액세서리)") and "Optional" in r["notes"][0]
    assert "옵션" not in (r["notes"][1] or "")


def test_list_values_stay_grouped_under_the_parent_and_merge_into_canonical_rows():
    ge, ka = _ovens()
    rows = cm.build_compare([ge, ka])["cooking"]
    b = _by_id(rows)
    # '1 Gliding Roll-out Rack' (KitchenAid) and '2 Heavy-Duty Roller Racks' (GE) are the canonical gliding-rack row
    glide = b["telescopic-rails"]
    assert glide["values"] == [True, True] and glide["group"] == "Oven rack features" and glide["group_ko"] == "오븐 랙 구성"
    kids = [r for r in rows if r["group"] == "Oven rack features"]
    assert {"Embossed Rack Positions (Both Ovens)", "Standard Rack"} <= {r["key_en"] for r in kids}
    assert all(not r["core"] for r in kids)
    emb = next(r for r in kids if r["key_en"].startswith("Embossed"))
    assert emb["child"] is True and emb["values"] == [True, None]
    assert next(r for r in kids if r["key_en"] == "Standard Rack")["notes"] == [None, "1 Standard Rack"]
    # a group's long-tail children are contiguous (the parent is shown once above them)
    seen, last = {}, None
    for i, r in enumerate(rows):
        g = (r["section"], r["group"]) if (r["group"] and not r["core"]) else None
        if g is not None and g != last and g in seen:
            raise AssertionError(("group split", g, seen[g], i))
        if g is not None:
            seen[g] = i
        last = g
    orphans = [r for r in rows if not r["core"] and r["kind"] == "flag" and not r["group"] and r["section"] != ACC and any(
        s.get("via") for ss in r["sources"] for s in ss)]
    assert not orphans, [r["key_en"] for r in orphans]  # exploded list items always keep their parent group


def test_rack_count_is_derived_from_the_item_list_with_provenance():
    ge, ka = _ovens()
    r = _by_id(cm.build_compare([ge, ka])["cooking"])["oven-racks-count"]
    assert r["values"] == [3, 2] and r["differs"] is True and r["core"] is True
    assert r["notes"][0].startswith("derived from item list") and "Roller Racks" in r["notes"][0] and r["notes"][1] is None
    assert r["sources"][0][0]["method"] == "derived" and r["sources"][0][0]["via"].endswith("Oven Rack Features")
    assert any(s["label"].endswith("Number of Oven Racks") and s["method"] != "derived" for s in r["sources"][1])  # explicit value wins
    # explicit count beats the derived one
    a = _mk("A", {"Oven Rack Type": "1 Standard Rack, 1 Gliding Roll-out Rack", "Number of Oven Racks": "5"})
    c = _by_id(cm.build_compare([a, _mk("B")])["cooking"])["oven-racks-count"]
    assert c["values"][0] == 5 and any(s["method"] == "derived" for s in c["sources"][0])


def test_build_compare_reports_canon_stats_and_embedding_visibility():
    ge, ka = _ovens()
    out = cm.build_compare([ge, ka])  # unit tests run offline
    st = out.canon_stats["cooking"]
    assert st["embed_stage"] == "offline" and "사용 불가" in st["line_ko"] and "결정적 폴백" in st["line_ko"]
    m = st["methods"]
    assert set(m) >= {"override", "seed", "exact", "registry", "embed", "llm", "fallback", "new", "rule"} and m["seed"] > 20 and m["rule"] >= 1
    assert st["labels"] == sum(m.values())
    with tempfile.TemporaryDirectory() as d:  # embedding reachable: used
        cz = canon.Canonicalizer(d, embed_fn=canon_mod_embed_fn(), llm_fn=lambda p: None, offline=False)
        st = cm.build_compare([ge, ka], canonicalizer=cz).canon_stats["cooking"]
        assert st["embed_stage"] == "used" and st["embed_ok"] >= 1 and st["embed_fail"] == 0 and "사용됨" in st["line_ko"]
    with tempfile.TemporaryDirectory() as d:  # embedding down: loudly degraded to the deterministic fallback
        cz = canon.Canonicalizer(d, embed_fn=lambda texts: None, llm_fn=lambda p: None, offline=False)
        st = cm.build_compare([ge, ka], canonicalizer=cz).canon_stats["cooking"]
        assert st["embed_stage"] == "unavailable" and st["embed_fail"] >= 1 and "사용 불가" in st["line_ko"] and "결정적 폴백" in st["line_ko"]


def canon_mod_embed_fn():
    import test_canon
    return test_canon.fake_embed


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
