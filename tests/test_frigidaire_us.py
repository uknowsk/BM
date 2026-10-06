"""Plain-assert offline tests (saved fixtures). Run: python tests/test_frigidaire_us.py
Covers frigidaire_us and the US half of _electrolux_common (classification, listing -> Candidate, product -> record)."""
import contextlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import catalog
import frigidaire_us as f

FX = Path(__file__).parent / "fixtures" / "frigidaire_us"
PDP = "https://www.frigidaire.com/en/p/"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


class FakeSession:
    """Answers OCC calls from fixtures: search by category code, product by code."""

    def __init__(self, searches=None, products=None):
        self.searches, self.products, self.calls = searches or {}, products or {}, []

    def json(self, url):
        self.calls.append(url)
        for code, data in self.searches.items():
            if code in url:
                return data
        for code, data in self.products.items():
            if f"/products/{code}?" in url:
                return data
        raise RuntimeError(f"unexpected fetch {url}")


@contextlib.contextmanager
def fake_session(sess):
    orig_s, orig_sleep = ec.session, ec.time.sleep
    ec.session = lambda *a, **k: contextlib.nullcontext(sess)
    ec.time.sleep = lambda s: None
    try:
        yield
    finally:
        ec.session, ec.time.sleep = orig_s, orig_sleep


def test_supported_subcategories_are_cooking_only_and_known():
    cooking = set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert f.SUPPORTED_SUBCATEGORIES == cooking  # Frigidaire sells every cooking type incl. OTR and gas cooktops
    assert not f.SUPPORTED_SUBCATEGORIES & set(catalog.sub_keys("refrigerator") + catalog.sub_keys("washer"))
    assert (f.COUNTRY, f.REGION, f.CURRENCY) == ("us", "na", "USD")


def test_classify_rules():
    c = f.classify
    assert c(["M_FoodPreparation_Microwaves_OverTheRange"], "1.9 Cu. Ft. Over-The-Range Microwave") == "otr"
    assert c(["M_FoodPreparation_Microwaves_BuiltIn"], "30\" Built-In Microwave Oven") == "microwave"
    assert c(["M_FoodPreparation_WallOvens_MicrowaveCombination"], "Wall Oven and Microwave Combination") == "sco"
    assert c(["M_FoodPreparation_WallOvens_Single"], "30\" Single Wall Oven with microwave") == "sco"
    assert c(["M_FoodPreparation_WallOvens_Double"], "30\" Double Electric Wall Oven") == "electric_oven"
    assert c(["M_FoodPreparation_WallOvens_Single"], "30\" Single Gas Wall Oven") == "gas_oven"  # gas wall oven
    assert c(["M_FoodPreparation_Ranges_Gas"], "30\" Gas Range") == "gas_oven"
    assert c(["M_FoodPreparation_Ranges_DualFuel"], "36\" Dual-Fuel Range") == "gas_oven"
    assert c(["M_FoodPreparation_Ranges_Induction"], "30\" Induction Range") == "induction"
    assert c(["M_FoodPreparation_Ranges_Electric"], "30\" Electric Range") == "radiant"
    assert c(["M_FoodPreparation_Cooktops_Gas"], "30\" Gas Cooktop") == "gas_cooktop"  # no oven -> gas_cooktop
    assert c(["M_FoodPreparation_Cooktops_Induction"], "30\" Induction Cooktop") == "induction"
    assert c(["M_FoodPreparation_Cooktops_Electric"], "36\" Electric Cooktop") == "radiant"
    assert c(["CC_Majors_BestSellers", "M_FoodPreparation_Ranges_Gas"], "x") == "gas_oven"  # unrelated codes ignored
    assert c(["M_FoodPreservation_Refrigerators_FrenchDoor"], "French Door Refrigerator") is None
    assert c([], "") is None
    assert c(["M_FoodPreparation_Cooktops"], "36\" Induction Cooktop") == "induction"  # parent category: by name


def test_frac_and_clean_helpers():
    assert ec.frac_in('35 5/8"') == 35.625 and ec.frac_in('30"') == 30.0 and ec.frac_in('67 9/10"') == 67.9
    assert ec.frac_in("n/a") is None and ec.frac_in(None) is None
    assert ec.clean("﻿﻿Wi-Fi® Enabled ") == "Wi-Fi Enabled"
    t = {}
    ec.put_spec(t, "k", "a")
    ec.put_spec(t, "k", "a")
    ec.put_spec(t, "k", "b")
    assert t == {"k": "a | b"}


def test_discover_builds_candidates_from_listing_fixture():
    sess = FakeSession(searches={"Ranges_Gas": _j("search_gas_ranges.json"), "Ranges_DualFuel": {"products": []}})
    with fake_session(sess):
        got = f.discover("gas_oven", 3)
    assert [c.model_number for c in got] == ["GCFG3060BF-A1", "GCFG3070BF-A2"]
    c = got[0]
    assert c.brand == "Frigidaire" and (c.category, c.subcategory) == ("cooking", "gas_oven")
    assert (c.region, c.country, c.currency) == ("na", "us", "USD") and c.price_usd == 1499.0 and c.price_local is None
    assert c.url == PDP + "kitchen/ranges/gas-ranges/GCFG3060BF-A1"
    assert c.attrs["fuel"] == "gas" and c.attrs["width_in"] == 30.0 and c.attrs["finish"] == "stainless"
    assert all(v == "listing" for v in c.attrs_src.values())
    assert "query=%3Arelevance%3AallCategories%3AM_FoodPreparation_Ranges_Gas" in sess.calls[0]
    with fake_session(FakeSession(searches={"Ranges_Gas": _j("search_gas_ranges.json")})):
        assert len(f.discover("gas_oven", 1)) == 1  # limit honoured
    try:
        f.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        f.discover("top_load")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_discover_drops_items_classified_elsewhere_and_bad_ids():
    d = _j("search_wall_ovens.json")
    good = d["products"][1]
    d["products"][0]["name"] = "30\" Single Wall Oven and Microwave"  # microwave -> sco, not electric_oven
    d["products"][1] = dict(good, url="/c//p/bad code!")
    d["products"].append(good)
    sess = FakeSession(searches={"WallOvens_Single": d, "WallOvens_Double": {"products": []}})
    with fake_session(sess):
        got = f.discover("electric_oven", 10)
    assert [c.model_number for c in got] == [good["url"].rsplit("/", 1)[-1]]


def _parse(code):
    p = _j(f"product_{code}.json")
    return f.parse_product(code, PDP + "x/" + code, p), p


def test_gas_range_record_is_complete():
    (rec, raw), p = _parse("GCFG3060BF-A1")
    assert rec.subcategory == "gas_oven" and rec.category == "cooking" and rec.price_usd == 1499.0
    assert (rec.region, rec.country, rec.currency) == ("na", "us", "USD")
    assert (rec.width_in, rec.height_in, rec.depth_in) == (30.0, 35.625, 28.5) and rec.weight_lb == 204.0
    assert rec.voltage_v == "120" and rec.amps == 12.0 and rec.finish_color
    assert rec.extra_specs["Cooktop > Total Number of Burners"] == "5"
    assert rec.extra_specs["Dimensions and Volume > Oven Capacity"] == "6 Cu. Ft."
    assert rec.extra_specs["Cooktop Performance > Center Element Burner"] == "10000 BTU"
    assert len(raw) >= 60 and all(r.source == "web" and r.model_number == "GCFG3060BF-A1" for r in raw)
    assert all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw)  # every row is in the sectioned table
    assert rec.image_url.startswith("https://frigidaire.bynder.com/") and rec.pod_features
    assert rec.wifi_supported is None  # no Wi-Fi row on this model: unknown, not False
    assert "﻿" not in "".join(rec.extra_specs)


def test_combo_oven_cooktop_and_otr_records():
    (rec, _), _ = _parse("GCWM3070AF-A1")
    assert rec.subcategory == "sco" and rec.wifi_supported is True and "Wi-Fi Enabled: Yes" in rec.wifi_evidence
    assert rec.extra_specs["Microwave Cooking > Microwave Cooking Power"] == "1000 Watts"
    assert rec.voltage_v == "240"  # from 'Connected Load @ 240V'
    (rec, _), _ = _parse("GCCG3048AS")
    assert rec.subcategory == "gas_cooktop" and rec.extra_specs["Cooktop > Total Number of Burners"] == "5"
    (rec, _), _ = _parse("GMOS1962AF")
    assert rec.subcategory == "otr" and rec.extra_specs["Dimensions and Volume > Microwave Capacity"] == "1.9 Cu. Ft."
    (rec, _), _ = _parse("GCFI3060BF-A1")
    assert rec.subcategory == "induction"


def test_docs_picks_english_pdfs_once_per_type():
    docs = dict(ec.us_docs(_j("product_GCFG3060BF-A1.json")))
    assert docs and set(docs) <= {"EnergyGuide", "Installation", "SpecSheet", "QuickSpecs", "Manual"}
    assert all(u.startswith("https://frigidaire.bynder.com/") and u.endswith(".pdf") for u in docs.values())
    assert "_fr-pdf" not in " ".join(docs.values()) and "Right_To_Repair" not in " ".join(docs.values())
    assert ec.us_docs({"ownerManuals": [{"altText": "Owner's Manual", "downloadUrl": "http://x/a.pdf", "language": "en"}]}) == []


def test_model_from_url_rejects_foreign_hosts_and_shapes():
    assert ec.us_model_from_url(f.SITE, PDP + "kitchen/ranges/gas-ranges/GCFG3060BF-A1") == "GCFG3060BF-A1"
    assert ec.us_model_from_url(f.SITE, PDP + "GCFG3060BF-A1") == "GCFG3060BF-A1"
    for bad in ("http://www.frigidaire.com/en/p/GCFG3060BF-A1", "https://evil.example.com/en/p/GCFG3060BF-A1",
                "https://www.frigidaire.com.evil.com/en/p/GCFG3060BF-A1", "https://www.frigidaire.com/en/kitchen/x",
                "https://www.frigidaire.com/en/p/", "https://www.frigidaire.com/en/p/bad%20code"):
        try:
            f.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_scrape_uses_product_endpoint_and_downloads_docs():
    p = _j("product_GCFG3060BF-A1.json")
    sess = FakeSession(products={"GCFG3060BF-A1": p})
    seen = []
    orig = ec.fetch_docs
    ec.fetch_docs = lambda brand, model, wanted: (seen.append((brand, model, wanted)), [])[1]
    try:
        with fake_session(sess):
            rec, docs, raw = f.scrape(PDP + "kitchen/ranges/gas-ranges/GCFG3060BF-A1")
    finally:
        ec.fetch_docs = orig
    assert rec.model_number == "GCFG3060BF-A1" and rec.subcategory == "gas_oven" and docs == [] and raw
    assert "apolloapi.electrolux.com/occ/v2/frigidaire/products/GCFG3060BF-A1?fields=FULL" in sess.calls[0]
    assert seen[0][0] == "Frigidaire" and seen[0][2]
    # the API answering another code is an error, never silently another product
    with fake_session(FakeSession(products={"GCFG3060BF-A1": dict(p, code="OTHER")})):
        try:
            f.scrape(PDP + "GCFG3060BF-A1")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_browser_modes_and_headless_fallback():
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        for val, want in (("headless", [True]), ("visible", [False]), ("auto", [True, False]), ("", [True, False])):
            os.environ["FRIDGE_BROWSER_MODE"] = val
            assert ec.browser_modes() == want
    finally:
        if old is None:
            os.environ.pop("FRIDGE_BROWSER_MODE", None)
        else:
            os.environ["FRIDGE_BROWSER_MODE"] = old

    class Good:
        closed = False

        def new_context(self, **k):
            return self

        def new_page(self):
            return self

        def goto(self, *a, **k):
            return type("R", (), {"status": 200})()

        def wait_for_function(self, *a, **k):
            return None

        def inner_text(self, sel):
            return "x" * 500

        url = "https://www.frigidaire.com/en/"

        def close(self):
            Good.closed = True

    class Bad(Good):
        closed = False

        def goto(self, *a, **k):
            raise ec.PlaywrightError("net::ERR_HTTP2_PROTOCOL_ERROR")

        def close(self):
            Bad.closed = True

    seq = [Bad(), Good()]
    patches = [(ec.common, "launch_browser", lambda p, headless=None: seq.pop(0)), (ec, "browser_modes", lambda: [True, False]),
               (ec, "dismiss_consent", lambda page: None)]
    orig = [(o, n, getattr(o, n)) for o, n, _ in patches]
    for o, n, v in patches:
        setattr(o, n, v)
    ec._HEADLESS_FAILED.discard("www.frigidaire.com")
    try:
        browser, page = ec.connect(None, "https://www.frigidaire.com/en/", ("frigidaire.com",))
    finally:
        for o, n, v in orig:
            setattr(o, n, v)
        ec._HEADLESS_FAILED.discard("www.frigidaire.com")
    assert isinstance(browser, Good) and Bad.closed and not seq  # headless refused -> closed -> visible used
    try:
        ec.connect(None, "https://evil.example.com/", ("frigidaire.com",))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_review_and_new_signals_default_to_unknown():
    """Live (2026-10): no Frigidaire cooking product has a review (FULL product: numberOfReviews 0, no averageRating) and the PLP
    primaryFlag is only 'Best Seller' / 'Top Rated' / 'Best Deal', so the real data yields no signal keys."""
    got = ec.us_candidate(f.SITE, _j("search_gas_ranges.json")["products"][0], "gas_oven", "M_FoodPreparation_Ranges_Gas")
    assert not {"rating", "review_count", "is_new", "release_date"} & set(got.attrs)
    (rec, _), p = _parse("GCFG3060BF-A1")
    assert (rec.rating, rec.review_count, rec.is_new, rec.release_date, rec.release_src) == (None,) * 5
    p0 = dict(p, numberOfReviews=0, colorVariants=[{"code": "GCFG3060BF-A1", "primaryFlag": "Best Seller"}])
    assert f.parse_product("GCFG3060BF-A1", PDP + "x/GCFG3060BF-A1", p0)[0].is_new is None


def test_review_and_new_signals_when_the_site_provides_them():
    # OCC field names (numberOfReviews / averageRating) and the 'New' flag text are exercised with a modified copy of the product
    (_, _), p = _parse("GCFG3060BF-A1")
    p2 = dict(p, numberOfReviews=7, averageRating=4.3, colorVariants=[{"code": "GCFG3060BF-A1", "primaryFlag": "New"}])
    rec, _ = f.parse_product("GCFG3060BF-A1", PDP + "x/GCFG3060BF-A1", p2)
    assert (rec.rating, rec.review_count, rec.is_new) == (4.3, 7, True)
    item = _j("search_gas_ranges.json")["products"][0]
    item["colorVariants"][0]["primaryFlag"] = "New"
    c = ec.us_candidate(f.SITE, item, "gas_oven", "M_FoodPreparation_Ranges_Gas")
    assert c.attrs["is_new"] is True and c.attrs_src["is_new"] == "listing"
    item["colorVariants"][0]["primaryFlag"] = "Best Seller"
    assert "is_new" not in ec.us_candidate(f.SITE, item, "gas_oven", "M_FoodPreparation_Ranges_Gas").attrs
    assert ec.review_signal(4.3, 0) == {} and ec.review_signal(None, 3) == {} and ec.review_signal(6, 3) == {}


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
