"""Plain-assert offline tests (saved fixtures). Run: python tests/test_electrolux_us.py
Electrolux US shares the OCC platform with Frigidaire (see test_frigidaire_us.py for the shared rules)."""
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _electrolux_common as ec
import catalog
import electrolux_us as e

FX = Path(__file__).parent / "fixtures" / "electrolux_us"
PDP = "https://www.electrolux.com/en/p/"


def _j(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


class FakeSession:
    def __init__(self, data):
        self.data, self.calls = data, []

    def json(self, url):
        self.calls.append(url)
        for key, val in self.data.items():
            if key in url:
                return val
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


def test_supported_subcategories():
    cooking = set(catalog.CATEGORY_TREE["cooking"]["children"])
    assert e.SUPPORTED_SUBCATEGORIES == cooking - {"radiant"}  # Electrolux US sells no electric ranges/cooktops
    assert (e.BRAND, e.COUNTRY, e.REGION, e.CURRENCY) == ("Electrolux", "us", "na", "USD")
    try:
        e.discover("radiant")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        e.discover("french_door")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_discover_gas_rangetops():
    sess = FakeSession({"Cooktops_Gas": _j("search_gas_cooktops.json")})
    with fake_session(sess):
        got = e.discover("gas_cooktop", 5)
    assert [c.model_number for c in got] == ["ECCG3672AS", "ECCG3668AS"]
    c = got[0]
    assert c.brand == "Electrolux" and c.subcategory == "gas_cooktop" and c.price_usd == 3049.0
    assert c.url == PDP + "kitchen/cooktops/gas-cooktops/ECCG3672AS" and c.attrs["fuel"] == "gas"
    assert "occ/v2/electrolux/products/search" in sess.calls[0]


def test_parse_gas_range_and_combination_oven():
    rec, raw = e.parse_product("ECFG3668AS", PDP + "x/ECFG3668AS", _j("product_ECFG3668AS.json"))
    assert rec.brand == "Electrolux" and rec.subcategory == "gas_oven" and rec.price_usd == 4899.0
    assert rec.width_in == 35.938 and rec.weight_lb == 245.0 and rec.voltage_v == "120"
    assert rec.extra_specs["Cooktop > Total Number of Burners"] == "6"
    assert len(raw) >= 40 and all(f"{r.section} > {r.key}" in rec.extra_specs for r in raw)
    assert rec.image_url.startswith("https://frigidaire.bynder.com/")  # the Electrolux US site serves the same CDN
    rec, _ = e.parse_product("ECWM3012AS", PDP + "x/ECWM3012AS", _j("product_ECWM3012AS.json"))
    assert rec.subcategory == "sco"


def test_scrape_rejects_other_hosts():
    for bad in ("https://www.frigidaire.com/en/p/ECFG3668AS", "http://www.electrolux.com/en/p/ECFG3668AS",
                "https://www.electrolux.com/en/kitchen/ranges"):
        try:
            e.scrape(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    for n, fn in tests:
        fn()
        print("ok", n)
    print(f"{len(tests)} passed")
