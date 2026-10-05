"""Plain-assert tests (pytest not installed). Run: python tests/test_canon.py
canon.py: normalization, seed ontology, registry persistence, overrides, embedding / LLM / fallback stages (fake embedder),
and precision / recall on tests/fixtures/canon_cases.json (real cross-brand, cross-language equivalence cases)."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import canon  # noqa: E402

CASES = json.loads((HERE / "fixtures" / "canon_cases.json").read_text(encoding="utf-8"))["cases"]


def fake_embed(texts):
    """Deterministic stand-in for the multilingual embedding model: hashed character trigrams + word tokens."""
    out = []
    for t in texts:
        v = np.zeros(512, dtype=np.float32)
        s = f"  {t.lower()}  "
        grams = [s[i:i + 3] for i in range(len(s) - 2)] + [f"w:{w}" for w in t.lower().split()] * 2
        for g in grams:
            v[int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % 512] += 1.0
        out.append(v.tolist())
    return out


fake_embed.model = "fake-trigram"


def make(tmp, **kw):
    kw.setdefault("embed_fn", fake_embed)
    kw.setdefault("llm_fn", lambda prompt: None)
    kw.setdefault("offline", False)
    return canon.Canonicalizer(tmp, **kw)


def evaluate(cz, cases=CASES):
    """Pairwise precision / recall of the merge decisions: (precision, recall, mistakes)."""
    tp = fp = fn = 0
    bad = []
    for case in cases:
        labels, values = case["labels"], case.get("values") or [None] * len(case["labels"])
        fn_ = cz.canonicalize_item if case["space"] == "item" else cz.canonicalize
        kw = {"category": case["category"]}
        ids = [fn_(lab, value=val, **kw).id for lab, val in zip(labels, values)]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                same = ids[i] == ids[j]
                if case["same"] and same:
                    tp += 1
                elif case["same"]:
                    fn += 1
                    bad.append(f"MISSED  {labels[i]!r} / {labels[j]!r} -> {ids[i]} / {ids[j]}")
                elif same:
                    fp += 1
                    bad.append(f"MERGED  {labels[i]!r} / {labels[j]!r} -> {ids[i]}")
    return tp / max(tp + fp, 1), tp / max(tp + fn, 1), bad


def test_normalize_strips_marks_units_and_sections():
    n = canon.normalize("Weights & Dimensions > Cabinet Width (in) ")
    assert n.section == "weights & dimensions" and n.tokens == ("cabinet", "width") and n.unit_hint == "in"
    assert canon.normalize("ENERGY STAR® Qualified").tokens == ("energy", "star", "qualified")
    assert canon.normalize("Racks").tokens == canon.normalize("rack").tokens
    assert canon.normalize("Wi-Fi Connectivity").tokens == ("wifi", "connectivity")
    assert canon.normalize("EN[용량] > EN[전체 용량 (L)]").key == "전체용량" and canon.normalize("EN[용량] > EN[전체 용량 (L)]").unit_hint == "l"
    n = canon.normalize("Oven Interior Dimensions (Lower) (in) (W x H x D)")
    assert n.core_tokens == ("oven", "interior", "dimension") and "lower" in n.tokens
    assert canon.normalize("Details: Temperature Probe").section == "details"


def test_value_helpers():
    assert canon.parse_number("28-5/8") == 28.625 and canon.parse_number("52 1/16") == 52.0625 and canon.parse_number("1,200") == 1200
    assert canon.parse_measure("29.75 in")[:2] == (29.75, "in") and canon.parse_measure("1,200 w")[2] == "power"
    assert canon.parse_measure('28 5/8"')[0] == 28.625 and canon.parse_measure("602 ℓ")[2] == "volume"
    assert canon.parse_measure("Stainless Steel") is None and canon.parse_measure("2.2 Upper / 2.8 Lower") is None
    assert canon.to_unit(25.4, "mm", "in") == 1 and canon.to_unit(1, "kg", "lb") == 2.205 and canon.to_unit(10, "w", "in") is None
    assert canon.to_unit(38.3, "kwh/month", "kWh/yr") == 459.6 and round(canon.to_unit(602, "l", "cu ft"), 1) == 21.3
    assert canon.parse_dims('28 1/2" x 50 1/4" x 23 1/2"') == {"width": 28.5, "height": 50.25, "depth": 23.5}
    assert canon.parse_dims('27 5/8" W x 66 7/8" H x 31 7/8" D') == {"width": 27.625, "height": 66.875, "depth": 31.875}
    assert canon.parse_dims("912 × 1,853 × 683 mm", "크기(가로 × 높이 × 깊이)") == {"width": 35.906, "height": 72.953, "depth": 26.89}
    assert canon.parse_dims("52 1/16 x 29 3/4 x 23 1/2", "Overall Appliance Dimensions (HxWxD) (in)") == {"height": 52.062, "width": 29.75, "depth": 23.5}
    assert canon.parse_dims("49 3/4 - 51 1/2 x 28 1/2 x 23 1/2") is None and canon.parse_dims("30 in") is None
    assert canon.parse_vha("208/240V ; 60Hz ; 20A") == {"voltage": "208/240", "frequency": 60.0, "amps": 20.0}
    assert canon.flag_of("Yes (Air Fry)") is True and canon.flag_of("No - tempered glass") is False and canon.flag_of("N/A") is False
    assert canon.flag_of("EN(있음)") is True and canon.flag_of("없음") is False and canon.flag_of("Stainless") is None
    assert canon.value_kind("3.3 lbs") == "numeric" and canon.value_kind("Yes") == "flag" and canon.value_kind("Built-In") == "text"
    assert canon.split_items("Self Clean + Steam Clean") == ["Self Clean", "Steam Clean"]
    assert canon.split_items("Bake / Conv. Bake / Steam Cook(Steam Bake, Steam Roast)") == ["Bake", "Conv. Bake", "Steam Cook(Steam Bake, Steam Roast)"]


def test_seed_is_valid_and_big_enough():
    seed = json.loads((canon.ROOT / "data" / "canon_seed.json").read_text(encoding="utf-8"))
    attrs = seed["attrs"]
    assert len(attrs) >= 150 and len({a["id"] for a in attrs}) == len(attrs)
    assert set(seed["sections"]) == set(canon.SECTIONS) and all(a["sec"] in canon.SECTIONS for a in attrs)
    assert all(a["kind"] in ("numeric", "text", "flag", "list") and a["en"] and a["ko"] for a in attrs)
    for cat in canon.CATS:
        core = [a for a in attrs if cat in a["core"]]
        assert 40 <= len(core) <= 70, (cat, len(core))
    assert sum(len(a["syn"]) for a in attrs) >= 1200
    ids = {a["id"] for a in attrs}
    assert all(set(a.get("comp", [])) <= ids and all(t in ids for t in a.get("alt", {}).values()) for a in attrs)


def test_size_dimensions_overall_dimensions_and_korean_share_one_attribute():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        got = {lab: cz.canonicalize(lab, category="cooking") for lab in ("Size", "Dimensions", "Overall dimensions", "크기", "Product Dimensions")}
        assert {c.id for c in got.values()} == {"dimensions"} and all(c.method == "seed" for c in got.values())
        c = got["Size"]
        assert (c.label_en, c.label_ko, c.section, c.kind, c.core, c.score) == ("Dimensions", "크기", "치수·무게", "text", True, 1.0)
        assert c.composite == ("width", "height", "depth")
        w = cz.canonicalize("Weights & Dimensions > Overall Width", category="refrigerator", value="29.75 in")
        assert (w.id, w.kind, w.unit, w.section) == ("width", "numeric", "in", "치수·무게")
        assert cz.canonicalize("Capacity", category="washer").id == "washer-capacity" and cz.canonicalize("Capacity", category="refrigerator").id == "capacity-total"
        assert cz.canonicalize("Capacity", category="cooking").id == "oven-capacity"


def test_qualifiers_alt_units_and_core_by_category():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        up = cz.canonicalize("Upper Oven Features > Bake Element", category="cooking", value="375 W")
        lo = cz.canonicalize("Bake wattage (lower)", category="cooking")
        assert (up.id, lo.id) == ("bake-wattage:upper", "bake-wattage:lower") and "upper" in up.label_en and up.cav
        assert cz.canonicalize("Left Front Element-Burner Power", category="cooking").id == "burner-power:lf"
        assert cz.canonicalize("Right Rear", category="cooking", section="Burner").id == "burner-power:rr"
        assert cz.canonicalize("Power Consumption", category="refrigerator", value="446 kWh/yr").id == "energy-annual"
        assert cz.canonicalize("Power Consumption", category="refrigerator", value="120 W").id == "power-wattage"
        assert cz.canonicalize("Energy Rating (kWh/year)", category="cooking", value="356 kWh/year").id == "energy-annual"
        assert cz.canonicalize("Energy efficiency class", category="cooking", value="Tier 3").id == "energy-rating"
        assert cz.canonicalize("Capacity", category="washer", value="21 kg").id == "washer-capacity-kg"
        assert cz.canonicalize("Washer capacity", category="washer", value="5 cu ft").id == "washer-capacity"
        assert cz.canonicalize("Ice maker", category="refrigerator").core is True
        assert cz.canonicalize("Air Filter", category="refrigerator").core is True and cz.canonicalize("Air Filter", category="cooking").core is False
        assert cz.canonicalize("Steam & Self Clean", category="cooking").also == ("self-clean", "steam-clean")
        it = cz.canonicalize_item("Convect Bake", category="cooking")
        assert (it.id, it.kind, it.label_ko) == ("convection-bake", "list-item", "컨벡션 베이크")


def test_registry_grows_is_stable_and_recreated_when_missing():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        first = cz.canonicalize("Zorblax Capacity Index", category="cooking", value="7")
        assert first.method == "new" and first.id == "zorblax-capacity-index" and first.kind == "numeric" and first.core is False
        assert first.section == "용량"  # keyword classification (capacity)
        assert not (Path(d) / "canon_registry.json").exists()  # nothing written until save()
        cz.save()
        reg = json.loads((Path(d) / "canon_registry.json").read_text(encoding="utf-8"))
        assert "zorblax-capacity-index" in reg["attrs"]
        again = make(d, offline=True).canonicalize("Zorblax Capacity Index", category="cooking", value="9")
        assert again.id == first.id and again.method == "exact"
        (Path(d) / "canon_registry.json").unlink()
        assert make(d, offline=True).canonicalize("Zorblax Capacity Index", category="cooking", value="7").method == "new"  # recreated
        p = make(d, offline=True, persist=False)
        p.canonicalize("Another Novel Label", category="cooking")
        p.save()
        assert not (Path(d) / "canon_registry.json").exists() or "another-novel-label" not in (Path(d) / "canon_registry.json").read_text(encoding="utf-8")


def test_overrides_merge_and_split_win():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "canon_overrides.json").write_text(json.dumps({
            "merge": [{"labels": ["Zorblax Index"], "into": "dimensions"}, {"ids": ["door-alarm"], "into": "door-lock"}],
            "split": [{"labels": ["Net capacity"], "as": "net-capacity", "label_en": "Net capacity", "label_ko": "순 용량", "section": "용량",
                       "kind": "numeric"}]}), encoding="utf-8")
        cz = make(d, offline=True)
        m = cz.canonicalize("Zorblax Index", category="cooking")
        assert (m.id, m.method, m.score) == ("dimensions", "override", 1.0)
        assert cz.canonicalize("Door alarm", category="refrigerator").id == "door-lock"  # id redirect
        s = cz.canonicalize("Net capacity", category="refrigerator")
        assert (s.id, s.method, s.label_ko, s.section) == ("net-capacity", "override", "순 용량", "용량")
        assert cz.canonicalize("Capacity", category="refrigerator").id == "capacity-total"  # the seed still merges the rest


def test_embedding_merge_llm_grey_zone_and_guards():
    target = np.asarray(fake_embed(["overall dimension"])[0], dtype=np.float32)
    target /= np.linalg.norm(target)
    close = {}

    def embed(texts):
        out = []
        for t, v in zip(texts, fake_embed(texts)):
            if t in close:
                perp = np.asarray(v, dtype=np.float32)
                perp -= perp.dot(target) * target
                perp /= np.linalg.norm(perp)
                c = close[t]
                v = (c * target + (1 - c * c) ** 0.5 * perp).tolist()
            out.append(v)
        return out

    embed.model = "ctl"
    asked = []
    verdicts = lambda: [p for p in asked if "SAME specification" in p]  # noqa: E731 (section-classification prompts are not verdicts)

    def llm(prompt):
        asked.append(prompt)
        return {"same": True}

    with tempfile.TemporaryDirectory() as d:
        cz = make(d, embed_fn=embed, llm_fn=llm)
        close.update({"qwertyuiop": 0.9, "asdfghjkl": 0.78, "zxcvbnm": 0.5})
        hi = cz.canonicalize("Qwertyuiop", category="cooking")
        assert (hi.id, hi.method) == ("dimensions", "embed") and 0.85 < hi.score <= 1 and not hi.needs_review
        mid = cz.canonicalize("Asdfghjkl", category="cooking")
        assert (mid.id, mid.method) == ("dimensions", "llm") and mid.needs_review and len(verdicts()) == 1 and "Asdfghjkl" in verdicts()[0]
        lo = cz.canonicalize("Zxcvbnm", category="cooking")
        assert lo.method == "new" and len(verdicts()) == 1
        assert cz.canonicalize("Asdfghjkl", category="cooking").method == "llm" and len(verdicts()) == 1  # decision (and verdict) cached
        # numeric value vs a text attribute is incompatible: never merged by embedding
        close["qwertyuiop two"] = 0.95
        num = cz.canonicalize("Qwertyuiop Two", category="cooking", value="12 in")
        assert num.id != "dimensions" and num.method == "new"
        # discriminating words block a merge even with a high similarity
        close["height"] = 0.99
        assert cz.canonicalize("Overall Height Index", category="cooking").id != "width"
    with tempfile.TemporaryDirectory() as d:
        no = make(d, embed_fn=embed, llm_fn=lambda p: {"same": False})
        close["asdfghjkl"] = 0.78
        assert no.canonicalize("Asdfghjkl", category="cooking").method == "new"


def test_fallback_when_embeddings_unreachable_never_blocks():
    calls = []

    def down(texts):
        calls.append(1)
        return None

    with tempfile.TemporaryDirectory() as d:
        cz = make(d, embed_fn=down)
        c = cz.canonicalize("Convection Baking Mode Deluxe", category="cooking")
        assert c.method == "new" and len(calls) >= 1
        n_calls = len(calls)
        cz.canonicalize("Another Strange Label", category="cooking")
        assert len(calls) == n_calls  # circuit breaker: not hammering an unreachable server
        near = cz.canonicalize_item("Convect Bakes", category="cooking")  # stemmed + soft token match: still resolved without embeddings
        assert near.id == "convection-bake"
        assert cz.canonicalize("Freezer Volume", category="refrigerator").id != "capacity-fridge"
        off = make(d, offline=True)
        assert off.canonicalize("Total Capacity", category="refrigerator").id == "capacity-total"


def test_run_stats_count_methods_and_make_a_silent_degrade_visible():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        cz.begin()
        cz.canonicalize("Total Capacity", category="refrigerator")
        cz.canonicalize("Total Capacity", category="refrigerator")  # the same label twice counts once
        cz.canonicalize("Zorblax Capacity Index", category="cooking", value="7")
        cz.canonicalize("Zorblax Capacity Index", category="cooking", value="7")  # now a known attribute: still the same label
        assert cz.canonicalize_item("Convectional Bake", category="cooking").method == "fallback"  # deterministic merge, named as such
        s = cz.run_stats()
        assert s["methods"]["seed"] == 1 and s["methods"]["new"] == 1 and s["methods"]["fallback"] == 1 and s["labels"] == 3
        assert s["embed_stage"] == "offline"
        assert set(s["methods"]) == {"override", "seed", "exact", "registry", "embed", "llm", "fallback", "new", "rule"}
        assert "임베딩 단계: 사용 불가" in s["line_ko"] and "결정적 폴백" in s["line_ko"]
        cz.begin()
        assert cz.run_stats()["labels"] == 0  # per-run: begin() resets
        cz.canonicalize_item("Convectional Bake", category="cooking")
        assert cz.run_stats()["methods"]["registry"] == 1  # a decision of an earlier build, reused
    with tempfile.TemporaryDirectory() as d:
        cz = make(d)
        cz.begin()
        cz.canonicalize("Total Capacity", category="refrigerator")
        assert cz.run_stats()["embed_stage"] == "not_needed"  # nothing needed the fuzzy stage
        cz.canonicalize("Qwertyzzz Plinth", category="cooking")
        s = cz.run_stats()
        assert s["embed_stage"] == "used" and s["embed_ok"] >= 1 and s["embed_model"] == "fake-trigram" and "사용됨" in s["line_ko"]
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, embed_fn=lambda texts: None)
        cz.begin()
        cz.canonicalize("Qwertyzzz Plinth", category="cooking")
        s = cz.run_stats()
        assert s["embed_stage"] == "unavailable" and s["embed_fail"] == 1 and "사용 불가" in s["line_ko"]
        near = cz.canonicalize_item("Convectional Bake", category="cooking")
        assert near.id == "convection-bake" and near.method == "fallback" and cz.run_stats()["methods"]["fallback"] == 1
        assert canon.needs_review("fallback", 0.89) and not canon.needs_review("fallback", 0.95)  # reviewable like embedding merges


def test_fuzzy_merge_guards_found_on_real_live_mistakes():
    dh = canon.distinct_heads
    assert dh(("slow", "roast"), ("slow", "cook")) and dh(("standard", "rack"), ("glide", "rack"))
    assert dh(("language", "conversion"), ("temperature", "conversion")) and dh(("cook", "start"), ("auto", "cook"))
    assert not dh(("overall", "appliance", "width"), ("overall", "width")) and not dh(("freezer", "capacity", "total"), ("freezer", "capacity"))
    assert not dh(("qwertyuiop",), ("dimension",))      # nothing shared: the embedding / LLM decides
    assert not dh(("convect", "bake", "mode"), ("convection", "bake"))  # generic words and stems do not count
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        assert cz.canonicalize_item("Microwave Interior Light", category="cooking").id != cz.canonicalize("Oven Light Type", category="cooking").id
        assert cz.canonicalize_item("Reheat", category="cooking").id == "reheat"  # seed: never an 'air reheat' merge into Air fry
        near = cz.canonicalize_item("Self-Cleaning Oven Racks", category="cooking")
        assert near.id != "self-clean"  # a rack is not the self-clean cycle (noun guard)


def test_registry_entries_the_guards_refuse_today_are_forgotten_on_load():
    with tempfile.TemporaryDirectory() as d:
        reg = {"version": 1, "attrs": {}, "verdicts": {}, "labels": {
            "item|cooking|standard rack": {"id": "telescopic-rails", "m": "llm", "s": 0.74, "label": "1 Standard Rack"},
            "item|cooking|convectional bake": {"id": "convection-bake", "m": "embed", "s": 0.9, "label": "Convectional Bake"}}}
        (Path(d) / "canon_registry.json").write_text(json.dumps(reg), encoding="utf-8")
        cz = make(d, offline=True)
        assert "item|cooking|standard rack" not in cz._reg["labels"] and "item|cooking|convectional bake" in cz._reg["labels"]
        assert cz.run_stats()["registry_pruned"] == 1
        assert cz.canonicalize_item("Standard Rack", category="cooking").id != "telescopic-rails"


def test_known_is_a_side_effect_free_cheap_lookup():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        before = len(cz.attrs)
        assert cz.known("Overall Width", category="cooking") and not cz.known("Zorblax Capacity Index", category="cooking")
        assert len(cz.attrs) == before and cz.run_stats()["labels"] == 0


def test_item_space_is_separate_and_unknown_items_become_flag_attributes():
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        a = cz.canonicalize_item("Precision Cooking Modes", category="cooking")
        assert a.kind == "list-item" and a.method == "new" and a.id == "precision-cooking-modes"
        assert cz.canonicalize_item("Air Fry", category="cooking").id == cz.canonicalize("Air Fry", category="cooking").id == "air-fry"
        assert cz.canonicalize_item("Width", category="cooking").id != "width"  # numeric attributes are not items


def test_cases_precision_recall_with_fake_embedder():
    assert len(CASES) >= 60 and sum(1 for c in CASES if not c["same"]) >= 10
    with tempfile.TemporaryDirectory() as d:
        precision, recall, bad = evaluate(make(d))
    assert precision >= 0.95 and recall >= 0.85, (precision, recall, bad)
    print(f"      precision {precision:.3f}  recall {recall:.3f}  ({len(CASES)} cases, {len(bad)} mistakes)")


def test_korean_spec_table_labels_match_english_labels():
    import samsung_kr as sk
    rows = sk.parse_spec_html((HERE / "fixtures" / "samsung_kr" / "spec_fr.html").read_text(encoding="utf-8"))
    ko = {k: v for _s, k, v in rows}
    assert {"전체 용량", "냉장실 용량", "냉동실 용량", "컴프레서", "냉매", "무게"} <= set(ko)
    pairs = [("전체 용량", "Total capacity (cu ft)"), ("냉장실 용량", "Fresh food capacity"), ("냉동실 용량", "Freezer Capacity"),
             ("컴프레서", "Compressor Type"), ("냉매", "Refrigerant"), ("무게", "Net Weight"), ("제빙기", "Ice Maker"),
             ("얼음 종류", "Types of Ice"), ("도어 색상", "Door Color"), ("에너지소비효율등급", "Energy Efficiency Class"),
             ("정격 전압", "Rated Voltage"), ("냉각방식", "Cooling Type"), ("WiFi", "Wi-Fi Connectivity")]
    with tempfile.TemporaryDirectory() as d:
        cz = make(d, offline=True)
        for k, e in pairs:
            a = cz.canonicalize(k, category="refrigerator", value=ko.get(k)).id
            b = cz.canonicalize(e, category="refrigerator").id
            assert a == b and a in cz.attrs, (k, e, a, b)
        wrows = sk.parse_spec_html((HERE / "fixtures" / "samsung_kr" / "spec_wf.html").read_text(encoding="utf-8"))
        wko = {k: v for _s, k, v in wrows}
        assert cz.canonicalize("세탁 용량", category="washer", value=wko["세탁 용량"]).id == "washer-capacity-kg"
        assert cz.canonicalize("크기(폭 × 높이 × 깊이)", category="washer").id == cz.canonicalize("Overall Dimensions", category="washer").id
        assert cz.canonicalize("도어 타입", category="washer").id == cz.canonicalize("Door type", category="washer").id
        assert cz.canonicalize_item("에어프라이", category="cooking").id == cz.canonicalize_item("Air Fry", category="cooking").id
        # curated English keys of the KR adapters (units in parentheses) land on the same attributes as the US labels
        assert cz.canonicalize("Total capacity (L)", category="refrigerator", value="602").id == "capacity-total"
        assert cz.canonicalize("Width (mm)", category="refrigerator", value="912").id == "width"
        assert cz.canonicalize("Door count", category="refrigerator", value="4").id == "number-of-doors"
        assert cz.canonicalize("EN[용량] > EN[전체 용량 (L)]", category="refrigerator", value="871").id == "capacity-total"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
