"""Unit conversion helpers and dual-unit formatting (internal standard: cu ft, in, lb, F).

Washer capacity (kg vs US drum cu ft) and energy figures (DOE vs EU/KS) are NOT convertible and are not handled here.
"""
from typing import Optional

L_PER_CUFT = 28.3168
MM_PER_IN = 25.4
KG_PER_LB = 0.45359237


def cuft_to_l(x: float) -> float:
    return x * L_PER_CUFT


def l_to_cuft(x: float) -> float:
    return x / L_PER_CUFT


def in_to_mm(x: float) -> float:
    return x * MM_PER_IN


def mm_to_in(x: float) -> float:
    return x / MM_PER_IN


def lb_to_kg(x: float) -> float:
    return x * KG_PER_LB


def kg_to_lb(x: float) -> float:
    return x / KG_PER_LB


def f_to_c(x: float) -> float:
    return (x - 32) * 5 / 9


def c_to_f(x: float) -> float:
    return x * 9 / 5 + 32


def _trim(x: float, nd: int) -> str:
    s = f"{x:.{nd}f}"
    return s.rstrip("0").rstrip(".") if nd else s


# kind -> unit -> (label, decimals, converter to the other unit, other unit)
_KINDS = {
    "capacity": {"cuft": ("cu ft", 1, cuft_to_l, "L", False), "L": ("L", 0, l_to_cuft, "cuft", False)},
    "length": {"in": ("in", 1, in_to_mm, "mm", True), "mm": ("mm", 0, mm_to_in, "in", True)},
    "weight": {"lb": ("lb", 0, lb_to_kg, "kg", True), "kg": ("kg", 0, kg_to_lb, "lb", True)},
    "temp": {"F": ("F", 0, f_to_c, "C", True), "C": ("C", 0, c_to_f, "F", True)},
}


def fmt_dual(value: Optional[float], kind: str, primary: str) -> str:
    """'28.0 cu ft (793 L)' / '36 in (914 mm)'; `value` is in the `primary` unit. '' for None."""
    units = _KINDS.get(kind)
    if units is None or primary not in units:
        raise ValueError(f"unsupported kind/unit {kind!r}/{primary!r}")
    if value is None:
        return ""
    label, nd, conv, other, trim = units[primary]
    olabel, ond, _, _, otrim = units[other]
    a = _trim(value, nd) if trim else f"{value:.{nd}f}"
    converted = conv(value)
    b = _trim(converted, ond) if otrim else f"{converted:.{ond}f}"
    return f"{a} {label} ({b} {olabel})"
