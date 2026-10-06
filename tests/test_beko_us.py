"""Offline tests for beko_us (trimmed saved beko.com/us-en HTML; no network). Run: python tests/test_beko_us.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import beko_us as b
import catalog

FIX = Path(__file__).parent / "fixtures" / "beko_us"
read = lambda n: (FIX / n).read_text(encoding="utf-8")
URL = "https://www.beko.com/us-en/x-product-page"


def classes(fx: str) -> dict[str, str | None]:
    return {c["model"]: b.classify(c["name"], c["cooktop_type"], c["burners"]) for c in b.parse_cards(read(fx))}


def test_contract():
    assert (b.COUNTRY, b.REGION, b.CURRENCY) == ("us", "na", "USD")
    assert b.SUPPORTED_SUBCATEGORIES == {"microwave", "otr", "gas_oven", "gas_cooktop", "electric_oven", "induction", "radiant"}
    assert b.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking"))
    assert catalog.module_name("Beko", "us") == "beko_us"
    try:
        b.discover("french_door")
    except ValueError:
        pass
    else:
        raise AssertionError("fridge keys must raise ValueError")


def test_classify_table():
    c = b.classify
    assert c("1.6 cu ft Over the Range Microwave Oven") == "otr" and c("Built-in Microwave 1000 W 62 L") == "microwave"
    assert c('30" Stainless Steel Wall Oven') == "electric_oven" and c('30" Double Wall Oven') == "electric_oven"
    assert c('24" Built-In Gas Cooktop with 4 Burners') == "gas_cooktop"
    assert c('30" Built-In Electric Cooktop', "Vitroceramic", "4 Vitroceramic Zones") == "radiant"
    assert c('36" Built-In Induction Cooktop', "Induction") == "induction"
    assert c('30" Slide-In Range', "", "4 Gas Burners") == "gas_oven"
    assert c('30" Slide-InRange', "Vitroceramic", "5 Vitroceramic Zones") == "radiant"   # site typo 'Slide-InRange'
    assert c('30" Pro-Style Induction Range') == "induction" and c('30" Pro-Style Dual Fuel Range', "Gas") == "gas_oven"
    assert c('24" Front-Load Washer') is None and c("Range hood accessory") is None


def test_listings():
    r = classes("list_ranges.html")
    assert r["SLGR30423SS"] == "gas_oven" and r["SLER24410SS"] == "radiant" and r["SLER30524SS"] == "radiant"
    assert r["PRIR34452SS"] == "induction" and r["SLIR24410SS"] == "induction" and None not in r.values()
    assert classes("list_microwaves.html")["MWOTR30100SS"] == "otr" and classes("list_microwaves.html")["MWDR24100SS"] == "microwave"
    assert set(classes("list_gas_cooktops.html").values()) == {"gas_cooktop"}
    assert set(classes("list_induction_cooktops.html").values()) == {"induction"}
    assert set(classes("list_electric_cooktops.html").values()) == {"radiant"}
    assert set(classes("list_wall_ovens.html").values()) == {"electric_oven"}
    cards = b.parse_cards(read("list_gas_cooktops.html"))
    cand = b.card_to_candidate(cards[0], "gas_cooktop")
    assert cand.model_number == "BCTG24400SS" and cand.price_usd is None and cand.attrs["fuel"] == "gas" and cand.attrs["width_in"] == 24.0
    assert cand.url == "https://www.beko.com/us-en/24-built-in-gas-cooktop-with-4-burners-p-bctg24400ss"
    assert b.card_to_candidate(cards[0], "induction") is None
    assert (cand.category, cand.subcategory, cand.country, cand.currency) == ("cooking", "gas_cooktop", "us", "USD")


def test_product_range():
    rec, raw = b.parse_product(read("pdp_range.html"), URL)
    assert rec.model_number == "SLGR30423SS" and rec.subcategory == "gas_oven" and rec.price_usd is None
    assert (rec.width_in, rec.height_in, rec.depth_in) == (29.8, 35.98, 28.74) and rec.weight_lb == 275.6
    assert rec.voltage_v == "110 - 120" and rec.frequency_hz == 60.0
    assert rec.extra_specs["Cooktop Features > Burner Configuration"] == "4 Gas Burners"
    assert rec.extra_specs["Cooktop Features > Front-left Zone"] == "12500 BTU"
    assert rec.extra_specs["Cleaning > Pyrolytic Self Cleaning"] == "Yes"        # feature rows without a value
    assert rec.image_url.startswith("https://www.beko.com/content/dam/")
    assert rec.extra_specs["Documents > Manual"].startswith("https://www.beko.com/content/dam/")
    assert any(r.section == "Dimensions & Weight" and r.key == "Width" and r.value == "75.7 cm" for r in raw)


def test_product_cooktop_and_otr():
    rec, _ = b.parse_product(read("pdp_gas_cooktop.html"), URL)
    assert rec.subcategory == "gas_cooktop" and rec.width_in == 22.83 and rec.weight_lb == 25.4
    otr, _ = b.parse_product(read("pdp_otr.html"), URL)
    assert otr.subcategory == "otr" and otr.capacity_total_cuft == 1.6 and otr.voltage_v == "120"


def test_security():
    for bad in ("http://www.beko.com/us-en/some-product-p-abc", "https://evil.com/us-en/some-product-p-abc",
                "https://www.beko.com/uk-en/some-product-p-abc", "https://www.beko.com/us-en/"):
        try:
            b._check_product_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert b.doc_links('<a href="https://evil.com/a.pdf">x</a><a href="/content/dam/x/User-Manual.pdf">y</a>') == \
        {"Manual": "https://www.beko.com/content/dam/x/User-Manual.pdf"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
