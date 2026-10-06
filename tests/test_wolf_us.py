"""Plain-assert offline tests (saved GraphQL fixtures). Run: python tests/test_wolf_us.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _subzerowolf_common as sz
import wolf_us as w

FX = Path(__file__).parent / "fixtures" / "wolf_us"
GR304 = "https://www.subzero-wolf.com/products/30-gas-range-5610210-08c60f4488869d48e73493702cdddf52/5610210-08c60f4488869d48e73493702cdddf52"
SPO24 = "https://www.subzero-wolf.com/products/24-e-series-transitional-speed-oven-legacy-5610516/5610516"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


class FakeGraphQL:
    """Replaces sz.graphql: serves fixtures and records the variables it was asked for."""

    def __init__(self, *payloads):
        self.payloads, self.calls = list(payloads), []

    def __call__(self, query, variables):
        self.calls.append(variables)
        return self.payloads.pop(0)


def _with(fake, fn):
    orig = sz.graphql
    sz.graphql = fake
    try:
        return fn()
    finally:
        sz.graphql = orig


def test_supported_cooking_only():
    import catalog
    assert w.SUPPORTED_SUBCATEGORIES == {"gas_oven", "gas_cooktop", "electric_oven", "induction", "sco", "microwave"}
    assert w.SUPPORTED_SUBCATEGORIES <= set(catalog.sub_keys("cooking")) and "otr" not in w.SUPPORTED_SUBCATEGORIES
    assert w.COUNTRY == "us" and w.REGION == "na" and w.CURRENCY == "USD"
    try:
        w.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_classify_rules():
    c = w.classify
    assert c("Dual Fuel Range") == "gas_oven" and c("Gas Range") == "gas_oven" and c("Induction Range") == "induction"
    assert c("Sealed Burner Rangetop") == "gas_cooktop" and c("Gas Cooktop") == "gas_cooktop"
    assert c("Induction Cooktop") == "induction" and c("Speed Oven") == "sco"
    assert c("Single Oven") == "electric_oven" and c("Convection Steam Oven") == "electric_oven"
    assert c("Drop-Down Door Microwave Oven") == "microwave" and c("Standard Microwave Oven") == "microwave"
    assert c("Pro Wall Hood") is None and c("Coffee System") is None and c("Burner Module") is None and c("") is None


def test_discover_dedupes_models_and_filters_shells():
    fake = FakeGraphQL(_j("search_ovens.json"))
    got = _with(fake, lambda: w.discover("sco", limit=5))
    assert [c.model_number for c in got] == ["SPO24TE/S/TH", "SPO30CM/B/TH"]
    assert all(c.subcategory == "sco" and c.category == "cooking" and c.country == "us" and c.price_usd for c in got)
    assert got[0].url == SPO24 and got[0].attrs == {"width_in": 24.0, "fuel": "electric"}
    f = {d["attribute"]: d for d in fake.calls[0]["f"]}
    assert f["manufacturername"]["eq"] == "Wolf" and f["is_accessory"]["eq"] == "no" and f["mnseries"]["in"] == ["Speed Oven"]
    # shells without a page URL (CITF, MS) are dropped
    fake = FakeGraphQL(_j("search_ovens.json"))
    ind = _with(fake, lambda: w.discover("induction", limit=5))
    assert [c.model_number for c in ind] == ["CI243TF/S"]


def test_propane_variants_collapse_to_one_model():
    fake = FakeGraphQL(_j("search_page1.json"))
    got = _with(fake, lambda: w.discover("gas_oven", limit=5))
    assert [c.model_number for c in got] == ["GR304"]
    assert got[0].price_usd == 6655.0 and got[0].attrs["burners"] == 4 and got[0].attrs["fuel"] == "gas"


def test_scrape_parses_full_spec_table():
    rec, docs, raw = _with(FakeGraphQL(_j("product_GR304.json")), lambda: w.scrape(GR304))
    assert docs == [] and rec.brand == "Wolf" and rec.model_number == "GR304" and rec.subcategory == "gas_oven"
    assert rec.price_usd == 6655.0 and rec.width_in == 29.875 and rec.height_in == 37.0 and rec.depth_in == 28.375
    assert rec.weight_lb == 413.0 and rec.voltage_v == "110/120" and rec.frequency_hz == 60.0 and rec.capacity_total_cuft == 4.4
    assert rec.extra_specs["Specifications > Gas Inlet"] == '1/2" NPT' and rec.extra_specs["Series"] == "Gas Range"
    assert all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw) and len(raw) >= 20
    assert not any(k.endswith("Combined SKU") or k.endswith("Parent SKU") for k in rec.extra_specs)
    assert rec.pod_features and rec.image_url.startswith("https://delivery-p28264-e87620.adobeaemcloud.com/adobe/assets/")
    rec2, _d, _r = _with(FakeGraphQL(_j("product_SPO24.json")), lambda: w.scrape(SPO24))
    assert rec2.subcategory == "sco" and rec2.price_usd == 2885.0 and rec2.model_number == "SPO24TE/S/TH"


def test_scrape_rejects_bad_urls_and_other_brands():
    for bad in ("http://www.subzero-wolf.com/products/a-b/5610210-abc", "https://evil.example.com/products/a-b/5610210-abc",
                "https://www.subzero-wolf.com/products/a-b", "https://www.subzero-wolf.com/products/A_B/5610210-abc"):
        try:
            w.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    sub_zero = _j("product_GR304.json")
    for a in sub_zero["products"][0]["attributes"]:
        if a["name"] == "manufacturername":
            a["value"] = "Sub-Zero"
    try:
        _with(FakeGraphQL(sub_zero), lambda: w.scrape(GR304))
        raise AssertionError("brand mismatch must fail")
    except ValueError:
        pass


def test_sku_url_roundtrip_and_config():
    assert sz.sku_from_url(GR304) == "5610210__08c60f4488869d48e73493702cdddf52" and sz.sku_from_url(SPO24) == "5610516"
    pv = _j("product_GR304.json")["products"][0]
    assert sz.product_url(pv) == GR304
    sz._config_cache.clear()
    orig = sz.fetch_json
    sz.fetch_json = lambda method, url, headers=None, body=None: _j("config.json")
    try:
        endpoint, hdr = sz.storefront_config()
    finally:
        sz.fetch_json = orig
        sz._config_cache.clear()
    assert endpoint == "https://www.subzero-wolf.com/szg-api/cs-graphql" and hdr["x-api-key"] == "TEST-KEY"
    old = os.environ.get("FRIDGE_BROWSER_MODE")
    try:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        assert sz._strategies() == ["requests", "headless", "visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "visible"
        assert sz._strategies() == ["visible"]
        os.environ["FRIDGE_BROWSER_MODE"] = "headless"
        assert sz._strategies() == ["headless"]
    finally:
        os.environ.pop("FRIDGE_BROWSER_MODE", None)
        if old is not None:
            os.environ["FRIDGE_BROWSER_MODE"] = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} passed")
