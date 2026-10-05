"""Benchmark-oriented product filters: schema, candidate filtering and disjunctive facet counts.

Pure functions (inputs are never mutated). A candidate's value for a filter group is resolved, in order, from
  1. a collected ProductRecord (`products[url]`, level 'spec'),
  2. Candidate.attrs (adapter listing facts, standard units: cu ft, in, kWh/yr),
  3. facts derived from the candidate name / sub key (regex, medium confidence),
and is 'unknown' when none of them knows it. Selections: group AND, values OR; multi/bucket groups take a list of
value keys (the pseudo value UNKNOWN includes unknowns for that group), boolean groups take True, 'price' takes
{"min": x, "max": y} (inclusive), 'keyword' takes a string. Unknown keys/values raise ValueError (HTTP 422).
"""
import math
import re
from typing import Any, Callable, Optional

import catalog
import units
from catalog import Candidate

UNKNOWN = "__unknown__"
MAX_GROUPS, MAX_VALUES, MAX_STR = 20, 50, 64
MAX_NUM = 1e9
HIDDEN_KEYS = {"keyword", "region"}  # valid selections, not rendered as panel groups


# ------------------------------------------------------------------ schema
def _plain_buckets(edges: list[float], unit: str) -> list[dict]:
    out = [{"key": f"lt{edges[0]:g}", "label_ko": f"~{edges[0]:g}{unit}", "min": None, "max": edges[0]}]
    out += [{"key": f"{lo:g}_{hi:g}", "label_ko": f"{lo:g}~{hi:g}{unit}", "min": lo, "max": hi}
            for lo, hi in zip(edges, edges[1:])]
    out.append({"key": f"{edges[-1]:g}_plus", "label_ko": f"{edges[-1]:g}{unit}~", "min": edges[-1], "max": None})
    return out


def _capacity_buckets(edges: list[float]) -> list[dict]:
    vals = _plain_buckets(edges, "L")
    for v in vals:  # dual-unit label, e.g. "500~600L (17.7~21.2 cu ft)"
        lo, hi = v["min"], v["max"]
        if lo is None:
            v["label_ko"] += f" (~{units.l_to_cuft(hi):.1f} cu ft)"
        elif hi is None:
            v["label_ko"] += f" ({units.l_to_cuft(lo):.1f} cu ft~)"
        else:
            v["label_ko"] += f" ({units.l_to_cuft(lo):.1f}~{units.l_to_cuft(hi):.1f} cu ft)"
    return vals


def _width_buckets(spec: list[tuple[str, str, Optional[float], Optional[float]]]) -> list[dict]:
    """Width classes in inches; label shows in/mm."""
    out = []
    for key, label, lo, hi in spec:
        out.append({"key": key, "label_ko": label, "min": lo, "max": hi})
    return out


def _g(key, label_ko, label_en, type_="multi", level="listing", values=None, unit=None, display_units=None,
       regions=("*",), default_open=True, more_after=6, **extra) -> dict:
    d = {"key": key, "label_ko": label_ko, "label_en": label_en, "type": type_, "level": level, "unit": unit,
         "display_units": list(display_units or []), "values": values, "regions": list(regions),
         "default_open": default_open, "more_after": more_after}
    d.update(extra)
    return d


def _vals(*pairs: tuple[str, str]) -> list[dict]:
    return [{"key": k, "label_ko": label} for k, label in pairs]


_SMART = _g("smart", "스마트", "Smart", values=_vals(("wifi", "Wi-Fi"), ("app_control", "앱 제어"), ("screen", "스마트 스크린")))
_ENERGY = _g("energy", "에너지", "Energy", level="spec", unit="kWh/yr", values=_vals(
    ("energy_star", "ENERGY STAR"), ("kwh_lt350", "연 350kWh 미만"), ("kwh_350_450", "연 350~450kWh"),
    ("kwh_450_plus", "연 450kWh 이상")))
_KR_GRADE = _g("kr_grade", "에너지소비효율등급(KR)", "KR energy grade", level="spec", regions=("kr",),
               values=_vals(*[(f"grade_{i}", f"{i}등급") for i in range(1, 6)]))
_EU_CLASS = _g("eu_class", "에너지 등급(EU)", "EU energy label", level="spec", regions=("eu",),
               values=_vals(*[(f"class_{c.lower()}", c) for c in "ABCDEFG"]))
_PRICE = _g("price", "가격대", "Price", type_="range", unit="price", values=None, more_after=0)

FILTER_SCHEMA: dict[str, list[dict]] = {
    "common": [
        _g("brand", "제조사(브랜드)", "Brand", values=None),
        _PRICE,
    ],
    "refrigerator:*": [
        _g("door_type", "도어 타입", "Door type", major_only=True, values=_vals(
            ("french_door", "프렌치도어"), ("side_by_side", "사이드바이사이드"), ("top_freezer", "상냉동"),
            ("bottom_freezer", "하냉동"), ("four_door", "4도어"), ("built_in_panel", "빌트인/패널형"))),
        _g("capacity_l", "총용량", "Total capacity", unit="L", display_units=["L", "cu ft"], bucketed=True,
           values=_capacity_buckets([400, 500, 600, 700])),
        _g("width_class", "폭(width)", "Width", unit="in", display_units=["in", "mm"], bucketed=True,
           values=_width_buckets([("w24", "24in급 (~27in, 686mm 미만)", None, 27), ("w30", "30in급 (27~32in)", 27, 32),
                                  ("w33", "33in급 (32~35in)", 32, 35), ("w36", "36in급 (35~38in, 914mm급)", 35, 38),
                                  ("w_other", "그 외 (38in~)", 38, None)])),
        _ENERGY, _KR_GRADE, _EU_CLASS,
        _g("dispenser", "제빙·정수", "Ice & water", level="spec", values=_vals(
            ("ice_maker", "제빙기"), ("water_dispenser", "정수(디스펜서)"), ("none", "없음"))),
        _SMART,
        _g("finish", "마감색", "Finish", values=_vals(
            ("stainless", "스테인리스"), ("black_stainless", "블랙 스테인리스"), ("white", "화이트"), ("black", "블랙"),
            ("slate", "슬레이트"), ("panel_ready", "패널형"))),
    ],
    "washer:*": [
        _g("machine_type", "유형", "Machine type", major_only=True, values=_vals(
            ("top_load", "전자동/탑로더"), ("front_load", "드럼"), ("dryer", "건조기"), ("laundry_center", "트윈/스택·워시타워"))),
        _g("capacity_kg", "용량(kg)", "Capacity", level="spec", unit="kg", display_units=["kg"], bucketed=True,
           values=_plain_buckets([9, 15, 21], "kg")),
        _g("spin_rpm", "탈수 속도", "Spin speed", level="spec", unit="rpm", bucketed=True,
           values=_plain_buckets([1000, 1200, 1400], "rpm")),
        _g("steam", "스팀", "Steam", type_="boolean", values=_vals(("true", "스팀 있음"))),
        _SMART, _ENERGY, _KR_GRADE, _EU_CLASS,
    ],
    "cooking:*": [
        _g("cook_type", "유형", "Appliance type", major_only=True, values=_vals(
            ("microwave", "전자레인지"), ("sco", "SCO (스피드쿡 오븐)"), ("otr", "OTR 오버더레인지"), ("gas_oven", "가스오븐"),
            ("electric_oven", "전기오븐"), ("induction", "인덕션"), ("radiant", "라디언트"))),
        _g("fuel", "열원", "Fuel", values=_vals(("gas", "가스"), ("electric", "전기"), ("induction", "인덕션"),
                                               ("dual_fuel", "듀얼퓨얼"))),
        _g("width_class", "폭(width)", "Width", unit="in", display_units=["in", "mm"], bucketed=True,
           values=_width_buckets([("w24", "24in급 (~27in)", None, 27), ("w30", "30in급 (27~33in)", 27, 33),
                                  ("w36", "36in급 (33~42in)", 33, 42), ("w48", "48in급 (42~54in)", 42, 54),
                                  ("w_other", "그 외 (54in~)", 54, None)])),
        _g("oven_capacity", "오븐 용량", "Oven capacity", level="spec", unit="cu ft", display_units=["cu ft", "L"],
           bucketed=True, values=_plain_buckets([4.0, 5.0, 6.0], " cu ft")),
        _g("burners", "버너/화구 수", "Burners", values=_vals(("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6_plus", "6+"))),
        _g("convection", "컨벡션/에어프라이", "Convection / air fry", values=_vals(
            ("convection", "컨벡션"), ("air_fry", "에어프라이"))),
        _SMART,
    ],
    # sub-group overrides (replace a group of the same key) and additions
    "sco": [
        _g("oven_capacity", "오븐 용량", "Oven capacity", level="spec", unit="cu ft", display_units=["cu ft", "L"],
           bucketed=True, values=_plain_buckets([1.0, 1.5, 2.0], " cu ft")),
        _g("microwave_power", "마이크로웨이브 출력", "Microwave power", level="spec", unit="W", bucketed=True,
           values=_plain_buckets([900, 1000, 1100], " W")),
    ],
    "compact": [
        _g("capacity_l", "총용량", "Total capacity", unit="L", display_units=["L", "cu ft"], bucketed=True,
           values=_capacity_buckets([100, 200])),
    ],
    "built_in": [
        _g("install_type", "설치 형태", "Install type", level="spec", values=_vals(
            ("fully_integrated", "풀 인테그레이티드"), ("panel_ready", "패널 레디"), ("column", "컬럼형"))),
    ],
}
FILTER_EXCLUDE: dict[str, set[str]] = {  # groups that make no sense for a sub group
    "microwave": {"burners", "fuel", "oven_capacity", "width_class"},
    "otr": {"burners", "oven_capacity"},
    "induction": {"oven_capacity", "fuel"},
    "radiant": {"oven_capacity", "fuel"},
    "sco": {"burners", "fuel"},
    "dryer": {"spin_rpm"},
}
_REGION_KEYS = list(catalog.REGIONS)


def _defs_for(sub: str) -> dict[str, dict]:
    """key -> definition for a sub group (common + major + sub overrides - excludes), in display order."""
    major = catalog.major_of(sub)
    defs: dict[str, dict] = {}
    for g in FILTER_SCHEMA["common"] + FILTER_SCHEMA.get(f"{major}:*", []) + FILTER_SCHEMA.get(sub, []):
        defs[g["key"]] = g
    for k in FILTER_EXCLUDE.get(sub, ()):
        defs.pop(k, None)
    return defs


def groups_for(selector: str, regions: Optional[list[str]] = None) -> list[dict]:
    """Filter groups for a sub key (door/machine/cook type hidden: the sub already says it) or a major key (all of
    the major's groups, type groups shown). `regions` drops groups restricted to other regions. Plain dicts."""
    if catalog.is_major(selector):
        defs = {g["key"]: g for g in FILTER_SCHEMA["common"] + FILTER_SCHEMA.get(f"{selector}:*", [])}
        major_only = True
    elif catalog.is_sub(selector):
        defs = _defs_for(selector)
        major_only = False
    else:
        raise ValueError(f"unknown product group {selector!r}")
    out = []
    for g in defs.values():
        if g.get("major_only") and not major_only:
            continue
        if regions is not None and "*" not in g["regions"] and not set(g["regions"]) & set(regions):
            continue
        out.append({k: v for k, v in g.items() if k not in ("major_only", "bucketed")} | {"bucketed": bool(g.get("bucketed"))})
    return out


_ALL_DEFS: dict[str, list[dict]] = {}
for _gs in FILTER_SCHEMA.values():
    for _g_ in _gs:
        _ALL_DEFS.setdefault(_g_["key"], []).append(_g_)
_ALL_DEFS["keyword"] = [{"key": "keyword", "type": "text", "values": None}]
_ALL_DEFS["region"] = [{"key": "region", "type": "multi", "values": _vals(*[(k, k) for k in _REGION_KEYS])}]


# ------------------------------------------------------------------ facts
_NUM = r"(\d{1,2}(?:\.\d+)?)"
_CUFT_RE = re.compile(_NUM + r"\s*(?:cu\.?\s*ft\.?|cubic\s*f(?:ee|oo)t)", re.I)
_L_RE = re.compile(r"(?<![A-Za-z0-9])(\d{3,4})\s*(?:L|리터)(?![A-Za-z])")
_WIDTH_RE = re.compile(r"(?<![\d.])(\d{2}(?:\.\d)?)\s*(?:-?\s*inch(?:es)?\b|\"|in\b)", re.I)
_BURNER_RE = re.compile(r"(\d)\s*-?\s*burner", re.I)
_WATT_RE = re.compile(r"(?<![\d,])(\d,?\d{3}|\d{3})\s*(?:watts?|w)\b", re.I)

_SUB_DOOR = {"french_door": "french_door", "side_by_side": "side_by_side", "top_freezer": "top_freezer",
             "bottom_freezer": "bottom_freezer", "built_in": "built_in_panel"}
_SUB_FUEL = {"gas_oven": "gas", "electric_oven": "electric", "induction": "induction", "radiant": "electric"}
_FINISH = (("black_stainless", r"black\s+stainless"), ("stainless", r"stainless"), ("panel_ready", r"panel[\s-]?ready"),
           ("slate", r"slate"), ("white", r"\bwhite\b"), ("black", r"\bblack\b"))
_DOORS = (("four_door", r"\b(?:4|four)[\s-]*door"), ("french_door", r"french[\s-]*door"),
          ("side_by_side", r"side[\s-]*by[\s-]*side"), ("top_freezer", r"top[\s-]*(?:freezer|mount)"),
          ("bottom_freezer", r"bottom[\s-]*(?:freezer|mount)"))


def finish_word(text: str) -> Optional[str]:
    t = (text or "").casefold()
    return next((k for k, pat in _FINISH if re.search(pat, t)), None)


def name_facts(name: str) -> dict[str, Any]:
    """Facts derivable from a listing name; only keys that are actually found (absence = unknown)."""
    n = name or ""
    low = n.casefold()
    f: dict[str, Any] = {}
    m = _CUFT_RE.search(n)
    if m:
        f["capacity_total_cuft"] = float(m.group(1))
    else:
        m = _L_RE.search(n)
        if m:
            f["capacity_total_cuft"] = round(units.l_to_cuft(float(m.group(1))), 1)
    m = _WIDTH_RE.search(n)
    if m and 18 <= float(m.group(1)) <= 60:
        f["width_in"] = float(m.group(1))
    if "energy star" in low:
        f["energy_star"] = True
    if re.search(r"smart|wi-?fi|family hub|thinq|smartthings", low):
        f["wifi"] = True
    if "family hub" in low:
        f["screen"] = True
    fin = finish_word(n)
    if fin:
        f["finish"] = fin
    door = next((k for k, pat in _DOORS if re.search(pat, low)), None)
    if door:
        f["door_type"] = door
    if re.search(r"ice\s*(?:and|&)\s*water|ice\s*maker", low):
        f["ice_maker"] = True
    if re.search(r"water\s*dispenser|ice\s*(?:and|&)\s*water", low):
        f["water_dispenser"] = True
    if "dual fuel" in low:
        f["fuel"] = "dual_fuel"
    elif re.search(r"\binduction\b", low):
        f["fuel"] = "induction"
    elif re.search(r"\bgas\b", low):
        f["fuel"] = "gas"
    elif re.search(r"\belectric\b", low):
        f["fuel"] = "electric"
    m = _BURNER_RE.search(n)
    if m:
        f["burners"] = int(m.group(1))
    m = _WATT_RE.search(n)
    if m:
        f["microwave_watts"] = int(m.group(1).replace(",", ""))
    if "convection" in low:
        f["convection"] = True
    if re.search(r"air[\s-]*fr(?:y|ier)", low):
        f["air_fry"] = True
    if re.search(r"\bsteam\b", low):
        f["steam"] = True
    return f


def enrich(cand: Candidate) -> Candidate:
    """Copy of the candidate with name-derived facts added to attrs (attrs_src='name'); listing attrs win."""
    attrs, src = dict(cand.attrs), dict(cand.attrs_src)
    for k, v in name_facts(cand.name).items():
        if k not in attrs:
            attrs[k], src[k] = v, "name"
    return cand.model_copy(update={"attrs": attrs, "attrs_src": src})


def _product_facts(p) -> dict[str, Any]:
    pairs = {"capacity_total_cuft": p.capacity_total_cuft, "width_in": p.width_in, "energy_kwh_year": p.energy_kwh_year,
             "energy_star": p.energy_star, "ice_maker": p.ice_maker, "water_dispenser": p.water_dispenser,
             "wifi": p.wifi_supported, "finish": finish_word(p.finish_color or "")}
    return {k: v for k, v in pairs.items() if v is not None}


def facts(cand: Candidate, product=None) -> dict[str, Any]:
    """Merged facts: sub key < name < candidate attrs < collected product (later wins)."""
    f: dict[str, Any] = {}
    sub = cand.subcategory
    if sub in _SUB_DOOR:
        f["door_type"] = _SUB_DOOR[sub]
    if sub in _SUB_FUEL:
        f["fuel"] = _SUB_FUEL[sub]
    if sub and catalog.major_of(sub) == "washer":
        f["machine_type"] = sub
    if sub and catalog.major_of(sub) == "cooking":
        f["cook_type"] = sub
    f.update(name_facts(cand.name))
    f.update({k: v for k, v in cand.attrs.items() if v is not None})
    if product is not None:
        f.update(_product_facts(product))
    return f


# ------------------------------------------------------------------ value extraction
def _price(c: Candidate) -> Optional[float]:
    return c.price_usd if c.price_usd is not None else c.price_local


def _flags(f: dict, mapping: dict[str, str], false_known: tuple[str, ...] = ()) -> Optional[set[str]]:
    got = {out for src, out in mapping.items() if f.get(src) is True}
    if got:
        return got
    return set() if any(f.get(k) is False for k in (false_known or tuple(mapping))) else None


def _energy(f: dict) -> Optional[set[str]]:
    got: set[str] = set()
    known = False
    if "energy_star" in f:
        known = True
        if f["energy_star"]:
            got.add("energy_star")
    kwh = f.get("energy_kwh_year")
    if kwh is not None:
        known = True
        got.add("kwh_lt350" if kwh < 350 else "kwh_350_450" if kwh < 450 else "kwh_450_plus")
    return got if known else None


def _dispenser(f: dict) -> Optional[set[str]]:
    got = _flags(f, {"ice_maker": "ice_maker", "water_dispenser": "water_dispenser"})
    return {"none"} if got == set() else got


def _burners(f: dict) -> Optional[set[str]]:
    n = f.get("burners")
    return None if n is None else {"6_plus" if n >= 6 else str(int(n))}


def _single(key: str) -> Callable[[dict], Optional[set[str]]]:
    return lambda f: None if f.get(key) is None else {str(f[key])}


_NUMERIC_INPUT: dict[str, Callable[[dict], Optional[float]]] = {
    "capacity_l": lambda f: units.cuft_to_l(f["capacity_total_cuft"]) if f.get("capacity_total_cuft") is not None else None,
    "width_class": lambda f: f.get("width_in"),
    "capacity_kg": lambda f: f.get("capacity_kg"),
    "spin_rpm": lambda f: f.get("spin_rpm"),
    "oven_capacity": lambda f: f.get("oven_capacity_cuft"),
    "microwave_power": lambda f: f.get("microwave_watts"),
}
_SET_EXTRACT: dict[str, Callable[[dict], Optional[set[str]]]] = {
    "door_type": _single("door_type"), "machine_type": _single("machine_type"), "cook_type": _single("cook_type"),
    "fuel": _single("fuel"), "finish": _single("finish"), "install_type": _single("install_type"),
    "kr_grade": lambda f: None if f.get("kr_grade") is None else {f"grade_{f['kr_grade']}"},
    "eu_class": lambda f: None if f.get("eu_class") is None else {f"class_{str(f['eu_class']).lower()}"},
    "smart": lambda f: _flags(f, {"wifi": "wifi", "app_control": "app_control", "screen": "screen"}, ("wifi",)),
    "energy": _energy, "dispenser": _dispenser, "burners": _burners,
    "convection": lambda f: _flags(f, {"convection": "convection", "air_fry": "air_fry"}),
    "steam": lambda f: None if "steam" not in f else ({"true"} if f["steam"] else set()),
}


def _bucket_key(x: float, values: list[dict]) -> Optional[str]:
    for v in values:
        if (v["min"] is None or x >= v["min"]) and (v["max"] is None or x < v["max"]):
            return v["key"]
    return None


def _values_def(key: str, sub: Optional[str]) -> Optional[dict]:
    if sub and (catalog.is_sub(sub)):
        d = _defs_for(sub).get(key)
        if d:
            return d
    return _ALL_DEFS[key][0] if key in _ALL_DEFS else None


def _values_of(key: str, cand: Candidate, f: dict) -> Optional[set[str]]:
    """Value keys of the candidate for a group, or None when unknown. (price/keyword are handled separately.)"""
    if key == "brand":
        return {cand.brand}
    if key == "region":
        return {cand.region}
    if key in _NUMERIC_INPUT:
        x = _NUMERIC_INPUT[key](f)
        d = _values_def(key, cand.subcategory)
        if x is None or d is None:
            return None
        b = _bucket_key(x, d["values"])
        return {b} if b else None
    return _SET_EXTRACT[key](f) if key in _SET_EXTRACT else None


# ------------------------------------------------------------------ validation
def _check_shape(selections: Optional[dict]) -> dict:
    """Validate selection structure without any product-group context. Returns the (same) dict."""
    sel = selections or {}
    if not isinstance(sel, dict):
        raise ValueError("filters must be an object")
    if len(sel) > MAX_GROUPS:
        raise ValueError(f"too many filter groups (max {MAX_GROUPS})")
    for key, val in sel.items():
        if not isinstance(key, str) or key not in _ALL_DEFS:
            raise ValueError(f"unknown filter {str(key)[:40]!r}")
        defs = _ALL_DEFS[key]
        typ = defs[0]["type"]
        if typ == "text":
            if not isinstance(val, str) or len(val) > MAX_STR:
                raise ValueError(f"{key}: text up to {MAX_STR} characters expected")
        elif typ == "boolean":
            if not isinstance(val, bool):
                raise ValueError(f"{key}: boolean expected")
        elif typ == "range":
            if not isinstance(val, dict) or set(val) - {"min", "max"}:
                raise ValueError(f"{key}: {{min, max}} expected")
            nums = []
            for k, x in val.items():
                if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= MAX_NUM:
                    raise ValueError(f"{key}.{k}: number between 0 and {MAX_NUM:g} expected")
                nums.append(x)
            if len(nums) == 2 and val["min"] > val["max"]:
                raise ValueError(f"{key}: min must be <= max")
        else:
            if not isinstance(val, list) or len(val) > MAX_VALUES:
                raise ValueError(f"{key}: list of up to {MAX_VALUES} values expected")
            valid = None if any(d["values"] is None for d in defs) else {v["key"] for d in defs for v in d["values"]}
            for x in val:
                if not isinstance(x, str) or len(x) > MAX_STR:
                    raise ValueError(f"{key}: values must be strings up to {MAX_STR} characters")
                if valid is not None and x != UNKNOWN and x not in valid:
                    raise ValueError(f"{key}: unknown value {x[:40]!r}")
    return sel


def validate_selections(selections: Optional[dict], subs: list[str], regions: Optional[list[str]] = None) -> dict:
    """Shape check plus: every group must be offered for at least one of the searched sub groups / regions."""
    sel = _check_shape(selections)
    allowed = set(HIDDEN_KEYS)
    for sub in subs:
        allowed |= {g["key"] for g in groups_for(catalog.major_of(sub) or sub, regions)}
        allowed |= {g["key"] for g in groups_for(sub, regions)}
    for key in sel:
        if key not in allowed:
            raise ValueError(f"filter {key!r} is not available for the selected product groups/regions")
    return sel


# ------------------------------------------------------------------ filtering
def _inc(include_unknown, key: str, val) -> bool:
    if isinstance(include_unknown, dict):
        base = bool(include_unknown.get(key))
    else:
        base = bool(include_unknown)
    return base or (isinstance(val, list) and UNKNOWN in val)


def _match(key: str, val, cand: Candidate, f: dict, inc_unknown) -> bool:
    if key == "keyword":
        q = val.strip().casefold()
        return not q or q in cand.name.casefold() or q in cand.model_number.casefold()
    if key == "price":
        p = _price(cand)
        if p is None:
            return _inc(inc_unknown, key, val)
        return val.get("min", 0) <= p <= val.get("max", MAX_NUM)
    if key not in _ALL_DEFS or (_ALL_DEFS[key][0]["type"] == "boolean" and val is False):
        return True
    got = _values_of(key, cand, f)
    if got is None:
        return _inc(inc_unknown, key, val)
    wanted = {"true"} if val is True else {x for x in val if x != UNKNOWN}
    return bool(got & wanted)


def _active(selections: dict) -> dict:
    """Drop no-op selections (False booleans, empty value lists without UNKNOWN, empty keyword)."""
    out = {}
    for k, v in selections.items():
        if v is False or v == [] or (k == "keyword" and not v.strip()) or (k == "price" and not v):
            continue
        out[k] = v
    return out


def filter_candidates(cands: list[Candidate], selections: Optional[dict], *, include_unknown=False,
                      products: Optional[dict] = None) -> list[Candidate]:
    """Candidates matching every selected group (input order kept). Raises ValueError on a malformed selection."""
    sel = _active(_check_shape(selections))
    if not sel:
        return list(cands)
    out = []
    for c in cands:
        f = facts(c, (products or {}).get(c.url))
        if all(_match(k, v, c, f, include_unknown) for k, v in sel.items()):
            out.append(c)
    return out


def facet_counts(cands: list[Candidate], selections: Optional[dict], group_keys: list[str], *,
                 products: Optional[dict] = None, include_unknown=False) -> dict[str, dict[str, int]]:
    """Disjunctive counts: per group, value counts over the candidates matching every OTHER selection.
    Bucket/multi/boolean groups list each value (0 included) plus UNKNOWN; range groups only UNKNOWN; text groups skipped."""
    sel = _active(_check_shape(selections))
    facts_by_url = {c.url: facts(c, (products or {}).get(c.url)) for c in cands}
    out: dict[str, dict[str, int]] = {}
    for key in group_keys:
        if key not in _ALL_DEFS or _ALL_DEFS[key][0]["type"] == "text":
            continue
        others = {k: v for k, v in sel.items() if k != key}
        pool = [c for c in cands if all(_match(k, v, c, facts_by_url[c.url], include_unknown) for k, v in others.items())]
        typ = _ALL_DEFS[key][0]["type"]
        counts: dict[str, int] = {}
        if key == "brand":
            counts = {b: 0 for b in dict.fromkeys(c.brand for c in cands)}
        elif key == "region":
            counts = {r: 0 for r in dict.fromkeys(c.region for c in cands)}
        elif typ != "range":
            for sub in dict.fromkeys(c.subcategory for c in cands):
                d = _values_def(key, sub)
                for v in (d["values"] if d else []):
                    counts.setdefault(v["key"], 0)
        unknown = 0
        for c in pool:
            if typ == "range":
                unknown += _price(c) is None
                continue
            got = _values_of(key, c, facts_by_url[c.url])
            if got is None:
                unknown += 1
                continue
            for v in got:
                counts[v] = counts.get(v, 0) + 1
        counts[UNKNOWN] = unknown
        out[key] = counts
    return out
