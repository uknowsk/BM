"""Plain-assert offline tests (saved fixtures). Run: python tests/test_jennair_us.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog
import jennair_us as ja

FX = Path(__file__).parent / "fixtures" / "jennair_us"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _rec(m):
    prod = _j(f"prod_{m}.json")
    return ja.parse_product(m, ja.BASE + prod["url"], prod)


def _subs():
    data = _j("search_all.json")
    return {s: [c.model_number for c in ja.parse_search(data, s)] for s in ja.SUPPORTED_SUBCATEGORIES}


def test_contract_and_scope():
    assert (ja.COUNTRY, ja.REGION, ja.CURRENCY, ja.BRAND) == ("us", "na", "USD", "JennAir")
    assert catalog.brand_slug(ja.BRAND) + "_us" == "jennair_us"
    assert ja.SUPPORTED_SUBCATEGORIES == {"microwave", "otr", "sco", "electric_oven", "gas_oven", "gas_cooktop",
                                          "induction", "radiant"}
    assert all(s in catalog.CATEGORY_TREE["cooking"]["children"] for s in ja.SUPPORTED_SUBCATEGORIES)
    assert set(ja.SITE.codes) == ja.SUPPORTED_SUBCATEGORIES and ja.SITE.occ == "/ws/v2/jennAir-us"
    for bad in ("french_door", "built_in", "top_load", "dryer", "nope"):
        try:
            ja.discover(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_search_by_sub():
    got = _subs()
    assert got["otr"] == ["JMV8208CS", "JMV9196CS", "JMHF730RBL", "JMHF930RSS"]
    assert got["microwave"] == ["JMC1116AS", "JMC3415ES", "JMDFS30HL", "JMDFS24HM", "JMDFS30HM", "JMDFS24JL", "JMCTC15RB"]
    assert {"JMC6224HL", "JMC2427IL", "JMW2430LL", "JOECC530RL", "JOETC330SM"} <= set(got["sco"])  # speed ovens, combos, 7-in-1
    assert "JJW6024HL" in got["electric_oven"] and "JMC6224HL" not in got["electric_oven"] and len(got["electric_oven"]) == 19
    assert {"JGRP430HL", "JDRP548HM", "JGS1450ML", "JDS1450ML"} <= set(got["gas_oven"])  # gas, dual fuel, slide-in
    assert {"JGCP436HL", "JGC3536GS", "JGD3430GB", "JGC3115GS"} <= set(got["gas_cooktop"])  # rangetops, cooktops, custom
    assert not set(got["gas_cooktop"]) & set(got["gas_oven"])
    assert {"JIC4530KS", "JIC4715GS", "JPIFC736RL", "JIS1450ML", "JIDT836SBL"} <= set(got["induction"])
    assert {"JEC3430HS", "JED3536GB", "JES1450ML", "JEC4536KB"} <= set(got["radiant"]) and "JEF3115GS" not in got["radiant"]
    everything = [c for v in got.values() for c in v]
    assert len(everything) == len(set(everything))  # one sub key per model
    for skipped in ("JFFCF72DKL", "JBRFL30IGX", "JUGFR242HL"):  # refrigerators are out of scope
        assert skipped not in everything
    for other in ("JBZFR18IGX", "JUIFN15HX", "W11751830"):  # freezer, ice machine, part
        assert other not in everything


def test_candidate_fields():
    c = {x.model_number: x for x in ja.parse_search(_j("search_all.json"), "gas_cooktop")}["JGCP436HL"]
    assert (c.brand, c.category, c.subcategory, c.price_usd) == ("JennAir", "cooking", "gas_cooktop", 4899.0)
    assert c.url.startswith("https://www.jennair.com/") and "™" not in c.name


def test_records():
    r, raw = _rec("JGRP430HL")
    assert (r.brand, r.category, r.subcategory, r.price_usd) == ("JennAir", "cooking", "gas_oven", 5899.0)
    assert r.capacity_total_cuft == 4.1 and (r.width_in, r.height_in, r.depth_in, r.weight_lb) == (29.875, 38.75, 29.1875, 358.0)
    assert r.wifi_supported is True and len(r.extra_specs) == len(raw) == 85 and all(" > " in k for k in r.extra_specs)
    assert r.image_url.startswith("https://www.jennair.com/is/image/") and r.image_url.endswith("?fmt=jpeg&wid=1200")
    for m, sub, cap in (("JGCP436HL", "gas_cooktop", None), ("JIC4530KS", "induction", None), ("JEC3430HS", "radiant", None),
                        ("JMC1116AS", "microwave", 1.6), ("JMV9196CS", "otr", None), ("JMC6224HL", "sco", 1.4),
                        ("JOEDC330RL", "electric_oven", 10.0)):
        r, _ = _rec(m)
        assert (r.subcategory, r.capacity_total_cuft) == (sub, cap), m
    listing = {c.model_number: c.subcategory for s in ja.SUPPORTED_SUBCATEGORIES
               for c in ja.parse_search(_j("search_all.json"), s)}
    for m in ("JGRP430HL", "JGCP436HL", "JIC4530KS", "JEC3430HS", "JMC1116AS", "JMV9196CS", "JMC6224HL", "JOEDC330RL"):
        assert _rec(m)[0].subcategory == listing[m], m  # discover and scrape share classify()


def test_docs_model_url_and_modes():
    docs = ja.parse_docs(_j("docs_JGRP430HL.json"))
    assert [t for t, _ in docs] == ["Manual", "SpecSheet", "Installation", "QuickSpecs"]
    assert all(u.startswith("https://www.jennair.com/") for _, u in docs)
    assert ja.model_from_url("https://www.jennair.com/ranges/gas/p.rise-30-gas-professional-range.jgrp430hl.html") == "JGRP430HL"
    for bad in ("https://www.maytag.com/p.a.jgrp430hl.html", "https://eviljennair.com/p.a.jgrp430hl.html"):
        try:
            ja.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert ja._modes() in ([True, False], [False, True]) and callable(ja.reset_browser_mode)
    assert ja._engine._search_path(ja.SITE, "Cooktops", 0).startswith("/ws/v2/jennAir-us/")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
