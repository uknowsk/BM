"""Plain-assert tests. Run: python tests/test_units.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import units


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol * max(abs(b), 1)


def test_known_values():
    assert close(units.cuft_to_l(28), 792.87, 1e-3)
    assert close(units.l_to_cuft(793), 28.0, 1e-2)
    assert units.in_to_mm(36) == 914.4 and close(units.mm_to_in(914.4), 36)
    assert close(units.lb_to_kg(300), 136.08, 1e-3) and close(units.kg_to_lb(136.08), 300, 1e-3)
    assert units.f_to_c(32) == 0 and close(units.f_to_c(212), 100) and close(units.c_to_f(-18), -0.4)


def test_round_trip_under_tenth_percent():
    for x in (0.5, 1, 17.7, 28, 36, 300, 793):
        assert close(units.l_to_cuft(units.cuft_to_l(x)), x, 1e-3)
        assert close(units.mm_to_in(units.in_to_mm(x)), x, 1e-3)
        assert close(units.kg_to_lb(units.lb_to_kg(x)), x, 1e-3)
        assert close(units.f_to_c(units.c_to_f(x)), x, 1e-3)


def test_fmt_dual():
    assert units.fmt_dual(28, "capacity", "cuft") == "28.0 cu ft (793 L)"
    assert units.fmt_dual(793, "capacity", "L") == "793 L (28.0 cu ft)"
    assert units.fmt_dual(36, "length", "in") == "36 in (914 mm)"
    assert units.fmt_dual(35.75, "length", "in") == "35.8 in (908 mm)"
    assert units.fmt_dual(914, "length", "mm") == "914 mm (36 in)"
    assert units.fmt_dual(300, "weight", "lb") == "300 lb (136 kg)"
    assert units.fmt_dual(34, "temp", "F") == "34 F (1 C)"
    assert units.fmt_dual(None, "length", "in") == ""
    for bad in (("volume", "L"), ("length", "ft")):
        try:
            units.fmt_dual(1, *bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_parse_eu_number():
    n = units.parse_eu_number
    assert n("1.299,00 €") == 1299.0 and n("1 299,00 €") == 1299.0 and n("1 299,00 €") == 1299.0
    assert n("1 299,00 €") == 1299.0 and n("EUR 2.499,-") == 2499.0 and n("ab 899,00 €") == 899.0
    assert n("12,5 kg") == 12.5 and n("0,99") == 0.99 and n("1.234.567,89") == 1234567.89
    assert n("1.299") == 1299.0 and n("1.5") == 1.5 and n("1299") == 1299.0 and n("1,299.50") == 1299.5
    assert n("1'299,00") == 1299.0 and n(5) == 5.0 and n(2.5) == 2.5
    assert n("") is None and n("abc") is None and n(None) is None and n(True) is None


def test_eu_energy_class():
    c = units.eu_energy_class
    assert c("E") == "EU class E" and c(" b ") == "EU class B" and c("A+++") == "EU class A+++"
    assert c("Energieeffizienzklasse C") == "EU class C" and c("Klasse E") == "EU class E"
    assert c("classe énergétique F") == "EU class F" and c("Energy class: D (scale A to G)") == "EU class D"
    assert c("A good fridge") is None and c("H") is None and c("") is None and c(None) is None


def test_parse_kwh_per_year():
    k = units.parse_kwh_per_year
    assert k("123 kWh/Jahr") == 123.0 and k("1.234,5 kWh/Jahr") == 1234.5 and k("95 kWh/annum") == 95.0
    assert k("157 kWh/an") == 157.0 and k("55 kWh per year") == 55.0 and k("180 kWh/a") == 180.0
    assert k("60 kWh/100 cycles") is None and k("12 kWh/month") is None and k(None) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
