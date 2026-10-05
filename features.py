"""Feature flags derived from the FULL spec table.

Adapters only fill a few curated extra_specs keys (e.g. 'Air fry' from one row), so a feature that is only
mentioned inside a multi-value row ('Oven Cooking Modes: ... | No Preheat Air Fry') used to show as missing.
derive_flags scans every extra_specs / RawSpec key and value and reports 'Yes (<evidence>)'. A 'No' is only
reported when a spec row whose *name* is the feature says so explicitly; no evidence -> the label is absent."""
import re
from typing import Iterable, Optional

import catalog

_B0, _B1 = r"(?<![A-Za-z])", r"(?![A-Za-z])"


def _p(expr: str) -> re.Pattern:
    return re.compile(f"{_B0}(?:{expr}){_B1}", re.IGNORECASE)


_WIFI = r"wi-?fi|remote (?:start|preheat|monitoring|control)|thinq|smartthings|smarthq|home connect|app control"
_SABBATH = r"sabbath|shabbos|shabbat"

# (label, pattern). Labels match the curated adapter keys where one exists ('Air fry', 'Convection', ...).
FEATURES: dict[str, list[tuple[str, re.Pattern]]] = {
    "cooking": [
        ("Air fry", _p(r"no preheat air fry|air[- ]?fry(?:er|ing)?|air cooking")),
        ("Convection", _p(r"true european convection|european convection|convection")),
        ("Self clean", _p(r"self[- ]?clean(?:ing)?|pyrolytic")),
        ("Steam clean", _p(r"steam[- ]?clean(?:ing)?")),
        ("Sabbath mode", _p(_SABBATH)),
        ("Temperature probe", _p(r"(?:temperature |meat |food )?probe")),
        ("Warming drawer", _p(r"warming drawer|warming zone")),
        ("Proof mode", _p(r"(?<!-)proof(?:ing)?(?: mode)?")),
        ("Wi-Fi / remote", _p(_WIFI)),
    ],
    "washer": [
        ("Steam", _p(r"steam")),
        ("Sanitize", _p(r"sanitiz\w*|sanitis\w*")),
        ("Allergen", _p(r"allergen\w*")),
        ("Wi-Fi / smart", _p(_WIFI + r"|smart pairing|smart diagnosis")),
        ("Auto dispense", _p(r"auto(?:matic)?[- ]?(?:dispens\w*|dose|detergent)|autodose|smart dispens\w*")),
        ("Vibration reduction", _p(r"vibration (?:reduction|control)")),
        ("Wrinkle care", _p(r"wrinkle (?:care|release|prevent\w*)")),
        ("Sensor dry", _p(r"sensor dry(?:ing)?")),
        ("Stackable", _p(r"stackab\w+")),
    ],
    "refrigerator": [  # ice maker / dispenser / Wi-Fi already have first-class ProductRecord fields
        ("Sabbath mode", _p(_SABBATH)),
        ("Door-in-door", _p(r"door[- ]in[- ]door|instaview")),
        ("Dual evaporator", _p(r"dual evaporator|twin cooling|dual cooling")),
        ("Convertible drawer", _p(r"flexzone|convertible (?:drawer|zone)|temperature[- ]controlled drawer|custom ?temp\w*")),
        ("Air filter", _p(r"air filter\w*|fresh air filter")),
        ("Fingerprint resistant", _p(r"fingerprint[- ]resistant|smudge[- ]proof")),
    ],
}
_NEGATIVE = re.compile(r"^\s*(?:no|none|n/?a|false|not (?:available|included|applicable)|without|0|[-–—])\s*$", re.IGNORECASE)
_POSITIVE_KEPT = re.compile(r"^\s*yes\b", re.IGNORECASE)


def _rows(extra_specs: dict, raw_specs: Iterable) -> list[tuple[str, str]]:
    rows = [(str(k), str(v)) for k, v in (extra_specs or {}).items()]
    for r in raw_specs or ():
        rows.append((str(getattr(r, "key", "") or ""), str(getattr(r, "value", "") or "")))
    return rows


def derive_flags(category: Optional[str], extra_specs: dict, raw_specs: Iterable = ()) -> dict[str, str]:
    """{label: 'Yes (<evidence>)' | 'No'} for the category's features, from every spec key/value.
    Evidence is the matched phrase of a value, or the (section-stripped) row name for 'Name: Yes' style rows."""
    defs = FEATURES.get(catalog.normalize_major(category) or "", [])
    rows = _rows(extra_specs, raw_specs)
    out: dict[str, str] = {}
    for label, pat in defs:
        yes, no = None, False
        for key, value in rows:
            name = key.rsplit(" > ", 1)[-1].strip()
            m_val, m_key = pat.search(value), pat.search(name)
            if m_val:
                yes = yes or m_val.group(0)
            elif m_key and value.strip():
                if _NEGATIVE.match(value):
                    no = True
                else:
                    yes = yes or m_key.group(0)
        if yes:
            out[label] = f"Yes ({' '.join(yes.split())})"
        elif no:
            out[label] = "No"
    return out


def apply_flags(product, raw_specs: Iterable = ()) -> None:
    """Merge derive_flags into product.extra_specs: evidence overwrites a missing / '–' / 'No' curated value
    (an existing 'Yes...' is kept); an explicit 'No' only fills a gap."""
    flags = derive_flags(product.category, product.extra_specs, raw_specs)
    for label, value in flags.items():
        cur = product.extra_specs.get(label)
        if value.startswith("Yes"):
            if not (cur and _POSITIVE_KEPT.match(cur)):
                product.extra_specs[label] = value
        elif not cur:
            product.extra_specs[label] = value


def dedupe_specs(extra_specs: dict) -> dict:
    """View for the 'All specifications' table: drop a flat label whose value equals the value of a
    'Section > Label' key (adapters keep old flat labels next to the new sectioned rows). Sectioned keys win.
    Bare yes/no values only count as duplicates when the labels match too (else unrelated flags would vanish)."""
    norm = lambda v: " ".join(str(v).lower().split())  # noqa: E731
    sect = [(k.rsplit(" > ", 1)[-1].strip().lower(), norm(v)) for k, v in extra_specs.items() if " > " in k]
    values, labels = {v for _, v in sect}, set(sect)

    def dup(k, v):
        n = norm(v)
        if n in ("yes", "no", "true", "false"):
            return (k.strip().lower(), n) in labels
        return n in values
    return {k: v for k, v in extra_specs.items() if " > " in k or not dup(k, v)}


FLAG_KO = {
    "Air fry": "에어프라이", "Convection": "컨벡션", "Self clean": "자가 세척", "Steam clean": "스팀 세척",
    "Sabbath mode": "안식일 모드", "Temperature probe": "온도 프로브", "Warming drawer": "워밍 서랍",
    "Proof mode": "발효 모드", "Wi-Fi / remote": "Wi-Fi·원격 제어", "Steam": "스팀", "Sanitize": "살균",
    "Allergen": "알레르겐 케어", "Wi-Fi / smart": "Wi-Fi·스마트", "Auto dispense": "세제 자동 투입",
    "Vibration reduction": "진동 저감", "Wrinkle care": "구김 방지", "Sensor dry": "센서 건조", "Stackable": "스택 가능",
    "Door-in-door": "도어인도어", "Dual evaporator": "듀얼 증발기", "Convertible drawer": "변환 서랍",
    "Air filter": "공기 청정 필터", "Fingerprint resistant": "지문 방지",
}


def flag_labels(category: Optional[str]) -> list[str]:
    return [label for label, _ in FEATURES.get(catalog.normalize_major(category) or "", [])]
