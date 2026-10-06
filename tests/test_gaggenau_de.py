"""Plain-assert offline tests for gaggenau_de. Run: python tests/test_gaggenau_de.py"""
import os
import sys
from pathlib import Path

os.environ["FRIDGE_I18N_LLM"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gaggenau_de as gd
import gaggenau_us as g
import thermador_us as t

FX = Path(__file__).parent / "fixtures" / "gaggenau_de"
URL = "https://www.gaggenau.com/de/de/mkt-product/backoefen/serie-400/backoefen/BO420102"


def _t(name):
    return (FX / name).read_text(encoding="utf-8")


class FakeSession:
    site = gd.SITE

    def text(self, url):
        assert url == "https://www.gaggenau.com/de/sitemap.xml"
        return _t("sitemap.xml")


def test_module_constants_and_supported():
    assert (gd.COUNTRY, gd.REGION, gd.CURRENCY) == ("de", "eu", "EUR")
    assert gd.SUPPORTED_SUBCATEGORIES == {"microwave", "sco", "gas_cooktop", "electric_oven", "induction", "radiant"}
    for sub in ("built_in", "washer", "otr", "gas_oven"):
        try:
            gd.discover(sub)
        except ValueError:
            continue
        raise AssertionError(sub)


def test_classify_german_paths():
    c = g.classify
    base = "/de/de/mkt-product/"
    assert c(base + "backoefen/serie-400/backoefen/BO420102") == "electric_oven"
    assert c(base + "backoefen/serie-200/dampfbackoefen/BS220100") == "electric_oven"
    assert c(base + "backoefen/serie-200/mikrowellen-backoefen/BM220100") == "sco"
    assert c(base + "backoefen/serie-200/mikrowellenoefen/BMP224100") == "microwave"
    assert c(base + "backoefen/serie-200/waermeschubladen/BW220100") is None
    assert c(base + "backoefen/serie-400/vakuumierschubladen/BV420100") is None
    assert c(base + "kochfelder/serie-400/serie-400/gas/CG492111") == "gas_cooktop"
    assert c(base + "kochfelder/serie-200/serie-200/induktion/CI261115") == "induction"
    assert c(base + "kochfelder/serie-200/serie-200/glaskeramik/CE261114") == "radiant"
    assert c(base + "kochfelder/serie-400/vario-serie-400/teppanyaki/VP414611") is None
    assert c(base + "kuehlschraenke/serie-200/serie-200/kuehl-gefrierkombinationen/RB280330") is None
    assert c(base + "BO420111") == "electric_oven"  # category-less redirect target, model prefix only


def test_sitemap_candidates():
    urls = g.sitemap_urls(FakeSession())
    assert len(urls) == 12 and not any("/us/en/" in u for u in urls)
    got = {s: [c.model_number for c in g.candidates_from_urls(urls, gd.SITE, s, "cooking", 30, "eu", "de", "EUR")]
           for s in gd.SUPPORTED_SUBCATEGORIES}
    assert got == {"electric_oven": ["BO420102", "BS220100", "BO420111"], "gas_cooktop": ["CG492111"],
                   "induction": ["CI261115"], "microwave": ["BMP224100"], "radiant": ["CE261114"], "sco": ["BM220100"]}
    c = g.candidates_from_urls(urls, gd.SITE, "radiant", "cooking", 5, "eu", "de", "EUR")[0]
    assert (c.country, c.region, c.currency, c.price_usd, c.price_local) == ("de", "eu", "EUR", None, None)


def test_oven_record_is_english_with_german_raw():
    flight = t.flight_text(_t("pdp_BO420102.html"))
    r, raw = g.build_record(gd.SITE, "BO420102", URL, flight, gd.facts_de,
                            dict(region="eu", country="de", currency="EUR"), gd._translate)
    assert (r.brand, r.category, r.subcategory, r.country, r.currency) == ("Gaggenau", "cooking", "electric_oven", "de", "EUR")
    assert r.price_local is None and r.width_in == 23.6 and r.wifi_supported is True
    assert r.extra_specs["Technical data > Energy efficiency class"] == "EU class A"
    assert r.extra_specs["Technical data > Cavity capacity (L)"] == "76" and r.extra_specs["Technical data > Connected load"] == "3.7 kW"
    assert "Additional information > Features (original)" in r.extra_specs and len(r.pod_features) == 6
    assert len(raw) == 57 and raw[0].value == "Grifflose Tür / automatische Türöffnung" and raw[0].key == "Feature 1"
    assert r.image_url.startswith("https://media3.bsh-group.com/")


def test_facts_de_parsing():
    upd, extra = gd.facts_de(["Energieverbrauch 207 kWh/Jahr.", "Energieeffizienzklasse E auf einer Skala der Effizienzklassen von A bis G.",
                              "Nutzinhalt gesamt: 272 Liter.", "Gesamtanschlusswert 0.090 kW.", "Gewicht ca. 20 kg"],
                             "Kühl- und Gefrierkombination 177.5 x 56 cm")
    assert upd["energy_kwh_year"] == 207.0 and upd["width_in"] == 69.9  # larger number of 'a x b cm' (compact ovens are W x H)
    assert upd["weight_lb"] == 44.1
    assert extra["Technical data > Energy efficiency class"] == "EU class E"
    assert extra["Technical data > Cavity capacity (L)"] == "272" and extra["Technical data > Connected load"] == "0.090 kW"


def test_url_validation():
    assert gd.model_from_url(URL) == "BO420102"
    for bad in ("https://www.gaggenau.com/us/en/mkt-product/cooktops/x/gas/CG1234",
                "https://www.gaggenau.de/de/de/mkt-product/backoefen/x/BO420102",
                "http://www.gaggenau.com/de/de/mkt-product/backoefen/x/BO420102"):
        try:
            gd.model_from_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
