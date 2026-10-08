"""Unit conversion helpers and dual-unit formatting (internal standard: cu ft, in, lb, F).

Washer capacity (kg vs US drum cu ft) and energy figures (DOE vs EU/KS) are NOT convertible and are not handled here.
"""
import re
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


# ------------------------------------------------------------------ European number/price/energy-label parsing
# Used by the de/uk/fr adapters. EU energy label classes are a separate scale from ENERGY STAR: they are stored as the
# text 'EU class X' (never converted to or merged with the ENERGY STAR flag).
_NBSP = "\u00a0\u202f\u2009 "
_EU_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+\.\d+|-?\d[\d.\u00a0\u202f\u2009 ']*(?:,\d+)?|-?\d+(?:\.\d+)?")


def parse_eu_number(text) -> Optional[float]:
    """First number of European-formatted text, comma = decimal separator: '1.299,00 €', '1 299,00 €' (also NBSP/thin
    space), "1'299,00" -> 1299.0; '12,5 kg' -> 12.5; '1.299' (dot + exactly 3 digits, no comma) -> 1299.0 (German
    thousands dot); '1.5' -> 1.5; plain '1299' -> 1299.0. Also reads US-style '1,299.50' (a comma followed by exactly 3
    digits and a later dot). None for None/bool/text without a number. Ints/floats pass through."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    m = _EU_NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0).strip().rstrip(".' " + _NBSP)
    neg = raw.startswith("-")
    raw = raw.lstrip("-")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw) and "." in raw:  # US style 1,299.50
        val = float(raw.replace(",", ""))
    else:
        raw = re.sub(r"[\u00a0\u202f\u2009 ']", "", raw)
        if "," in raw:
            whole, _, frac = raw.rpartition(",")
            val = float(whole.replace(".", "") + "." + frac)
        elif raw.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", raw):
            val = float(raw.replace(".", ""))
        else:
            val = float(raw)
    return -val if neg else val


_EU_CLASS_CTX_RE = re.compile(
    r"(?:energy\s*(?:efficiency\s*)?(?:class|label|rating)|energieeffizienzklasse|energieeffizienz|energieklasse|"
    r"klasse|class(?:e(?:\s+[ée]nerg[ée]tique)?)?|energielabel)\W{0,4}([A-G])(\+{1,3})?(?![A-Za-z])", re.I)
_EU_CLASS_BARE_RE = re.compile(r"\s*([A-G])(\+{1,3})?\s*", re.I)


def eu_energy_class(text) -> Optional[str]:
    """EU energy label class as 'EU class A' .. 'EU class G' ('EU class A+++' for the pre-2021 scale), else None.
    Accepts 'E', 'Energieeffizienzklasse C', 'classe énergétique F', 'Energy class: D (scale A to G)'. A bare letter
    must be the whole string (so prose such as 'A good fridge' is not read as class A)."""
    if text is None or isinstance(text, bool):
        return None
    s = str(text).strip()
    m = _EU_CLASS_CTX_RE.search(s) or _EU_CLASS_BARE_RE.fullmatch(s)
    if not m or not m.group(1):
        return None
    letter = m.group(1).upper()
    plus = m.group(2) or ""
    return f"EU class {letter}{plus}"


_KWH_YEAR_RE = re.compile(
    r"(-?\d[\d.\u00a0\u202f\u2009 ']*(?:,\d+)?|\d+(?:\.\d+)?)\s*kWh\s*(?:/|pro|per|par)\s*"
    r"(?:year|yr|annum|a\b|jahr|an\b|ann[ée]e|ans\b|pa\b)", re.I)


def parse_kwh_per_year(text) -> Optional[float]:
    """Annual energy figure 'kWh/Jahr' | 'kWh/year' | 'kWh/an' | 'kWh per annum' | 'kWh/a' -> float kWh per year
    ('123 kWh/Jahr', '1.234,5 kWh/Jahr' -> 1234.5). None for other bases (kWh/100 cycles, kWh/1000 h, kWh/month)."""
    if text is None or isinstance(text, bool):
        return None
    m = _KWH_YEAR_RE.search(str(text))
    return parse_eu_number(m.group(1)) if m else None


# ------------------------------------------------------------------ Brazil (pt-BR)
# Prices: parse_eu_number already reads 'R$ 1.299,00', 'R$1.299', '1.299,90' (comma = decimal, dot = thousands).
# Procel/INMETRO labels (A..E) are their own scale: stored as the text 'BR class X', never merged with ENERGY STAR / EU.
_BR_CLASS_CTX_RE = re.compile(
    r"(?:classe|classifica[cç][aã]o|categoria|n[ií]vel|selo\s+procel|procel|etiqueta|efici[eê]ncia\s+energ[eé]tica)"
    r"(?:\s+de\s+efici[eê]ncia\s+energ[eé]tica|\s+energ[eé]tica)?\W{0,4}([A-E])(?![A-Za-z])", re.I)
_BR_CLASS_BARE_RE = re.compile(r"\s*([A-E])\s*", re.I)


def br_energy_class(text) -> Optional[str]:
    """Brazilian Procel/INMETRO energy class as 'BR class A' .. 'BR class E', else None. Accepts 'A', 'Selo Procel A',
    'Classe de eficiência energética: B', 'Classificação C'. A bare letter must be the whole string."""
    if text is None or isinstance(text, bool):
        return None
    s = str(text).strip()
    m = _BR_CLASS_CTX_RE.search(s) or _BR_CLASS_BARE_RE.fullmatch(s)
    return f"BR class {m.group(1).upper()}" if m else None


_KWH_MONTH_RE = re.compile(
    r"(-?\d[\d.    ']*(?:,\d+)?|\d+(?:\.\d+)?)\s*kWh\s*(?:/|por|per)\s*(?:m[eê]s|month|mo\b)", re.I)


def parse_kwh_per_month(text) -> Optional[float]:
    """Monthly energy figure 'kWh/mês' | 'kWh por mês' | 'kWh/month' -> float kWh per month ('42,5 kWh/mês' -> 42.5).
    Annual consumption is this x 12. None for other bases (kWh/ano, kWh/ciclo)."""
    if text is None or isinstance(text, bool):
        return None
    m = _KWH_MONTH_RE.search(str(text))
    return parse_eu_number(m.group(1)) if m else None
