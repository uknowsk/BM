"""Semantic canonicalization of spec labels and list items across products, brands and languages.

canonicalize(label, *, category, value=None, section=None) -> Canon  maps 'Size' / 'Dimensions' / 'Overall dimensions' /
'크기' to ONE canonical attribute (id, EN/KO label, section, kind, core). canonicalize_item() does the same for list
items (cooking modes, features) in their own item space. Resolution pipeline (first hit wins):

  0 override   data/canon_overrides.json  ({'merge': [...], 'split': [...]})
  1 normalize  lowercase, strip (R)(TM) / unit parentheses, 'Section > Label' -> leaf (section kept as a hint), stems,
               cavity (upper/lower/second) and burner-position qualifiers become an id suffix, e.g. 'bake-wattage:upper'
  2 seed       data/canon_seed.json: ~235 canonical attributes with EN/KO synonym lists, section and core flag
  3 exact      registry hit (data/canon_registry.json) - decisions of earlier runs stay stable
  4 embed      cosine on LM Studio embeddings (llm.embed, vectors cached on disk) >= EMBED_AUTO merges into the nearest
               compatible attribute; EMBED_ASK..EMBED_AUTO asks the local LLM (verdict cached); below -> new attribute
  5 fallback   no embeddings reachable: token-set + difflib ratio with a stricter threshold (never blocks); method 'fallback'
  6 new        a new canonical id (kebab of the label) is added to the registry, long-tail (core=False)

run_stats() reports, per build (begin() resets it), how many distinct labels each stage resolved and whether the embedding
stage was used / unavailable / offline / not needed, so a silent degrade to the deterministic fallback is visible.

Merges are guarded: numeric/flag/text value kinds and unit families must be compatible and a label pair that differs
by a discriminating word (width/height, fridge/freezer, bake/broil, net/gross, lock/alarm ...) never auto-merges.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent
SECTIONS = ("기본정보", "치수·무게", "용량", "전기·에너지", "성능", "기능", "디자인", "연결성", "조리(오븐·쿡탑)", "세탁·건조", "냉각·신선", "보증·기타")
CATS = ("refrigerator", "washer", "cooking")
EMBED_AUTO, EMBED_ASK = 0.84, 0.72   # cosine thresholds (auto-merge / ask the LLM)
FALLBACK_MIN = 0.88                  # deterministic token+difflib score needed when embeddings are unavailable
REVIEW_BELOW = 0.9                   # a merge scored below this (or decided by the LLM) is flagged for review
LLM_BUDGET = 40                      # max LLM verdicts / classifications per build (cost-aware)
METHODS = ("override", "seed", "exact", "embed", "llm", "fallback", "new")
# per-run tally keys (Canonicalizer.run_stats): every distinct label is counted once, under how it was resolved.
# 'registry' = a decision of an earlier run reused, 'rule' = routed by a deterministic rule outside this module (accessory items)
STAT_KEYS = ("override", "seed", "exact", "registry", "embed", "llm", "fallback", "new", "rule")


def needs_review(method: str, score: float) -> bool:
    """A mapping the user should double-check: decided by the LLM, or a fuzzy (embedding / fallback) merge below REVIEW_BELOW."""
    return method == "llm" or (method in ("embed", "fallback") and score < REVIEW_BELOW)


def stats_line(stats: dict) -> str:
    """One-line Korean summary of Canonicalizer.run_stats() for the Mapping sheet header and the compare payload."""
    stage, model = stats.get("embed_stage"), stats.get("embed_model") or ""
    head = {"used": f"임베딩 단계: 사용됨{f' ({model})' if model else ''}",
            "unavailable": "임베딩 단계: 사용 불가 - 결정적 폴백",
            "offline": "임베딩 단계: 사용 불가 (오프라인 모드) - 결정적 폴백",
            "not_needed": "임베딩 단계: 호출 불필요 (모든 항목이 시드/레지스트리에서 해결)"}.get(stage, "임베딩 단계: 알 수 없음")
    by = ", ".join(f"{k} {v}" for k, v in (stats.get("methods") or {}).items() if v)
    return f"{head} | 항목 {stats.get('labels', 0)}개 해결 방법: {by or '-'}"


@dataclass(frozen=True)
class Canon:
    id: str
    label_en: str
    label_ko: str
    section: str
    kind: str          # 'numeric' | 'text' | 'flag' | 'list-item'
    core: bool
    method: str        # one of METHODS
    score: float
    unit: str = ""
    composite: tuple = ()      # ids of the parts a composite value (W x H x D) is split into
    asitems: bool = False      # the value is a list: every item becomes its own row
    order: int = 10_000        # position in the seed (stable row order)
    alt: tuple = ()            # ((unit family, id), ...) used when the value's unit family differs from `unit`
    cav: bool = False          # an upper/lower/second-cavity qualifier applies (values like '1700W Upper / 2200W Lower' split)
    also: tuple = ()           # a truthy value of this flag also counts for these other ids ('Steam & Self Clean')

    def label(self, lang: str = "en") -> str:
        return (self.label_ko or self.label_en) if lang == "ko" else self.label_en

    @property
    def needs_review(self) -> bool:
        return needs_review(self.method, self.score)


# ------------------------------------------------------------------------------------------ value helpers
_FRAC = re.compile(r"^(?:(\d+)[\s-]+)?(\d+)/(\d+)$")
_NUM = r"\d[\d,]*(?:\.\d+)?(?:[\s-]+\d+/\d+)?|\d+/\d+|\.\d+"
_MEASURE = re.compile(rf"^\s*(?P<n>{_NUM})\s*(?P<u>[^\d\s].*?)?\s*$", re.I)

# unit token -> (family, factor to the family's base unit). Bases: in, lb, cu ft, W, kWh/yr, BTU ...
_UNITS = {
    "in": ("length", 1), "inch": ("length", 1), "inches": ("length", 1), '"': ("length", 1), "''": ("length", 1), "in.": ("length", 1),
    "ft": ("length", 12), "feet": ("length", 12), "foot": ("length", 12), "'": ("length", 12), "mm": ("length", 1 / 25.4),
    "cm": ("length", 1 / 2.54), "m": ("length", 39.3701),
    "lb": ("mass", 1), "lbs": ("mass", 1), "lbs.": ("mass", 1), "lb.": ("mass", 1), "pound": ("mass", 1), "pounds": ("mass", 1),
    "kg": ("mass", 2.20462), "g": ("mass", 0.00220462),
    "cu ft": ("volume", 1), "cu. ft.": ("volume", 1), "cu.ft": ("volume", 1), "cu.ft.": ("volume", 1), "cuft": ("volume", 1),
    "cubic feet": ("volume", 1), "cubic foot": ("volume", 1), "cu ft.": ("volume", 1), "cu. ft": ("volume", 1),
    "l": ("volume", 0.0353147), "liter": ("volume", 0.0353147), "liters": ("volume", 0.0353147), "litre": ("volume", 0.0353147),
    "w": ("power", 1), "watt": ("power", 1), "watts": ("power", 1), "kw": ("power", 1000), "kilowatt": ("power", 1000),
    "btu": ("heat", 1), "btus": ("heat", 1), "btu/hr": ("heat", 1),
    "kwh/yr": ("energy", 1), "kwh/year": ("energy", 1), "kwh per year": ("energy", 1), "kwh": ("energy", 1),
    "kwh/month": ("energy", 12), "kwh/mo": ("energy", 12), "kwh/월": ("energy", 12), "kwh/년": ("energy", 1),
    "v": ("voltage", 1), "volt": ("voltage", 1), "volts": ("voltage", 1), "a": ("current", 1), "amp": ("current", 1),
    "amps": ("current", 1), "ampere": ("current", 1), "hz": ("frequency", 1), "hertz": ("frequency", 1),
    "db": ("sound", 1), "dba": ("sound", 1), "rpm": ("speed", 1), "°f": ("temp", 1), "f": ("temp", 1), "°c": ("temp_c", 1),
    "cfm": ("flow", 1), "min": ("time", 1), "mins": ("time", 1), "minutes": ("time", 1), "ea": ("count", 1),
}
_ATTR_UNITS = {  # canonical display unit of an attribute -> family, factor
    "in": ("length", 1), "lb": ("mass", 1), "kg": ("mass", 2.20462), "cu ft": ("volume", 1), "W": ("power", 1),
    "kWh/yr": ("energy", 1), "V": ("voltage", 1), "A": ("current", 1), "Hz": ("frequency", 1), "dB": ("sound", 1),
    "rpm": ("speed", 1), "°F": ("temp", 1), "CFM": ("flow", 1), "ea": ("count", 1), "min": ("time", 1), "BTU": ("heat", 1),
}


def parse_number(text) -> Optional[float]:
    """'1,200' -> 1200, '28-5/8' / '52 1/16' -> 28.625 / 52.0625, '3/4' -> .75, '.5' -> .5; None if not a number."""
    t = str(text).strip().replace(",", "")
    if not t:
        return None
    m = _FRAC.match(t)
    if m:
        den = int(m.group(3))
        return (int(m.group(1) or 0) + int(m.group(2)) / den) if den else None
    try:
        return float(t)
    except ValueError:
        return None


def _unit_key(raw: Optional[str]) -> Optional[str]:
    u = (raw or "").strip().lower().rstrip(".,;")
    u = re.sub(r"\s+", " ", u)
    if u in _UNITS:
        return u
    u2 = u.replace(".", "").replace("cu ", "cu ").strip()
    if re.match(r"^cu ?ft$", u2) or u2 in ("cubic feet", "cubic ft", "cu feet"):
        return "cu ft"
    return u if u in _UNITS else None


def parse_measure(text, default_unit: str = "") -> Optional[tuple[float, str, str]]:
    """'29.75 in' / '1,200 w' / '28 5/8"' / '3.3 lbs' -> (number, unit key, family). A bare number takes `default_unit`.
    Anything else (ranges, composite strings, prose) -> None."""
    m = _MEASURE.match(unicodedata.normalize("NFKC", str(text or "")))
    if not m:
        return None
    n = parse_number(m.group("n"))
    if n is None:
        return None
    raw = (m.group("u") or "").strip()
    if not raw:
        key = _unit_key(default_unit)
        return (n, key or default_unit, _UNITS.get(key, ("", 1))[0] if key else (_ATTR_UNITS.get(default_unit, ("", 1))[0]))
    key = _unit_key(raw)
    if key is None:
        return None
    return n, key, _UNITS[key][0]


def to_unit(number: float, unit_key: str, target: str) -> Optional[float]:
    """Convert `number` given in `unit_key` (a _UNITS key) to the attribute display unit `target` (a _ATTR_UNITS key)."""
    src = _UNITS.get(unit_key) or _ATTR_UNITS.get(unit_key)
    dst = _ATTR_UNITS.get(target)
    if not src or not dst or src[0] != dst[0]:
        return None
    out = number * src[1] / dst[1]
    return round(out, 3) if not float(out).is_integer() else int(out)


def family_of_unit(unit: str) -> str:
    return _ATTR_UNITS.get(unit, ("", 1))[0]


_YES = re.compile(r"^\s*(?:yes|true|y|included|available|standard|supported|built[- ]?in|✓|있음|예|지원|유)\b", re.I)
_NO = re.compile(r"^\s*(?:no|none|n/?a|false|not\b|without|nil|[-–—]|없음|해당\s?없음|아니오|미지원|무)\s*(?:$|[\s,;:.(/-])", re.I)


def flag_of(value) -> Optional[bool]:
    """True / False for yes-like / no-like text ('Yes (Air Fry)', 'No - tempered glass', 'Not Applicable'), else None."""
    t = _unwrap(str(value if value is not None else "")).strip()
    if not t:
        return None
    if re.fullmatch(r"[oO]", t):
        return True
    if re.fullmatch(r"[xX]", t):
        return False
    if _NO.match(t + " "):
        return False
    if _YES.match(t):
        return True
    return None


_STRICT_FLAG = re.compile(r"^\s*(?:yes|no|true|false|y|n|n/?a|none|not applicable|not available|not possible|not included)\b", re.I)


def value_kind(value) -> Optional[str]:
    """'numeric' (one measure), 'flag' (yes/no), 'text', or None when there is no value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return "flag"
    if isinstance(value, (int, float)):
        return "numeric"
    if _STRICT_FLAG.match(str(value)) and len(str(value)) <= 40:
        return "flag"
    return "numeric" if parse_measure(value, "x") and not re.match(r"^\s*0+\s*$", str(value)) else "text"


_AXIS_ORDER = re.compile(r"(?:^|[^a-z])([whd])\s*[x×*]\s*([whd])\s*[x×*]\s*([whd])(?:$|[^a-z])", re.I)


def dim_order_hint(label: str) -> str:
    """Axis order of a composite dimension label: 'WHD' (default), or e.g. 'HWD' for 'Overall (HxWxD)'. KO 가로x세로x높이 = W,D,H."""
    s = unicodedata.normalize("NFKC", label or "").lower()
    s = re.sub(r"\s+", "", s)
    m = _AXIS_ORDER.search(label or "") or re.search(r"([whd])x([whd])x([whd])", s)
    if m and len({x.lower() for x in m.groups()}) == 3:
        return "".join(x.upper() for x in m.groups())
    if re.search(r"가로.{0,3}세로.{0,3}높이", s):
        return "WDH"
    if re.search(r"(가로|폭).{0,3}높이.{0,3}깊이", s):
        return "WHD"
    return "WHD"


def parse_dims(value, label: str = "") -> Optional[dict]:
    """'28 1/2" x 50 1/4" x 23 1/2"' / '27 5/8" W x 66 7/8" H x 31 7/8" D' / '746 x 1090 x 920 mm' ->
    {'width': in, 'height': in, 'depth': in} (inches); None unless exactly three measures parse."""
    text = str(value or "")
    if len(re.findall(r"[x×*]|\bby\b", text, re.I)) < 2:
        return None
    seg = re.sub(r"\s*(?:[x×*]|\bby\b)\s*", " | ", text, flags=re.I)
    parts = [p for p in re.split(r"\|", seg) if p.strip()]
    if len(parts) != 3:
        return None
    nums, units, axes = [], set(), []
    for p in parts:
        m = re.match(rf"^\s*(?P<n>{_NUM})\s*(?P<u>mm|cm|inch(?:es)?|in\.?|\"|'')?\s*\(?\s*(?P<ax>[whd])?\)?\s*(?P<tail>[a-z]*)\s*$", p, re.I)
        if not m:
            return None
        n = parse_number(m.group("n"))
        if n is None:
            return None
        nums.append(n)
        if m.group("u"):
            units.add(m.group("u").lower().rstrip("."))
        axes.append((m.group("ax") or "").upper())
    if len(units) > 1:
        return None
    unit = next(iter(units), "in")
    unit = "in" if unit in ('"', "''", "inch", "inches") else unit
    if unit in ("in",) and max(nums) > 120:  # metric numbers mislabelled as inches ('746 x 1090 x 920 inch')
        unit = "mm"
    k = {"in": 1, "mm": 1 / 25.4, "cm": 1 / 2.54}[unit]
    order = "".join(axes) if all(axes) and len(set(axes)) == 3 else dim_order_hint(label)
    names = {"W": "width", "H": "height", "D": "depth"}
    return {names[a]: round(n * k, 3) for a, n in zip(order, nums)}


_VHA = (("voltage", re.compile(r"(\d{2,3}(?:\s*[/,&-]\s*\d{2,3})*)\s*v(?:olts?)?\b", re.I)),
        ("frequency", re.compile(r"(\d{2,3})\s*hz\b", re.I)), ("amps", re.compile(r"(\d{1,3}(?:\.\d+)?)\s*a(?:mps?)?\b", re.I)))


def parse_vha(value) -> dict:
    """'208/240V ; 60Hz ; 20A' -> {'voltage': '208/240', 'frequency': 60, 'amps': 20} (only the parts found)."""
    out: dict = {}
    for key, rx in _VHA:
        m = rx.search(str(value or ""))
        if m:
            v = re.sub(r"\s+", "", m.group(1)).replace(",", "/").replace("&", "/")
            out[key] = v if key == "voltage" else parse_number(v)
    return out


_SPLIT_LIST = re.compile(r"\s*(?:\||;|\n|\s/\s|,(?![^()]*\))|\s\+\s|\s&\s|\sand\s|\swith\s)\s*", re.I)


def split_items(value) -> list[str]:
    """Items of a list-valued spec ('Bake / Conv. Bake', 'Self Clean + Steam Clean', 'A | B', 'A, B')."""
    parts = [p.strip(" .;") for p in _SPLIT_LIST.split(str(value or "").replace("\r", ""))]
    return [p for p in parts if p and len(p) <= 80]


# ------------------------------------------------------------------------------------------ normalization
_STOP = {"the", "of", "a", "an", "and", "or", "for", "to", "is"}
_ALIAS = {"fridge": "refrigerator", "conv": "convection", "convect": "convection", "airfry": "air fry", "airfryer": "air fry",
          "airfrying": "air fry", "icemaker": "ice maker", "selfclean": "self clean", "selfcleaning": "self clean", "cutout": "cutout",
          "wifi": "wifi", "w/": "with", "w/o": "without", "energystar": "energy star", "watts": "watt", "wattage": "wattage",
          "amps": "amp", "volts": "volt", "dims": "dimension", "inches": "inch", "lbs": "lb", "temp": "temperature", "baking": "bake", "broiling": "broil", "roasting": "roast", "cleaning": "clean",
          "proofing": "proof", "warming": "warm", "frying": "fry", "defrosting": "defrost", "dispensing": "dispense"}
_UNITWORDS = {"in", "inch", "inches", "lb", "lbs", "ft", "cu", "w", "kw", "v", "a", "hz", "db", "dba", "rpm", "ea", "qty", "kg", "mm",
              "cm", "l", "min", "btu", "kwh", "decimal", "wxhxd", "hxwxd", "dxwxh", "whd", "hwd", "cfm", "f", "c", "year", "yr", "usd",
              "x", "h", "d", "elec", "gal", "mm", "cubic", "feet", "foot", "month", "mo", "per"}
_HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")
_PAREN = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]")
_WRAP = re.compile(r"\bEN\[(.*?)\](?=\s|$)|\bEN\(((?:[^()]|\([^()]*\))*)\)")  # adapters wrap untranslated Korean as EN(...) / EN[...]
_JUNK = re.compile(r"[®™℠©*†‡�​]")


def _unwrap(text: str) -> str:
    return _WRAP.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text)


def stem(tok: str) -> str:
    if _HANGUL.search(tok) or len(tok) <= 3 or tok.isdigit():
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("sses"):
        return tok[:-2]
    if tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def _is_unit_paren(text: str) -> bool:
    toks = re.findall(r"[a-z]+|\d+", text.lower())
    return bool(toks) and all(t in _UNITWORDS or t.isdigit() for t in toks)


@dataclass(frozen=True)
class Norm:
    raw: str
    section: str          # lowercase section hint ('' if none)
    leaf: str             # label without section / unit parentheses
    tokens: tuple         # stemmed content tokens, parenthetical words included
    core_tokens: tuple    # same without parenthetical words
    unit_hint: str        # unit found in a parenthesis ('cu. ft.' -> 'cu ft'), else ''
    sec_tokens: tuple = ()

    @property
    def key(self) -> str:
        return _join(self.tokens)

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


def _join(tokens) -> str:
    key = " ".join(tokens)
    return key.replace(" ", "") if _HANGUL.search(key) else key


def _skey(tokens) -> str:
    return _join(sorted(tokens))


def _tokens(s: str) -> list[str]:
    s = re.sub(r"wi[\s-]*fi", "wifi", s)
    s = s.replace("w/o", " without ").replace("w/", " with ").replace("&", " and ").replace("#", " number ")
    s = re.sub(r"[^0-9a-zㄱ-ㆎ가-힣]+", " ", s)
    out: list[str] = []
    for t in s.split():
        t = _ALIAS.get(t, t)
        for u in t.split():
            u = stem(u)
            if u and u not in _STOP:
                out.append(u)
    return out


def normalize(label: str, section: Optional[str] = None) -> Norm:
    s = unicodedata.normalize("NFKC", str(label or ""))
    s = _unwrap(s)
    s = _JUNK.sub("", s).lower().strip()
    sec = (section or "").lower()
    if " > " in s:
        head, _, s = s.rpartition(" > ")
        sec = f"{head.strip()} {sec}".strip()
    else:
        m = re.match(r"^([^:]{3,28}):\s*(\S.*)$", s)
        if m and len(m.group(1).split()) <= 3:
            sec, s = f"{m.group(1)} {sec}".strip(), m.group(2)
    unit_hint = ""
    for m in _PAREN.finditer(s):
        if _is_unit_paren(m.group(1)):
            unit_hint = unit_hint or (_unit_key(re.sub(r"[()]", "", m.group(1))) or "")
    s = _PAREN.sub(lambda m: "" if _is_unit_paren(m.group(1)) else f"({m.group(1)})", s)
    full = _tokens(re.sub(r"[()\[\]]", " ", s))
    core = _tokens(_PAREN.sub(" ", s))
    leaf = re.sub(r"\s+", " ", re.sub(r"[()\[\]]", " ", s)).strip()
    return Norm(str(label or ""), sec, leaf, tuple(full), tuple(core), unit_hint, tuple(_tokens(sec)))


# --- qualifiers: upper/lower/second cavity and cooktop burner position become an id suffix
_CAV = (("upper", {"upper", "top", "상단", "상부"}), ("lower", {"lower", "bottom", "하단", "하부"}),
        ("second", {"second", "secondary", "2nd"}))
_CAV_LABEL = {"upper": ("upper oven", "상단 오븐"), "lower": ("lower oven", "하단 오븐"), "second": ("2nd cavity", "2번째 캐비티")}
_CAV_NOUN = {"oven", "cavity", "compartment"}
_POS = (("center front", "cf", "center front"), ("center rear", "cr", "center rear"), ("left front", "lf", "left front"),
        ("left rear", "lr", "left rear"), ("right front", "rf", "right front"), ("right rear", "rr", "right rear"),
        ("center", "c", "center"), ("left", "l", "left"), ("right", "r", "right"))


def _extract_cav(tokens: tuple, sec_tokens: tuple) -> tuple[Optional[str], tuple]:
    toks = list(tokens)
    for src_is_label, pool in ((True, toks), (False, list(sec_tokens))):
        for qid, words in _CAV:
            hit = [i for i, t in enumerate(pool) if t in words]
            if hit:
                if not src_is_label:
                    return qid, tuple(toks)
                drop = set(hit)
                for i in hit:  # 'lower oven' / 'second cavity': drop the noun next to the word too
                    for j in (i + 1, i - 1):
                        if 0 <= j < len(pool) and pool[j] in _CAV_NOUN:
                            drop.add(j)
                            break
                return qid, tuple(t for i, t in enumerate(toks) if i not in drop)
    if "primary" in toks:
        return "", tuple(t for t in toks if t not in ("primary", "cavity"))
    return None, tokens


def _extract_pos(tokens: tuple) -> tuple[Optional[str], tuple]:
    toks = list(tokens)
    for phrase, qid, _lab in _POS:
        w = phrase.split()
        for i in range(len(toks) - len(w) + 1):
            if toks[i:i + len(w)] == w:
                return qid, tuple(toks[:i] + toks[i + len(w):])
    return None, tokens


# --- discriminating words: two labels that hit DIFFERENT members of one group never auto-merge
_CONFLICT = (
    ({"width", "w"}, {"height", "h"}, {"depth", "d"}, {"length"}, {"dimension", "size"}),
    ({"refrigerator", "fridge", "fresh"}, {"freezer", "frozen"}),
    ({"upper", "top"}, {"lower", "bottom"}, {"second", "secondary"}),
    ({"bake", "baking"}, {"broil", "broiling"}, {"roast", "roasting"}, {"grill"}, {"steam"}, {"proof", "proofing"}, {"fry"}),
    ({"net"}, {"gross", "shipping", "packing", "packaging", "carton"}),
    ({"lock"}, {"alarm"}, {"light", "lighting"}, {"handle"}, {"filter"}),
    ({"min", "minimum"}, {"max", "maximum"}),
    ({"with", "including", "incl"}, {"without", "excluding", "excl"}),
    ({"watt", "wattage"}, {"amp", "ampere"}, {"volt", "voltage"}, {"hertz", "hz", "frequency"}),
    ({"hot"}, {"cold"}, {"warm"}),
    ({"microwave"}, {"oven"}),
    ({"washer", "wash", "washing"}, {"dryer", "dry", "drying"}),
    ({"cubed"}, {"crushed"}),
    ({"number", "count"}, {"type", "size", "color", "capacity", "power", "location", "material"}),
)


def conflicts(a, b) -> bool:
    sa, sb = set(a), set(b)
    for group in _CONFLICT:
        ha = [i for i, g in enumerate(group) if sa & g]
        hb = [i for i, g in enumerate(group) if sb & g]
        if ha and hb and not (set(ha) & set(hb)):
            return True
    return False


_SECTION_RULES = (
    ("보증·기타", r"warrant|certif|\bupc\b|award|listed|manufactur|\bul\b|prop|kosher|제조|인증|보증"),
    ("연결성", r"wifi|bluetooth|\bapp\b|smart|connect|voice|remote|thinq|alexa|assistant|sync|wi-fi|연결|음성|원격"),
    ("치수·무게", r"width|height|depth|dimension|\bsize\b|weight|clearance|cutout|length|치수|크기|무게"),
    ("용량", r"capacity|용량"),
    ("전기·에너지", r"watt|\bamp|volt|hertz|kwh|energy|power|btu|consumption|electric|fuse|cord|전력|전압|에너지"),
    ("조리(오븐·쿡탑)", r"oven|burner|cooktop|bake|broil|convection|rack|probe|griddle|grate|microwave|cook|grill|ignition|오븐|조리|화구"),
    ("세탁·건조", r"wash|spin|\bdry|dryer|cycle|drum|laundry|rinse|soil|세탁|건조|탈수"),
    ("냉각·신선", r"cool|freez|frost|refrigerant|compressor|crisper|shelf|shelves|drawer|\bice\b|defrost|냉각|냉장|냉동|신선"),
    ("디자인", r"color|finish|handle|door|display|knob|material|style|design|panel|색상|디자인|도어"),
    ("성능", r"noise|speed|rpm|performance|efficien|temperature|time|소음|성능"),
)


def guess_section(text: str, fallback: str = "기능") -> str:
    for sec, rx in _SECTION_RULES:
        if re.search(rx, text.lower()):
            return sec
    return fallback


def kebab(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKC", label).lower()).strip("-")
    if s and len(s) >= 2:
        return s[:48].strip("-")
    return "ko-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]


def _cat(category) -> str:
    try:
        import catalog
        return catalog.normalize_major(category) or "other"
    except Exception:  # noqa: BLE001
        return str(category or "other").lower()


def _soft_overlap(a: tuple, b: tuple) -> float:
    """Soft Jaccard of two token lists: tokens match when their difflib ratio >= 0.8 (convect ~ convection)."""
    if not a or not b:
        return 0.0
    unused, matched = list(b), 0
    for t in a:
        best, bi = 0.0, -1
        for i, u in enumerate(unused):
            r = 1.0 if t == u else difflib.SequenceMatcher(None, t, u).ratio()
            if r > best:
                best, bi = r, i
        if best >= 0.8:
            matched += 1
            unused.pop(bi)
    return matched / (len(a) + len(b) - matched)


_GENERIC = {"mode", "type", "function", "feature", "setting", "option", "total", "overall", "appliance", "number", "count", "no", "item"}
_NOUNS = {"rack", "light", "lamp", "drawer", "shelf", "probe", "basket", "tray", "filter", "burner", "element", "guide", "rail"}  # physical parts
DISTINCT_OK = 0.93  # an embedding score this high overrides the 'each side has its own extra word' guard


def distinct_heads(a: tuple, b: tuple) -> bool:
    """True when the labels share a word but BOTH carry a content word the other lacks ('Slow Roast' / 'Slow Cook', 'Standard Rack' /
    'Glide Rack', 'Language Conversion' / 'Temperature Conversion'): different specs sharing one word. One side being a plain
    extension of the other ('Overall Appliance Width' / 'Overall Width') is not distinct. Generic words (mode, type, ...) do not count."""
    ta = [t for t in a if t not in _GENERIC]
    tb = [t for t in b if t not in _GENERIC]
    if not ta or not tb:
        return False
    unused, only_a, shared = list(tb), 0, 0
    for t in ta:
        best, bi = 0.0, -1
        for i, u in enumerate(unused):
            r = 1.0 if t == u else difflib.SequenceMatcher(None, t, u).ratio()
            if r > best:
                best, bi = r, i
        if best >= 0.8:
            unused.pop(bi)
            shared += 1
        else:
            only_a += 1
    return shared > 0 and only_a > 0 and len(unused) > 0  # no shared word at all is the embedding's / the LLM's call


def fallback_score(a: tuple, b: tuple) -> float:
    """Deterministic similarity of two token tuples: half character ratio, half soft token overlap."""
    return 0.5 * difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() + 0.5 * _soft_overlap(a, b)


# ------------------------------------------------------------------------------------------ embeddings
class _EmbedCache:
    """sha1(text) -> float16 vector, one model per file (data/embed_cache.json); fetches missing texts in batches."""

    def __init__(self, path: Path, embed_fn: Callable, batch: int = 64):
        self.path, self.fn, self.batch = path, embed_fn, batch
        self.vecs: dict[str, "np.ndarray"] = {}
        self.dirty = False
        self.model = ""
        self._load()

    def _load(self) -> None:
        import numpy as np
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.model = data.get("model", "")
            self.vecs = {k: np.frombuffer(base64.b64decode(v), dtype=np.float16).astype(np.float32) for k, v in data.get("v", {}).items()}
        except (OSError, ValueError):
            self.vecs = {}

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def get(self, texts: list[str]):
        """float32 matrix (rows L2-normalised) for texts, or None if any embedding is missing and unreachable."""
        import numpy as np
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.vecs]
        for i in range(0, len(missing), self.batch):
            chunk = missing[i:i + self.batch]
            vecs = self.fn(chunk)
            if not vecs or len(vecs) != len(chunk):
                return None
            model = getattr(self.fn, "model", None) or getattr(sys.modules.get("llm"), "EMBED_MODEL", "") or "unknown"
            if self.model and model != self.model and self.vecs:  # a different model: old vectors are not comparable
                self.vecs, self.model, self.dirty = {}, model, True
                return self.get(texts)
            self.model = model
            for t, v in zip(chunk, vecs):
                a = np.asarray(v, dtype=np.float32)
                n = float(np.linalg.norm(a))
                self.vecs[self._key(t)] = a / n if n else a
            self.dirty = True
        return np.stack([self.vecs[self._key(t)] for t in texts]) if texts else np.zeros((0, 1), dtype=np.float32)

    def save(self) -> None:
        import numpy as np
        if not self.dirty:
            return
        payload = {"model": self.model, "v": {k: base64.b64encode(v.astype(np.float16).tobytes()).decode("ascii") for k, v in self.vecs.items()}}
        _atomic_write(self.path, json.dumps(payload, separators=(",", ":")))
        self.dirty = False


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except PermissionError:  # locked by another process: the registry is only a cache of decisions, never block
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"[canon] could not write {path}", file=sys.stderr)


# ------------------------------------------------------------------------------------------ the canonicalizer
_UNSET = object()


class Canonicalizer:
    """Holds the seed ontology + persistent registry; thread-safe. See the module docstring for the pipeline."""

    def __init__(self, data_dir: Optional[Path] = None, *, embed_fn=_UNSET, llm_fn=_UNSET,
                 offline: Optional[bool] = None, persist: bool = True):
        self.dir = Path(data_dir or os.environ.get("FRIDGE_CANON_DIR") or ROOT / "data")
        self.persist = persist
        if offline is None:
            offline = bool(os.environ.get("FRIDGE_CANON_OFFLINE")) or os.environ.get("FRIDGE_MOCK") == "1"
        if data_dir is None and not os.environ.get("FRIDGE_CANON_DIR") and Path(sys.argv[0] or "").name.startswith("test_"):
            offline, self.persist = True, False  # a test script run directly: never call LM Studio or write the real registry
        self.offline = offline
        self._lock = threading.RLock()
        self._embed_fn = None if embed_fn is _UNSET else embed_fn
        self._llm_fn = None if llm_fn is _UNSET else llm_fn
        if not offline:
            import llm
            self._embed_fn = llm.embed if embed_fn is _UNSET else embed_fn
            self._llm_fn = llm.chat_json if llm_fn is _UNSET else llm_fn
        self._embed_dead_until = 0.0
        self._synk: dict = {}
        self._llm_left = LLM_BUDGET
        self._cache: Optional[_EmbedCache] = None
        self._mats: dict = {}
        self.stats = {"embed_calls": 0, "llm_calls": 0}
        self._run = self._fresh_run()
        self._pruned = 0     # learned merges dropped at load because the guards refuse them now
        self.attrs: dict[str, dict] = {}
        self._idx: dict = {}
        self._sidx: dict = {}
        self._dirty = False
        self._load_seed()
        self._load_registry()
        self._load_overrides()

    # ---------------------------------------------------------------- loading
    def _load_seed(self) -> None:
        path = self.dir / "canon_seed.json"
        if not path.exists():
            path = ROOT / "data" / "canon_seed.json"
        seed = json.loads(path.read_text(encoding="utf-8"))
        for order, a in enumerate(seed["attrs"]):
            a = dict(a)
            a["order"] = order
            a["origin"] = "seed"
            self.attrs[a["id"]] = a
        self._reindex()

    def _load_registry(self) -> None:
        self._reg = {"version": 1, "attrs": {}, "labels": {}, "verdicts": {}}
        try:
            data = json.loads((self.dir / "canon_registry.json").read_text(encoding="utf-8"))
            for k in ("attrs", "labels", "verdicts"):
                self._reg[k] = dict(data.get(k, {}))
        except (OSError, ValueError):
            pass
        for i, (aid, a) in enumerate(self._reg["attrs"].items()):
            if aid not in self.attrs:
                self.attrs[aid] = {**a, "id": aid, "order": 5000 + i, "origin": "registry"}
        self._reindex()
        self._prune_registry()

    def _prune_registry(self) -> None:
        """Forget learned fuzzy merges (embed / llm / fallback) that today's guards would refuse: earlier, laxer runs may have
        stored e.g. 'Standard Rack' -> gliding racks. The label is then resolved again (and usually becomes its own row)."""
        bad = []
        for key, ent in self._reg["labels"].items():
            a = self.attrs.get(ent.get("id", ""))
            if a is None or ent.get("m") not in ("embed", "llm", "fallback") or not ent.get("label"):
                continue
            try:
                if self._blocked(normalize(ent["label"]), a, float(ent.get("s", 0))):
                    bad.append(key)
            except Exception:  # noqa: BLE001 - a malformed entry is simply kept
                continue
        for key in bad:
            del self._reg["labels"][key]
        if bad:
            self._dirty = True
            self._pruned = len(bad)

    def _load_overrides(self) -> None:
        self._ov: dict[str, dict] = {}
        self._ov_ids: dict[str, str] = {}
        try:
            data = json.loads((self.dir / "canon_overrides.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for kind in ("merge", "split"):
            for ent in data.get(kind, []) or []:
                into = ent.get("into") or ent.get("as")
                if not into:
                    continue
                if into not in self.attrs:
                    self._add_attr({"id": into, "en": ent.get("label_en") or into.replace("-", " ").title(), "ko": ent.get("label_ko", ""),
                                    "sec": ent.get("section") or "기능", "kind": ent.get("kind", "text"), "cats": list(CATS), "core": [],
                                    "syn": []}, origin="override")
                for lab in ent.get("labels", []) or []:
                    n = normalize(lab)
                    for key in {n.key, _skey(n.tokens)}:
                        self._ov[f"{ent.get('category') or '*'}|{key}"] = {"into": into, "kind": kind}
                for aid in ent.get("ids", []) or []:
                    self._ov_ids[aid] = into

    def _syn_keys(self, a: dict) -> list[tuple[str, tuple]]:
        cached = self._synk.get(a["id"])
        if cached is not None and cached[0] == len(a.get("syn", ())):
            return cached[1]
        out = self._compute_syn_keys(a)
        self._synk[a["id"]] = (len(a.get("syn", ())), out)
        return out

    def _compute_syn_keys(self, a: dict) -> list[tuple[str, tuple]]:
        out = []
        for raw in [a.get("en", ""), a.get("ko", ""), *a.get("syn", [])]:
            if raw == "_":
                out.append(("", ()))
                continue
            toks = tuple(normalize(raw).tokens)
            if toks:
                out.append((_join(toks), toks))
        return out

    def _index_attr(self, a: dict) -> None:
        spaces = ("attr", "item") if a.get("kind") == "flag" else ("attr",)
        for key, toks in self._syn_keys(a):
            for cat in (*a.get("cats", CATS), "*"):
                for sp in spaces:
                    self._idx.setdefault((sp, cat), {}).setdefault(key, a["id"])
                    if toks:
                        self._sidx.setdefault((sp, cat), {}).setdefault(_skey(toks), a["id"])

    def _reindex(self) -> None:
        self._idx, self._sidx = {}, {}
        for a in self.attrs.values():
            self._index_attr(a)
        self._mats = {}

    def _add_attr(self, a: dict, origin: str) -> dict:
        a = {**a, "origin": origin, "order": a.get("order", 9000 + len(self.attrs))}
        self.attrs[a["id"]] = a
        self._index_attr(a)
        return a

    # ---------------------------------------------------------------- public API
    def begin(self) -> None:
        """Reset the per-build LLM budget and run statistics (called by compare_model.build_compare)."""
        with self._lock:
            self._llm_left = LLM_BUDGET
            self._run = self._fresh_run()

    @staticmethod
    def _fresh_run() -> dict:
        return {"methods": {k: 0 for k in STAT_KEYS}, "seen": set(), "embed_ok": 0, "embed_fail": 0, "llm_calls": 0}

    def tally(self, kind: str, key=None) -> None:
        """Count one distinct label (`key` dedupes within the run) under `kind` (one of STAT_KEYS)."""
        with self._lock:
            run = self._run
            if key is not None:
                if key in run["seen"]:
                    return
                run["seen"].add(key)
            run["methods"][kind] = run["methods"].get(kind, 0) + 1

    def run_stats(self) -> dict:
        """{'methods': {stage: n}, 'labels': n, 'embed_stage': 'used'|'unavailable'|'offline'|'not_needed', 'embed_ok', 'embed_fail',
        'embed_model', 'llm_calls', 'line_ko': one-line summary} of the current run (since begin())."""
        with self._lock:
            run = self._run
            methods = dict(run["methods"])
            if self.offline or self._embed_fn is None:
                stage = "offline"
            elif run["embed_fail"] or time.time() < self._embed_dead_until:
                stage = "unavailable"
            elif run["embed_ok"]:
                stage = "used"
            else:
                stage = "not_needed"
            out = {"methods": methods, "labels": sum(methods.values()), "embed_stage": stage, "embed_ok": run["embed_ok"],
                   "embed_fail": run["embed_fail"], "embed_model": (self._cache.model if self._cache is not None else "") or "",
                   "llm_calls": run["llm_calls"], "registry_pruned": self._pruned}
        out["line_ko"] = stats_line(out)
        return out

    def known(self, label: str, *, category: str, value=None, section: Optional[str] = None, space: str = "attr") -> bool:
        """True when the label resolves by the cheap stages (override / seed / registry) - no learning, no embedding, no counting."""
        with self._lock:
            n = normalize(label, section)
            return bool(n.tokens) and self._cheap_hit(n, _cat(category), space)

    def canonicalize(self, label: str, *, category: str, value=None, section: Optional[str] = None, space: str = "attr") -> Canon:
        with self._lock:
            return self._resolve(label, _cat(category), value, section, space)

    def canonicalize_item(self, item: str, *, category: str, value=None, section: Optional[str] = None) -> Canon:
        """Like canonicalize() for a list item (cooking mode, feature ...): own item space, kind 'list-item'."""
        with self._lock:
            return replace(self._resolve(item, _cat(category), value, section, "item"), kind="list-item")

    def get(self, canon_id: str, *, category: Optional[str] = None, method: str = "seed", score: float = 1.0) -> Optional[Canon]:
        """Canon of a known id (optionally 'base:qualifier'); `category` decides the core flag."""
        with self._lock:
            base, _, qual = canon_id.partition(":")
            a = self.attrs.get(base)
            return self._canon(a, qual or None, method, score, "attr", _cat(category) if category else None) if a else None

    def prime(self, items: list[tuple[str, str, Optional[str], str]]) -> None:
        """Pre-embed, in ONE batch, the labels (label, category, section, space) the cheap stages cannot resolve."""
        if not self._embedding_ok():
            return
        with self._lock:
            todo, cats = [], set()
            for label, category, section, space in items:
                cat = _cat(category)
                n = normalize(label, section)
                if n.tokens and not self._cheap_hit(n, cat, space):
                    todo.append(n.text)
                    cats.add((space, cat))
            try:
                for sp, cat in cats:
                    self._matrix(sp, cat)
                if todo:
                    self._cache_get(todo)
            except Exception as exc:  # noqa: BLE001 - priming is an optimisation only
                print(f"[canon] prime failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def save(self) -> None:
        with self._lock:
            if not self.persist:
                return
            if self._dirty:
                _atomic_write(self.dir / "canon_registry.json", json.dumps(self._reg, ensure_ascii=False, indent=1, sort_keys=True))
                self._dirty = False
            if self._cache is not None:
                self._cache.save()

    # ---------------------------------------------------------------- resolution
    def _resolve(self, label: str, cat: str, value, section: Optional[str], space: str) -> Canon:
        n = normalize(label, section)
        if not n.tokens:
            return Canon("unnamed", "(unnamed)", "(이름 없음)", "보증·기타", "text", False, "new", 0.0)
        seen = (space, cat, n.key)
        ov = self._override(n, cat)
        if ov is not None:
            self.tally("override", seen)
            return self._finish(ov[0], None, "override", 1.0, space, value, cat)
        hit = self._lookup(n, cat, space)
        if hit is not None:
            attr, qual, method, score, via = hit
            self.tally(via, seen)
        else:
            attr, method, score = self._fuzzy(n, cat, space, value)
            qual = None
            if attr is None:
                attr = self._new_attr(n, label, cat, space, value, score)
                method = "new"
            self.tally(method, seen)
        return self._finish(attr, qual, method, score, space, value, cat)

    def _finish(self, attr: dict, qual, method: str, score: float, space: str, value, cat: Optional[str] = None) -> Canon:
        redirect = self._ov_ids.get(attr["id"])
        if redirect and redirect in self.attrs:
            attr, method, score = self.attrs[redirect], "override", 1.0
        attr = self._apply_alt(attr, value)
        return self._canon(attr, qual, method, score, space, cat)

    def _apply_alt(self, attr: dict, value) -> dict:
        alt = attr.get("alt")
        if alt and value is not None:
            m = parse_measure(value, "")
            if m and m[2] and m[2] != family_of_unit(attr.get("unit", "")):
                tgt = self.attrs.get(alt.get(m[2], ""))
                if tgt:
                    return tgt
        return attr

    def _canon(self, a: dict, qual: Optional[str], method: str, score: float, space: str, cat: Optional[str] = None) -> Canon:
        kind = {"numeric": "numeric", "text": "text", "flag": "flag", "list": "text"}.get(a.get("kind", "text"), "text")
        cid, en, ko = a["id"], a["en"], a.get("ko") or ""
        if qual and (a.get("cav") or a.get("pos")):
            cid = f"{cid}:{qual}"
            if qual in _CAV_LABEL:
                lab = _CAV_LABEL[qual]
            else:
                lab = next(((p[2], p[2]) for p in _POS if p[1] == qual), (qual, qual))
            en = f"{en} ({lab[0]})"
            ko = f"{ko or en} ({lab[1]})" if ko else ""
        return Canon(cid, en, ko, a.get("sec") or "기능", "list-item" if space == "item" and kind == "flag" else kind,
                     self._is_core(a, cat),
                     method, round(float(score), 3), a.get("unit", ""), tuple(a.get("comp", ())), a.get("kind") == "list",
                     a.get("order", 9000), tuple((a.get("alt") or {}).items()), bool(a.get("cav")),
                     tuple(a.get("also", ())))

    @staticmethod
    def _is_core(a: dict, cat: Optional[str]) -> bool:
        core = a.get("core")
        if isinstance(core, list):
            return (cat in core) if cat in CATS else bool(core)
        return bool(core)

    def _override(self, n: Norm, cat: str):
        for c in (cat, "*"):
            for key in (n.key, _skey(n.tokens), _join(n.core_tokens)):
                ent = self._ov.get(f"{c}|{key}")
                if ent and ent["into"] in self.attrs:
                    return self.attrs[ent["into"]], ent
        return None

    def _index_hit(self, space: str, cat: str, toks: tuple) -> Optional[str]:
        key = _join(toks)
        idx = self._idx.get((space, cat)) or {}
        if key in idx:
            return idx[key]
        return (self._sidx.get((space, cat)) or {}).get(_skey(toks)) if toks else None

    def _variants(self, n: Norm):
        """Token variants to try, most specific first: [(tokens, cav qualifier, pos qualifier)]."""
        out = []
        for toks in dict.fromkeys([n.tokens, n.core_tokens]):
            cav, t1 = _extract_cav(toks, n.sec_tokens)
            pos, t2 = _extract_pos(t1)
            if cav is not None or pos is not None:
                out.append((t2, cav, pos))
        for toks in dict.fromkeys([n.tokens, n.core_tokens]):
            out.append((toks, None, None))
        if n.sec_tokens:
            out.append((n.sec_tokens + n.core_tokens, None, None))
        return out

    def _lookup(self, n: Norm, cat: str, space: str):
        """Seed/registry hit -> (attr, qualifier, method, score, stat key) or None."""
        cats = [cat] if cat in CATS else ["*"]
        for toks, cav, pos in self._variants(n):
            for c in cats:
                aid = self._index_hit(space, c, toks)
                if not aid:
                    continue
                a = self.attrs[aid]
                if (cav and not a.get("cav")) or (pos and not a.get("pos")):
                    continue
                qual = (cav if a.get("cav") and cav else None) or (pos if a.get("pos") and pos else None)
                kind = "seed" if a.get("origin") == "seed" else "exact"
                return a, qual, kind, 1.0, kind
        learned = self._reg["labels"].get(f"{space}|{cat}|{n.key}")
        if learned and learned["id"] in self.attrs:
            m = learned.get("m", "exact")
            return self.attrs[learned["id"]], None, ("exact" if m == "new" else m), float(learned.get("s", 1.0)), "registry"
        return None

    def _cheap_hit(self, n: Norm, cat: str, space: str) -> bool:
        return self._override(n, cat) is not None or self._lookup(n, cat, space) is not None

    # ---------------------------------------------------------------- stage 4/5: fuzzy matching
    def _embedding_ok(self) -> bool:
        return self._embed_fn is not None and not self.offline and time.time() >= self._embed_dead_until

    def _cache_get(self, texts: list[str]):
        if self._cache is None:
            self._cache = _EmbedCache(self.dir / "embed_cache.json", self._embed_fn)
        self.stats["embed_calls"] += 1
        vecs = self._cache.get(texts)
        if vecs is None:
            self._embed_dead_until = time.time() + 120  # LM Studio unreachable: do not hammer it, use the fallback
            self._run["embed_fail"] += 1
        else:
            self._run["embed_ok"] += 1
        return vecs

    def _cands(self, space: str, cat: str) -> list[dict]:
        return [a for a in self.attrs.values() if (cat in a.get("cats", CATS) or cat not in CATS) and (space == "attr" or a.get("kind") == "flag")]

    def _matrix(self, space: str, cat: str):
        key = (space, cat, len(self.attrs))
        if key in self._mats:
            return self._mats[key]
        owners, texts = [], []
        for a in self._cands(space, cat):
            for raw in dict.fromkeys(t for t in [a.get("en", ""), a.get("ko", ""), *a.get("syn", [])] if t and t != "_"):
                owners.append(a["id"])
                texts.append(normalize(raw).text or raw)
        mat = self._cache_get(texts) if texts else None
        self._mats = {k: v for k, v in self._mats.items() if k[:2] != (space, cat)}
        self._mats[key] = (owners, mat) if mat is not None else None
        return self._mats[key]

    @staticmethod
    def _compatible(a: dict, value) -> bool:
        vk = value_kind(value)
        if vk is None:
            return True
        ak = a.get("kind", "text")
        if vk == "numeric":
            if ak != "numeric":
                return False
            m = parse_measure(value, "")
            return not (m and m[2] and a.get("unit") and family_of_unit(a["unit"]) not in ("", m[2]) and a["unit"] != "ea")
        if vk == "flag":
            return ak in ("flag", "text", "list")
        return ak in ("text", "flag", "list")

    def _fuzzy(self, n: Norm, cat: str, space: str, value):
        """-> (attr or None, method, best score). Embeddings (+ LLM for the grey zone), else deterministic fallback."""
        best_score = 0.0
        if self._embedding_ok():
            try:
                mat = self._matrix(space, cat)
                q = self._cache_get([n.text]) if mat is not None else None
            except Exception as exc:  # noqa: BLE001
                print(f"[canon] embedding stage failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._embed_dead_until = time.time() + 120
                mat = q = None
            if mat is not None and q is not None and len(mat[0]):
                owners, m = mat
                sims = m @ q[0]
                per: dict[str, float] = {}
                for o, s in zip(owners, sims):
                    if s > per.get(o, -1):
                        per[o] = float(s)
                for aid, score in sorted(per.items(), key=lambda kv: -kv[1])[:6]:
                    a = self.attrs[aid]
                    if not self._compatible(a, value) or self._blocked(n, a, score):
                        continue
                    best_score = score
                    if score >= EMBED_AUTO:
                        return self._learn(n, cat, space, a, "embed", score)
                    if score >= EMBED_ASK and self._ask(n, a, cat, value):
                        return self._learn(n, cat, space, a, "llm", score)
                    break
                return None, "new", best_score
        # deterministic fallback (no embeddings): strict, token + difflib, same guards
        best, ba = 0.0, None
        pre = {t[:4] for t in n.tokens}
        for a in self._cands(space, cat):
            if not self._compatible(a, value):
                continue
            for _key, toks in self._syn_keys(a):
                if not toks or not (pre & {t[:4] for t in toks}) or conflicts(n.tokens, toks) or distinct_heads(n.tokens, toks):
                    continue
                s = fallback_score(n.tokens, toks)
                if s > best:
                    best, ba = s, a
        if ba is not None and best >= FALLBACK_MIN:
            return self._learn(n, cat, space, ba, "fallback", best)
        return None, "new", best

    def _blocked(self, n: Norm, a: dict, score: float) -> bool:
        """Guards of a fuzzy merge: discriminating words (vs the best synonym AND the attribute's own label), and - for same-script
        labels scoring below DISTINCT_OK - 'each side has its own extra word'."""
        syn = self._best_syn(a, n)
        en = tuple(normalize(a.get("en", "")).tokens)
        if conflicts(n.tokens, syn) or conflicts(n.tokens, en):
            return True
        if (_NOUNS & set(n.tokens)) - set(syn) - set(en):  # 'Self-Cleaning Oven Racks' is about racks, not about self clean
            return True
        if score >= DISTINCT_OK or _HANGUL.search(n.raw) or _HANGUL.search(" ".join(syn)):
            return False
        return distinct_heads(n.tokens, syn)

    def _best_syn(self, a: dict, n: Norm) -> tuple:
        syns = [t for _k, t in self._syn_keys(a) if t]
        return max(syns, key=lambda t: fallback_score(n.tokens, t), default=())

    def _learn(self, n: Norm, cat: str, space: str, attr: dict, method: str, score: float):
        self._reg["labels"][f"{space}|{cat}|{n.key}"] = {"id": attr["id"], "m": method, "s": round(score, 3), "label": n.raw[:80]}
        self._dirty = True
        return attr, method, score

    def _ask(self, n: Norm, a: dict, cat: str, value) -> bool:
        """LLM yes/no for a grey-zone pair; verdicts are cached in the registry (asked once per pair)."""
        vkey = hashlib.sha1(f"{cat}|{n.key}|{a['id']}".encode("utf-8")).hexdigest()
        cached = self._reg["verdicts"].get(vkey)
        if cached is not None:
            return bool(cached.get("same"))
        if self._llm_fn is None or self._llm_left <= 0:
            return False
        self._llm_left -= 1
        self.stats["llm_calls"] += 1
        self._run["llm_calls"] += 1
        prompt = (f"Appliance spec sheets, product category: {cat}. Do these two spec labels name the SAME specification "
                  f"(the same measured attribute or feature, not a related but different one such as width vs height, "
                  f"fridge vs freezer, bake vs broil, net vs gross weight, door lock vs door alarm, microwave vs oven, "
                  f"a specific variant such as 'Audible Preheat Signal' vs 'Audible Signal', a plain rack vs a gliding rack)? "
                  f"When in doubt answer false: a wrong merge hides a real difference between products.\n"
                  f"A: \"{n.raw}\" (sample value: {str(value)[:60] if value is not None else 'n/a'}; section: {n.section or 'n/a'})\n"
                  f"B: \"{a['en']}\" / \"{a.get('ko', '')}\" (also called: {'; '.join(a.get('syn', [])[:6])})\n"
                  f"Answer ONLY JSON: {{\"same\": true}} or {{\"same\": false}}.")
        try:
            res = self._llm_fn(prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"[canon] llm verdict failed: {type(exc).__name__}", file=sys.stderr)
            return False
        if not isinstance(res, dict) or "same" not in res:
            return False
        same = res["same"] is True or str(res["same"]).lower() == "true"
        self._reg["verdicts"][vkey] = {"same": same, "a": n.raw[:60], "b": a["id"]}
        self._dirty = True
        return same

    # ---------------------------------------------------------------- stage 6: new canonical attribute
    def _new_attr(self, n: Norm, label: str, cat: str, space: str, value, score: float) -> dict:
        vk = value_kind(value)
        kind = {"numeric": "numeric", "flag": "flag"}.get(vk or "", "flag" if space == "item" else "text")
        clean = re.sub(r"\s+", " ", _PAREN.sub(lambda m: "" if _is_unit_paren(m.group(1)) else m.group(0), n.raw.rsplit(" > ", 1)[-1])).strip(" :-")
        en = clean if clean and not _HANGUL.search(clean) else (clean or n.raw)
        ko = clean if _HANGUL.search(clean) else ""
        base = kebab(clean or n.raw)
        aid = base
        cur = self.attrs.get(aid)
        if cur is not None and space == "item" and cur.get("kind") != "flag":
            aid = f"{base}-item"  # a numeric / text attribute owns this id: the item gets its own flag attribute
            cur = self.attrs.get(aid)
        if cur is not None and cat in cur.get("cats", CATS):
            return cur
        if cur is not None:  # same label in another category: widen the learned attribute instead of a second copy
            cur["cats"] = sorted(set(cur.get("cats", [])) | ({cat} if cat in CATS else set(CATS)))
            if cur.get("origin") != "seed":
                self._reg["attrs"][aid] = {k: v for k, v in cur.items() if k not in ("id", "origin", "order")}
            self._index_attr(cur)
            self._dirty = True
            return cur
        sec = self._classify(n, cat, value, space)
        attr = {"id": aid, "en": en, "ko": ko, "sec": sec, "kind": kind, "cats": [cat] if cat in CATS else list(CATS), "core": [],
                "syn": [], "unit": self._unit_of(f"1 {n.unit_hint}" if n.unit_hint else value)}
        if not attr["unit"]:
            attr.pop("unit")
        self._reg["attrs"][aid] = {k: v for k, v in attr.items() if k != "id"}
        self._reg["labels"][f"{space}|{cat}|{n.key}"] = {"id": aid, "m": "new", "s": round(score, 3), "label": n.raw[:80]}
        self._dirty = True
        return self._add_attr(attr, "registry")

    @staticmethod
    def _unit_of(value) -> str:
        m = parse_measure(value, "") if value is not None else None
        if not m or not m[2]:
            return ""
        return {"length": "in", "mass": "lb", "volume": "cu ft", "power": "W", "energy": "kWh/yr", "voltage": "V", "current": "A",
                "frequency": "Hz", "sound": "dB", "speed": "rpm", "flow": "CFM", "heat": "BTU", "temp": "°F", "time": "min"}.get(m[2], "")

    def _classify(self, n: Norm, cat: str, value, space: str) -> str:
        """Section of a new attribute: keyword rules, else the nearest known attribute's section, else LLM (cached), else 기능."""
        txt = f"{n.text} {n.section}"
        for sec, rx in _SECTION_RULES:
            if re.search(rx, txt):
                return sec
        if self._llm_fn is not None and not self.offline and self._llm_left > 0:
            self._llm_left -= 1
            self.stats["llm_calls"] += 1
            self._run["llm_calls"] += 1
            try:
                res = self._llm_fn(f"Classify the appliance spec label \"{n.raw}\" (category {cat}, sample value "
                                   f"{str(value)[:50] if value is not None else 'n/a'}) into exactly one of: {', '.join(SECTIONS)}. "
                                   f"Answer ONLY JSON: {{\"section\": \"<one of the list>\"}}.")
                if isinstance(res, dict) and res.get("section") in SECTIONS:
                    return res["section"]
            except Exception:  # noqa: BLE001
                pass
        return "기능" if space == "item" or value_kind(value) in ("flag", None) else "보증·기타"


# ------------------------------------------------------------------------------------------ module-level default instance
_default: Optional[Canonicalizer] = None
_default_lock = threading.Lock()


def get_default() -> Canonicalizer:
    global _default
    with _default_lock:
        if _default is None:
            _default = Canonicalizer()
        return _default


def set_default(c: Optional[Canonicalizer]) -> None:
    """Install (or, with None, drop) the process-wide canonicalizer - tests use a temp dir + a fake embedder."""
    global _default
    with _default_lock:
        _default = c


def canonicalize(label: str, *, category: str, value=None, section: Optional[str] = None) -> Canon:
    return get_default().canonicalize(label, category=category, value=value, section=section)


def canonicalize_item(item: str, *, category: str, value=None, section: Optional[str] = None) -> Canon:
    return get_default().canonicalize_item(item, category=category, value=value, section=section)
