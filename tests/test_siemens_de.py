"""Plain-assert offline tests for siemens_de (trimmed fixtures). Run: python tests/test_siemens_de.py"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"  # glossary + rules only: deterministic, no local LLM
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import siemens_de as s
import thermador_us as t

FX = Path(__file__).parent / "fixtures" / "siemens_de"
P = "https://www.siemens-home.bsh-group.com/de/de/product/kochen-backen/"
URLS = {
    "ED645HQC1E": P + "kochfelder-kochstellen/induktionskochfelder/ED645HQC1E",
    "HM776GKB1": P + "backoefen-herde/backoefen-mit-mikrowelle/HM776GKB1",
    "EC6A5HB90D": P + "kochfelder-kochstellen/gaskochfelder/EC6A5HB90D",
}


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


def _parse(m):
    return s.parse_product(m, URLS[m], t.flight_text(_t(f"pdp_{m}.html")))


def test_constants_and_supported():
    assert (s.COUNTRY, s.REGION, s.CURRENCY, s.BRAND) == ("de", "eu", "EUR", "Siemens")
    assert s.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "electric_oven", "gas_cooktop", "induction", "radiant"}
    for sub in ("french_door", "front_load", "gas_oven", "otr"):
        try:
            s.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


def test_classify():
    c = s.classify
    kb = "/product/kochen-backen/"
    assert c(kb + "backoefen-herde/einbau-backoefen/HB314G0S3", "iQ300 Einbau-Backofen 60 x 60 cm Edelstahl") == "electric_oven"
    assert c(kb + "backoefen-herde/backoefen-mit-mikrowelle/HM776GKB1", "Einbau-Backofen mit Mikrowellenfunktion") == "sco"
    assert c(kb + "backoefen-herde/kompakt-backoefen/CM776GKB1", "Einbau-Kompaktbackofen mit Mikrowellenfunktion") == "sco"
    assert c(kb + "backoefen-herde/kompakt-backoefen/BE732L1B1", "iQ700 Einbau-Mikrowelle mit Grill") == "microwave"
    assert c(kb + "mikrowellen/freistehend/FF020LMW0", "Mikrowelle freistehend") == "microwave"
    assert c(kb + "dampfgarer-dampfbackoefen/dampfbackoefen/HQ372G0S3", "Einbau-Dampfbackofen") == "electric_oven"
    assert c(kb + "dampfgarer-dampfbackoefen/dampfgarer/CD714GXB1", "Einbau-Dampfgarer") is None
    assert c(kb + "backoefen-herde/einbau-herde/HB778G9B1", "Einbau-Herd") is None
    assert c(kb + "kochfelder-kochstellen/gaskochfelder/EC6A5HB90D", "Gaskochfeld 60 cm") == "gas_cooktop"
    assert c(kb + "kochfelder-kochstellen/induktionskochfelder/ED645HQC1E", "Induktionskochfeld 60 cm") == "induction"
    assert c(kb + "kochfelder-kochstellen/kochfelder-integrierter-dunstabzug/ED611BS16E", "Induktionskochfeld mit Dunstabzug") == "induction"
    assert c(kb + "kochfelder-kochstellen/glaskeramik-kochfelder/EA601GN17", "Elektro-Kochfeld 60 cm") == "radiant"
    assert c(kb + "zubehoer/x/Z123456", "Zubehör") is None
    assert c("/product/waeschepflege/waschmaschinen/frontlader/WG44B2040", "Waschmaschine") is None  # out of scope


def test_listing_candidates():
    items, total = t.parse_listing(_t("listing_gaskochfelder.html"))
    assert total == 4 and len(items) == 4
    cands = [s.item_to_candidate(i, "gas_cooktop") for i in items]
    assert [c.model_number for c in cands] == ["EC6A5HI90", "ER9A6SH40", "ER7A6RH40", "ER6A6PH40"]
    c = cands[1]
    assert (c.brand, c.category, c.subcategory, c.country, c.region, c.currency) == ("Siemens", "cooking", "gas_cooktop", "de", "eu", "EUR")
    assert c.price_usd is None and c.name == "iQ700 Gaskochfeld 90 cm Hartglas, Schwarz"
    assert c.attrs["width_in"] == 35.4 and c.attrs["finish"] == "black" and c.attrs["wifi"] is True
    assert c.url.startswith("https://www.siemens-home.bsh-group.com/de/de/product/kochen-backen/")
    assert s.item_to_candidate(items[0], "induction") is None


def test_candidate_price_and_filters():
    ok = {"productCode": "ED1234", "urlPath": "/product/kochen-backen/kochfelder-kochstellen/induktionskochfelder/ED1234",
          "productName": ["iQ300", "Induktionskochfeld 60 cm", "Mit Rahmen"],
          "price": {"kind": "SHOP_PRICE", "vatIncluded": True, "amount": 629, "currency": "EUR"}}
    c = s.item_to_candidate(ok, "induction")
    assert c.price_local == 629.0 and c.price_usd is None
    for bad in ({**ok, "urlPath": "/product/waeschepflege/trockner/x/ED1234"}, {**ok, "urlPath": "//evil.com/product/kochen-backen/x"},
                {**ok, "productCode": "../x"}, {**ok, "productCode": None}):
        assert s.item_to_candidate(bad, "induction") is None, bad
    assert s.finish_de("Schwarz, Edelstahl") == "stainless" and s.finish_de("Weiß") == "white"


def test_site_signals():
    # real listing excerpt (EC6A5HI90: 5 stars from 2 ratings); items without a rating stay unknown
    items, _ = t.parse_listing(_t("listing_gaskochfelder.html"))
    assert s.item_to_candidate(items[0], "gas_cooktop").attrs.get("rating") is None
    rated = {**items[0], "rating": {"average": 5, "count": 2}}
    c = s.item_to_candidate(rated, "gas_cooktop")
    assert c.attrs["rating"] == 5.0 and c.attrs["review_count"] == 2 and c.attrs_src["review_count"] == "listing"
    # PDP: schema.org aggregateRating + product flags; fixtures carry none -> record fields stay unset
    flight = t.flight_text(_t("pdp_ED645HQC1E.html"))
    assert t.page_signals(flight) == {}
    extra = '"aggregateRating":{"@type":"AggregateRating","bestRating":5,"ratingValue":4.5,"reviewCount":8}'
    r, _ = s.parse_product("ED645HQC1E", URLS["ED645HQC1E"], flight + extra)
    assert (r.rating, r.review_count, r.is_new, r.release_date) == (4.5, 8, None, None)
    r0, _ = _parse("ED645HQC1E")
    assert (r0.rating, r0.review_count, r0.release_src) == (None, None, None)


def test_induction_hob_record():
    r, raw = _parse("ED645HQC1E")
    assert (r.brand, r.category, r.subcategory, r.country, r.currency) == ("Siemens", "cooking", "induction", "de", "EUR")
    assert r.product_name == "iQ500 Induktionskochfeld 60 cm Mit Rahmen aufliegend" and r.finish_color == "Black"
    assert (r.height_in, r.width_in, r.depth_in) == (2.2, 23.0, 20.2)  # 55 x 583 x 513 mm
    assert r.wifi_supported is True and "Remote" in r.wifi_evidence and r.price_usd is None
    x = r.extra_specs
    assert x["Design > Hob type"] == "Induction hob" and x["Installation > Connected load"] == "7400 W"
    assert x["Installation > Appliance dimensions (H x W x D) (mm)"] == "55x583x513 mm"
    assert x["Comfort > Favoriten-Taste"] == "Yes"  # boolean normalised even when the label stays German offline
    assert len(raw) == 50 and any(r_.key == "Kochfeldart" and r_.value == "Induktionskochfeld" for r_ in raw)  # German original kept
    assert r.image_url.startswith("https://media3.bsh-group.com/Product_Shots/")


def test_combi_oven_record_units_and_class():
    r, _ = _parse("HM776GKB1")
    assert r.price_local == 1789.0 and r.currency == "EUR" and r.price_usd is None
    assert (r.height_in, r.width_in, r.depth_in) == (23.4, 23.4, 21.6) and r.weight_lb == 97.0
    assert r.voltage_v == "220-240" and r.amps == 16.0 and r.finish_color == "Black, Stainless steel"
    assert r.extra_specs["General > Maximum microwave power"] == "800 W"
    r2, _ = _parse("EC6A5HB90D")
    assert r2.price_local is None and r2.wifi_supported is False and r2.extra_specs["Design > Colour"] == "Stainless steel"


def test_eu_class_in_extra_specs_only():
    sections = [{"name": "Allgemein", "key": "GENERAL_PROPERTIES", "specifications": [
        {"key": "ENERGY_CLASS_2017", "name": {"text": "Energieeffizienzklasse"}, "value": {"text": "C"}, "unit": None},
        {"key": "ENERGY_CONS_ANNUAL_2017", "name": {"text": "Verbrauch"}, "value": {"text": "333"}, "unit": "kWh/annum"}]}]
    flight = ('{"pricing":null,"title":{"valueClass":"iQ300","headline":"Test"},"specifications":'
              + __import__("json").dumps(sections, separators=(",", ":")) + "}")
    r, _ = s.parse_product("X1234", URLS["EC6A5HB90D"], flight)
    assert r.extra_specs["General > Energy efficiency class"] == "EU class C" and r.energy_star is None
    assert r.energy_kwh_year == 333.0


def test_url_validation_and_missing_markers():
    assert s.model_from_url(URLS["HM776GKB1"]) == "HM776GKB1"
    for bad in ("https://www.siemens-home.bsh-group.com/de/de/product/waeschepflege/waschmaschinen/frontlader/WG44B2040",
                "http://www.siemens-home.bsh-group.com/de/de/product/kochen-backen/x/y/HM776GKB1",
                "https://evil.example.com/de/de/product/kochen-backen/x/y/HM776GKB1",
                "https://www.siemens-home.bsh-group.com/de/de/manual/9001690529?productCode=KF96NAXEA",
                "https://www.siemens-home.bsh-group.com/de/de/product/kochen-backen/../x/HM776GKB1"):
        try:
            s.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    for fn in (lambda: t.flight_text("<html/>"), lambda: s.parse_product("M1", URLS["HM776GKB1"], '"x":1')):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_scrape_without_network():
    @contextmanager
    def fake_session(site, url):
        yield type("S", (), {"html": lambda self, u: _t("pdp_HM776GKB1.html")})()

    s0, d0 = s.session, s.download_docs
    s.session, s.download_docs = fake_session, lambda *a, **k: []
    try:
        rec, docs, raw = s.scrape(URLS["HM776GKB1"])
    finally:
        s.session, s.download_docs = s0, d0
    assert rec.model_number == "HM776GKB1" and rec.subcategory == "sco" and docs == [] and raw


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
