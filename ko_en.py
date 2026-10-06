"""Korean -> English helpers for the kr adapters (Samsung KR / LG KR).

Public API (all pure/deterministic unless an unknown string reaches the local LLM):
  translate_key(label_ko)           spec LABEL -> English (glossary, then cached local LLM, else the original text)
  translate_value(text_ko)          spec VALUE -> English (digits/units untouched)
  translate_many(texts, kind)       batched variant (one LLM call per <=40 unknown strings)
  parse_kr_quantity(text)           {'value', 'unit'[, 'max']} for L/mm/kg/W/kWh/month|year/V/rpm/dB/C, or None
  parse_dimensions(text)            (a, b, c) numbers from '595 x 1,850 x 688 mm' (order as written), or None
  convert_to_schema(label_en, v, u) Quantity(value, unit, si_value, si_unit): schema unit (cu ft/in/lb/F) + original
  kr_energy_grade(text)             'KR grade 1'..'KR grade 5' or None (never converted to ENERGY STAR)
  annualize(kwh_month)              monthly kWh x 12 (an estimate; see ANNUALIZED_LABEL)
  parse_krw(text)                   KRW price as int ('199만원' -> 1990000) or None for non prices

The glossary -> cache -> LLM machinery lives in i18n.Translator (shared with the de/fr languages); this module keeps
the Korean-specific rules and parsers and exposes the old public API as a compatibility wrapper (i18n.get('ko')).

Cost/safety: glossary first, then in-memory/disk cache (sha1 keys, atomic writes), then ONE batched LLM call
(llm.chat_json, qwen3-8b, temperature 0.1). Scraped text is untrusted: it is sent as JSON data and replies are
only accepted for the strings asked, as short Hangul-free single-line strings. If the LLM is unreachable the
original Korean is returned unchanged (never invented) and nothing is cached."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import i18n
import units

logger = logging.getLogger("ko_en")

_HERE = Path(__file__).resolve().parent
GLOSSARY_PATH = _HERE / "data" / "ko_glossary.json"
CACHE_PATH = _HERE / "data" / "ko_cache.json"
LLM_BATCH = i18n.LLM_BATCH
LLM_MAX_CHARS = i18n.LLM_MAX_CHARS

_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")


def _has_hangul(s: str) -> bool:
    return bool(_HANGUL.search(s))


_clean = i18n.clean  # unescape (twice), drop tags, NFKC, collapse whitespace


def _norm(s: str) -> str:
    """Glossary key: cleaned, whitespace removed, lower-cased, trailing ':' dropped."""
    return _clean(s).replace(" ", "").lower().rstrip(":")


# ------------------------------------------------------------------ glossary
def _load_glossary() -> tuple[dict[str, str], dict[str, str]]:
    raw = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    return ({_norm(k): v for k, v in raw["labels"].items()},
            {_norm(k): v for k, v in raw["values"].items()})


_LABELS, _VALUES = _load_glossary()

# Korean unit words inside label parentheses / after '/' in values.
_UNIT_WORDS = (("개월", "months"), ("인용", "place settings"), ("월", "month"), ("년", "year"),
               ("회", "cycle"), ("일", "day"), ("분", "min"), ("시간", "h"), ("개", "pcs"))
_COUNTERS = {"개": "pcs", "인용": "place settings", "단계": "levels", "단": "tiers", "구": "burners",
             "분": "min", "시간": "h", "회": "cycles", "개월": "months", "년": "years", "대": "units"}
_COUNTER_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?)\s*(" + "|".join(sorted(_COUNTERS, key=len, reverse=True)) + r")$")
_DATE_RE = re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월$")
_GRADE_VAL_RE = re.compile(r"^([1-5])\s*등급$")
_PARTS_RE = re.compile(r"(\s*[/,()+]\s*)")
_PAREN_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _paren_en(inner: str) -> Optional[str]:
    """Translate the inside of a label's trailing parentheses, comma-separated parts independently; ASCII parts and
    unit words are handled without the glossary. None if any part is unresolvable."""
    parts = []
    for part in inner.split(","):
        out = part
        for ko, en in _UNIT_WORDS:
            out = out.replace(ko, en)
        if _has_hangul(out):
            out = _LABELS.get(_norm(part)) or _VALUES.get(_norm(part))
            if out is None:
                return None
        parts.append(out.strip())
    return ", ".join(parts)


def _label_local(label: str) -> Optional[str]:
    s = _clean(label)
    if not s:
        return ""
    hit = _LABELS.get(_norm(s))
    if hit:
        return hit
    m = _PAREN_RE.match(s)
    if m and m.group(1):
        base = _LABELS.get(_norm(m.group(1)))
        inner = _paren_en(m.group(2))
        if base and inner:
            return f"{base} ({inner})"
    if not _has_hangul(s):
        return s
    return None


def _piece_local(piece: str) -> Optional[str]:
    p = piece.strip()
    if not p:
        return ""
    hit = _VALUES.get(_norm(p)) or _LABELS.get(_norm(p))
    if hit:
        return hit
    m = _GRADE_VAL_RE.match(p)
    if m:
        return f"KR grade {m.group(1)}"
    m = _DATE_RE.match(p)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = _COUNTER_RE.match(p)
    if m:
        return f"{m.group(1)} {_COUNTERS[m.group(2)]}"
    return p if not _has_hangul(p) else None


def _value_local(text: str) -> Optional[str]:
    """Glossary/regex translation of a spec value; None when the local LLM is needed."""
    s = _clean(text)
    if not s:
        return ""
    hit = _VALUES.get(_norm(s)) or _LABELS.get(_norm(s))
    if hit:
        return hit
    if not _has_hangul(s):
        return str(text).strip()  # numbers, units, model codes: untouched
    out: list[str] = []
    for i, part in enumerate(_PARTS_RE.split(s)):
        if i % 2:  # separator, kept verbatim
            out.append(part)
            continue
        en = _piece_local(part)
        if en is None:
            return None
        out.append(part[: len(part) - len(part.lstrip())] + en + part[len(part.rstrip()):])
    res = "".join(out)
    res = re.sub(r"/(?:회|월|년|인용)\b", lambda m: "/" + dict(_UNIT_WORDS)[m.group(0)[1:]], res)
    return res


# ------------------------------------------------------------------ cache + LLM (shared machinery: i18n.Translator)
_cache_mem: dict[str, str] = {}  # in-memory cache of the 'ko' translator (tests clear/inspect it)


def _default_chat(prompt: str):
    import llm  # local import: keeps ko_en importable (and testable) without the HTTP client
    return llm.chat_json(prompt)


_chat = _default_chat  # tests replace this with a fake

# The glossary, rules above, CACHE_PATH and _chat are read at call time, so reassigning them (tests) keeps working.
TRANSLATOR = i18n.Translator(
    "ko", glossary_path=lambda: GLOSSARY_PATH, cache_path=lambda: CACHE_PATH, chat=lambda prompt: _chat(prompt),
    cache_mem=_cache_mem, label_local=_label_local, value_local=_value_local, untranslated=_has_hangul)
TRANSLATOR.llm_enabled = lambda: True  # ko_en has always asked the LLM; i18n's FRIDGE_I18N_LLM switch is for de/fr


def translate_many(texts: list[str], kind: str = "value") -> list[str]:
    """Translate many specification strings (kind 'value' or 'label') with at most one LLM call per 40 unknowns.
    Order/length preserved; untranslatable strings come back unchanged."""
    return TRANSLATOR.translate_many(texts, kind)


def translate_key(label_ko: str) -> str:
    """Spec label -> English (glossary, cached LLM, else the unchanged Korean)."""
    return TRANSLATOR.translate_key(label_ko)


def translate_value(text_ko: str) -> str:
    """Spec value -> English; numbers/units untouched."""
    return TRANSLATOR.translate_value(text_ko)


# ------------------------------------------------------------------ quantities
@dataclass(frozen=True)
class Quantity:
    """value/unit are in the schema unit (cu ft, in, lb, F, or unchanged); si_value/si_unit keep the original."""
    value: float
    unit: str
    si_value: float
    si_unit: str


_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNIT_MAP = (  # (regex, canonical unit, multiplier); longest/most specific first
    (r"kwh\s*/\s*(?:월|month|mo)", "kWh/month", 1), (r"kwh\s*/\s*(?:년|yr|year)", "kWh/year", 1),
    (r"kw", "W", 1000), (r"kg|킬로그램", "kg", 1), (r"mm|밀리미터", "mm", 1), (r"rpm", "rpm", 1),
    (r"db(?:\s*\(a\))?", "dB", 1), (r"°\s*c|℃|도씨", "C", 1), (r"w", "W", 1), (r"v", "V", 1),
    (r"l|리터", "L", 1),
)
_QTY_RE = re.compile(
    rf"(?<![\w.])(?P<v>{_NUM})(?:\s*[~∼\-–]\s*(?P<v2>{_NUM}))?\s*"
    r"(?P<u>" + "|".join(u for u, _, _ in _UNIT_MAP) + r")(?![A-Za-z])", re.I)


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def parse_kr_quantity(text) -> Optional[dict]:
    """First 'number unit' in Korean spec text: {'value': 636.0, 'unit': 'L'} (+ 'max' for '40~45dB').
    Handles commas, '약', '리터', '㎏', '℃', kW->W, kWh/월 -> kWh/month. None if there is no number with a known unit."""
    if text is None:
        return None
    s = _clean(text).replace("약", " ")
    for m in _QTY_RE.finditer(s):
        u = m.group("u").lower().replace(" ", "")
        for rx, canon, mult in _UNIT_MAP:
            if re.fullmatch(rx.replace(r"\s*", ""), u, re.I):
                out = {"value": _to_float(m.group("v")) * mult, "unit": canon}
                if m.group("v2"):
                    out["max"] = _to_float(m.group("v2")) * mult
                return out
    return None


_DIM_RE = re.compile(rf"({_NUM})\s*[x×*]\s*(?:[가-힣]+\s*)?({_NUM})\s*[x×*]\s*(?:[가-힣]+\s*)?({_NUM})", re.I)


def parse_dimensions(text) -> Optional[tuple[float, float, float]]:
    """Three numbers separated by x/×/*: '595 x 1,850 x 688 mm' -> (595.0, 1850.0, 688.0), in the order written
    (Samsung says W x H x D, LG says WxHxD; check the label). The first triple wins."""
    if not text:
        return None
    m = _DIM_RE.search(_clean(text))
    return tuple(_to_float(g) for g in m.groups()) if m else None


def convert_to_schema(label_en: str, value: float, unit: str) -> Quantity:
    """Convert a metric value to the schema's unit while keeping the original.
    L -> cu ft only for capacities; mm -> in; kg -> lb except capacities (washer/dryer kg is not a volume);
    C -> F; W/V/rpm/dB/kWh unchanged."""
    label = (label_en or "").lower()
    capacity = "capacity" in label or "volume" in label
    if unit == "L" and capacity:
        return Quantity(units.l_to_cuft(value), "cu ft", value, "L")
    if unit == "mm":
        return Quantity(units.mm_to_in(value), "in", value, "mm")
    if unit == "kg" and not capacity:
        return Quantity(units.kg_to_lb(value), "lb", value, "kg")
    if unit == "C":
        return Quantity(units.c_to_f(value), "F", value, "C")
    return Quantity(value, unit, value, unit)


# ------------------------------------------------------------------ energy, price
ANNUALIZED_LABEL = "Annual energy consumption (kWh/yr, estimated = monthly x 12)"
_GRADE_RE = re.compile(r"(?<![\d.])([1-5])\s*등급")


def kr_energy_grade(text) -> Optional[str]:
    """Korean energy efficiency grade 1 (best) .. 5 as text 'KR grade N'. Not comparable to ENERGY STAR; no conversion."""
    s = _clean(text) if text is not None else ""
    m = _GRADE_RE.search(s) or re.fullmatch(r"\s*([1-5])\s*", s)
    return f"KR grade {m.group(1)}" if m else None


def annualize(kwh_month: float) -> float:
    """Monthly kWh (월간 소비전력량) x 12 -> estimated kWh/yr. Label it with ANNUALIZED_LABEL, never as measured."""
    return round(kwh_month * 12, 2)


_KRW_UNITS = {"억": 10**8, "만": 10**4, "천": 10**3, "백": 100}
_KRW_TOKEN = r"(?:\d[\d,]*\s*[억만천백]?|[억만천백])"
_KRW_RE = re.compile(rf"(?<![\d,\-−])(?P<amt>{_KRW_TOKEN}(?:\s*{_KRW_TOKEN})*)\s*원|[₩￦]\s*(?P<sym>\d[\d,]*)")


def parse_krw(text) -> Optional[int]:
    """KRW amount as int: '1,990,000원', '199만원', '199만 9천원', '1억 2천만원', '₩1,990,000', 1990000.
    None for non-prices ('최저가', '가격문의', '품절'), zero/negative amounts and bools."""
    if isinstance(text, bool) or text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text) if text > 0 else None
    s = _clean(text)
    if re.fullmatch(r"\d[\d,]*", s):
        n = int(s.replace(",", ""))
        return n or None
    m = _KRW_RE.search(s)
    if not m:
        return None
    if m.group("sym"):
        n = int(m.group("sym").replace(",", ""))
        return n or None
    total = cur = 0
    for tok in re.finditer(r"(\d[\d,]*)?\s*([억만천백])?", m.group("amt")):
        num, unit = tok.group(1), tok.group(2)
        if num is None and unit is None:
            continue
        n = int(num.replace(",", "")) if num else 0
        if unit in ("천", "백"):
            cur += (n or 1) * _KRW_UNITS[unit]
        elif unit in ("만", "억"):
            total += (cur + n or 1) * _KRW_UNITS[unit]
            cur = 0
        else:
            cur += n
    total += cur
    return total or None
