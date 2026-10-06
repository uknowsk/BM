"""Plain-assert offline tests (saved fixtures). Run: python tests/test_smeg_us.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smeg_us as s

FX = Path(__file__).parent / "fixtures" / "smeg_us"
PDP = "https://www.smeg.com/us/products/"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def test_supported_are_cooking_only():
    import catalog
    assert s.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking")) and "gas_cooktop" in s.SUPPORTED_SUBCATEGORIES
    assert s.COUNTRY == "us" and s.REGION == "na" and s.CURRENCY == "USD"
    try:
        s.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_classify_rules():
    c = s.classify
    assert c('Professional | Range | 36" | Professional | Stainless steel | Full Gas | Cooktop type: Gas') == "gas_oven"
    assert c("Range | 48\" | Professional | Dual-Fuel | Main oven: Thermo-ventilated") == "gas_oven"
    assert c('Professional | Range | 36" | Full electric | Cooktop type: Induction') == "induction"
    assert c("Cooktop | Gas | 36\" | Stainless steel") == "gas_cooktop"
    assert c("Cooktop | Induction | 30\"") == "induction" and c("Cooktop | Ceramic | 24\"") == "radiant"
    assert c("Oven | Cooking method: Combi Microwave | Compact 24\"") == "sco"
    assert c("Oven | Cooking method: Microwave with grill | Compact 24\"") == "microwave"
    assert c("Oven | Cooking method: Convection | 30\"") == "electric_oven"
    assert c("Oven | Cooking method: Combi Steam") == "electric_oven"
    assert c("Hood | Under cabinet | 30 \"") is None and c("Refrigerator | French-door") is None and c("") is None


def test_listing_cards_and_candidates():
    cards = s.parse_listing(_t("listing_ranges_all.html"))
    assert len(cards) == 27 and cards[0][0] == "SPR36UGGAN"
    gas = [m for m, d in cards if s.classify(d) == "gas_oven"]
    ind = [m for m, d in cards if s.classify(d) == "induction"]
    assert len(gas) == 18 and len(ind) == 9
    cand = s._candidate("SPR36UGGAN", cards[0][1], "gas_oven")
    assert cand.url == PDP + "SPR36UGGAN" and cand.price_usd is None and cand.country == "us" and cand.category == "cooking"
    assert cand.attrs == {"width_in": 36.0, "fuel": "gas"}
    cook = s.parse_listing(_t("listing_cooktops_all.html"))
    assert sorted({s.classify(d) for _m, d in cook}) == ["gas_cooktop", "induction", "radiant"]
    ovens = s.parse_listing(_t("listing_ovens_all.html"))
    assert {s.classify(d) for _m, d in ovens} == {"electric_oven", "sco"}


def test_discover_uses_listings_and_filters_by_sub():
    seen = []
    orig = s.fetch_html
    s.fetch_html = lambda url, probe: seen.append(url) or _t("listing_cooktops_all.html")
    try:
        got = s.discover("gas_cooktop", limit=3)
    finally:
        s.fetch_html = orig
    assert len(got) == 3 and all(c.subcategory == "gas_cooktop" and c.attrs["fuel"] == "gas" for c in got)
    assert seen == [s.BASE + "/cooktops/all"]


def test_scrape_parse_fixtures():
    for model, sub, w, wt, volt in (("SPR36UIMX", "induction", 35.875, 215.0, "208/240"),
                                    ("SOCU3304MCX", "sco", 29.6875, 102.0, "120/240"),
                                    ("PGFU36X2", "gas_cooktop", 36.0, 46.0, "100-120")):
        html = _t(f"pdp_{model}.html")
        rec, raw = s.parse_product(model, PDP + model, html)
        assert rec.subcategory == sub and rec.category == "cooking" and rec.price_usd is None, model
        assert rec.width_in == w and rec.weight_lb == wt and rec.voltage_v == volt, model
        assert rec.product_name.startswith("Smeg ") and model in rec.product_name, model
        assert all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw) and len(raw) >= 40, model
        assert rec.image_url.startswith("https://assets.4flow.cloud/" + model), model
    html = _t("pdp_SOCU3304MCX.html")
    assert dict(s.doc_links(html)).keys() >= {"Installation", "Manual"}
    assert all(u.startswith("https://doc.smeg.it/") for _t_, u in s.doc_links(html))


def test_url_and_helpers():
    assert s.model_from_url(PDP + "SPR36UIMX") == "SPR36UIMX"
    for bad in ("http://www.smeg.com/us/products/X123", "https://evil.example.com/us/products/SPR36UIMX",
                PDP + "../x", "https://www.smeg.com/us/ranges/all"):
        try:
            s.model_from_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert s._frac_in('27 11/16 "') == 27.6875 and s._frac_in('36 "') == 36.0 and s._frac_in("n/a") is None
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        assert s._strategies() == ["requests", "headless", "visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert s._strategies() == ["headless"]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert s._strategies() == ["visible"]
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


def test_new_flag_from_listing_card_class():
    html = _t("listing_ranges_all.html")
    assert s.new_models(html) == set()  # IS_NEW_0 everywhere: not flagged, key omitted
    cards = s.parse_listing(html)
    assert "is_new" not in s._candidate(cards[0][0], cards[0][1], "gas_oven").attrs
    flagged = html.replace("IS_NEW_0", "IS_NEW_1", 1)
    assert s.new_models(flagged) == {cards[0][0]}
    cand = s._candidate(cards[0][0], cards[0][1], "gas_oven", cards[0][0] in s.new_models(flagged))
    assert cand.attrs["is_new"] is True and cand.attrs_src["is_new"] == "listing" and cand.attrs["fuel"] == "gas"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
