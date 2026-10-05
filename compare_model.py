"""One comparison model shared by the Excel writer and the web API: products are COLUMNS, canonical attributes are ROWS.

build_compare(...) -> {major: [row, ...]} where each row is
    {id, section, group, key_en, key_ko, unit, values:[one per product], kind:'value'|'flag', differs, same, notes:[...],
     sources:[[{label, value, method, score, via?}, ...] per product], core, uncertain, method}
A row is one CANONICAL attribute (canon.py): 'Size' / 'Dimensions' / 'Overall dimensions' / '크기' are the same row, a composite
'W x H x D' string is split into width / height / depth rows, 'Convection Bake' / 'Convect Bake' / '컨벡션 베이크' share a row.
Every product keeps its ORIGINAL wording in `sources` (and in `notes` for check-mark cells). Sections are the fixed taxonomy
`SECTIONS` (기본정보 / 치수·무게 / 용량 / ... / 액세서리·옵션); `core` rows are the default view, the rest is long-tail. Rows no
product has a value for are dropped; a product that reaches one row through several keys (flat + sectioned duplicates) shows the most
specific value and lists all of them in `sources`.
Accessories / consumables (part numbers, 'Optional ...', kits) are routed to the last section ACCESSORY (never core) and stay in the
audit; a feature only an optional accessory provides keeps its row but is noted '옵션(액세서리)'. Items exploded from a list value keep
their parent group (`group` / `group_ko`, `child`: learned item) and sit contiguously in the long tail; countable facts are derived
from item lists ('1 X Rack | 2 Y Racks' -> Oven racks (count) = 3, method 'derived'). build_compare() returns a dict subclass that
carries `.canon_stats` ({major: Canonicalizer.run_stats()}) so a silent degrade of the embedding stage is visible."""
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import canon as canon_mod
import catalog
import features
from canon import Canon
from schema import DocumentRecord, ModeRecord, ProductRecord, RawSpec

BASIC = "기본정보"
ACCESSORY = "액세서리·옵션"
SECTIONS = canon_mod.SECTIONS + (ACCESSORY,)
ITEM_SEP = " | "
KEY_SEP = " › "
HEADER_IDS = ("image", "brand", "model", "name", "price")  # shown in the sheet's frozen header block
MAX_SOURCES = 6

# ProductRecord field -> canonical id per major (None = same for every major)
_FIELD_IDS = [
    ("capacity_total_cuft", {"refrigerator": "capacity-total", "washer": "washer-capacity", "cooking": "oven-capacity"}),
    ("capacity_fridge_cuft", "capacity-fridge"), ("capacity_freezer_cuft", "capacity-freezer"),
    ("width_in", "width"), ("height_in", "height"), ("depth_in", "depth"), ("weight_lb", "weight"),
    ("door_style", "door-type"), ("finish_color", "finish-color"), ("voltage_v", "voltage"), ("amps", "amps"),
    ("frequency_hz", "frequency"), ("energy_kwh_year", "energy-annual"),
    ("fridge_temp_range_f", "temperature-range-fridge"), ("freezer_temp_range_f", "temperature-range-freezer"),
]
_FLAG_FIELDS = [("energy_star", "energy-star"), ("ice_maker", "ice-maker"), ("water_dispenser", "water-dispenser"), ("wifi_supported", "wifi")]
_FIELD_LABEL = {"capacity_total_cuft": "Total capacity", "capacity_fridge_cuft": "Fridge capacity", "capacity_freezer_cuft": "Freezer capacity",
                "width_in": "Width", "height_in": "Height", "depth_in": "Depth", "weight_lb": "Weight", "door_style": "Door style",
                "finish_color": "Finish", "voltage_v": "Voltage", "amps": "Current", "frequency_hz": "Frequency",
                "energy_kwh_year": "Power consumption", "fridge_temp_range_f": "Fridge setpoint range", "freezer_temp_range_f": "Freezer setpoint range",
                "energy_star": "ENERGY STAR", "ice_maker": "Ice maker", "water_dispenser": "Water dispenser", "wifi_supported": "Wi-Fi"}

_COMP_NAMES = {"dimensions": ("width", "height", "depth"), "cutout-dimensions": ("width", "height", "depth"),
               "interior-dimensions": ("width", "height", "depth")}
_QTY = re.compile(r"^\s*\d+\s*[x×]?\s+(?=\D)")                      # leading quantity of a list item: '2 Heavy-Duty Roller Racks'
_RACK_ITEM = re.compile(r"^\s*(\d+)\s+(?!.*\b(?:position|guide)s?\b)[^|]*?\bracks?\b\s*(?:\([^)]*\))?\s*$", re.I)  # 'N <something> Rack(s)'
_PAREN_UNIT = re.compile(r"\s*\(([^()]{1,12})\)\s*$")
_CAV_PAIR = re.compile(r"(?P<n>\d[\d,.]*)\s*(?P<u>[a-z.\"]*)\s*(?P<q>upper|lower|top|bottom)\b", re.I)
_SYNONYMS = {"airfryer": "airfry", "airfrying": "airfry", "wifi": "wifi", "wifienabled": "wifi"}


def item_norm(text: str) -> str:
    """Cheap merge key for item texts: lowercase, punctuation/space dropped, plural 's' stripped, small synonym table."""
    t = re.sub(r"[^a-z0-9]+", "", str(text).lower())
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return _SYNONYMS.get(t, t)


def _split(value: Optional[str]) -> list[str]:
    return [x.strip() for x in str(value or "").split(ITEM_SEP) if x.strip()]


_BARE_NUM = re.compile(r"^\d[\d.\s]*[a-z\"°/]{0,5}$", re.I)


def _commas(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [x.strip() for x in parts + [cur] if x.strip()]


def list_items(value: Optional[str]) -> Optional[list[str]]:
    """Items of a list-valued spec cell, else None. ' | ' always splits; a comma list qualifies only when it has
    >= 2 short word-like parts (not numbers/units, not yes/no, not a sentence)."""
    v = str(value or "").strip()
    if not v:
        return None
    if ITEM_SEP in v:
        return _split(v)
    if "," not in v or v.endswith("."):
        return None
    parts = _commas(v)
    if len(parts) < 2 or any(len(x.split()) > 5 or _BARE_NUM.match(x) for x in parts):
        return None
    if all(x.lower() in ("yes", "no", "true", "false") for x in parts):
        return None
    return parts


def _differs(values: list, kind: str) -> bool:
    if len(values) < 2:
        return False
    norm = [bool(v) for v in values] if kind == "flag" else [None if v is None else re.sub(r"\s+", " ", str(v)).strip().lower() for v in values]
    return len(set(norm)) > 1


def group_products(products: Iterable[ProductRecord]) -> dict[str, list[ProductRecord]]:
    import pod
    out: dict[str, list[ProductRecord]] = {}
    for p in products:
        out.setdefault(pod.major_of_product(p), []).append(p)
    return {m: out[m] for m in catalog.major_keys() if m in out} | {m: v for m, v in out.items() if m not in catalog.major_keys()}


# ------------------------------------------------------------------------------------------ contributions -> rows
@dataclass
class _Contrib:
    label: str                       # the source wording (full key, 'Section > Label', or the list item)
    value: str                       # the source value as scraped
    method: str = "seed"
    score: float = 1.0
    shown: object = None             # numeric (converted to the canonical unit) / text / bool for flags
    flag: Optional[bool] = None
    note: Optional[str] = None
    rank: tuple = ()
    via: str = ""                    # list item: the spec key that held the list
    acc: bool = False                # evidence comes from an optional accessory (not built in)


@dataclass
class _Cell:
    canon: Canon
    per: list = field(default_factory=list)   # per product: list[_Contrib]
    group: str = ""
    group_ko: str = ""
    route: str = ""                  # section override (accessories); dropped as soon as a non-routed contribution arrives


class _Cells:
    def __init__(self, n: int):
        self.n, self.cells = n, {}

    def add(self, c: Canon, pi: int, contrib: _Contrib, group: str = "", group_ko: str = "", route: str = "") -> None:
        cell = self.cells.get(c.id)
        if cell is None:
            cell = self.cells[c.id] = _Cell(c, [[] for _ in range(self.n)], group, group_ko, route)
        else:
            if c.label_ko and not cell.canon.label_ko:
                cell.canon = c
            if cell.route != route:
                cell.route = ""
        cell.per[pi].append(contrib)


def _row(section, group, key_en, key_ko, unit, values, kind="value", notes=None, rid=None, core=False, sources=None, uncertain=False, method="seed",
         group_ko="", child=False) -> dict:
    return {"id": rid or f"{section}:{group}:{key_en}", "section": section, "group": group or "", "group_ko": group_ko or group or "",
            "child": child, "key_en": key_en,
            "key_ko": key_ko or key_en, "unit": unit or "", "values": values, "kind": kind,
            "differs": _differs(values, kind), "notes": notes or [None] * len(values),
            "sources": sources or [[] for _ in values], "core": core, "uncertain": uncertain, "method": method,
            "same": len(values) > 1 and not _differs(values, kind) and all(bool(v) if kind == "flag" else v is not None for v in values)}


def _basic_rows(ps, image_ref) -> list[dict]:
    sub = lambda p: catalog.label_ko(p.subcategory) if p.subcategory else ""  # noqa: E731
    mk = lambda en, ko, unit, vals, rid: _row(BASIC, "", en, ko, unit, vals, rid=rid, core=True)  # noqa: E731
    return [
        mk("Image", "이미지", "", [image_ref(p) for p in ps], "image"), mk("Brand", "브랜드", "", [p.brand for p in ps], "brand"),
        mk("Model", "모델", "", [p.model_number for p in ps], "model"), mk("Name", "제품명", "", [p.product_name for p in ps], "name"),
        mk("Sub-category", "소분류", "", [sub(p) or None for p in ps], "sub"), mk("Price", "가격", "USD", [p.price_usd for p in ps], "price"),
        mk("Product page", "제품 페이지", "", [p.product_url or None for p in ps], "url"),
    ]


def _split_key(key: str) -> tuple[str, str]:
    sec, sep, label = key.partition(" > ")
    return (sec.strip(), label.strip() or key) if sep else ("", key)


def _flag_note(value: str) -> Optional[str]:
    """Evidence text of a truthy flag value: 'Yes (No Preheat Air Fry)' -> 'No Preheat Air Fry'; 'Built-In' -> 'Built-In'."""
    v = str(value or "").strip()
    m = re.match(r"^(?:yes|true)\s*[-:(]\s*(.*?)\)?$", v, re.I)
    if m:
        return m.group(1).strip() or None
    return None if re.match(r"^(?:yes|true|y)$", v, re.I) else (v or None)


def _same_words(a: str, b: str) -> bool:
    lit = lambda x: re.sub(r"[^a-z0-9ㄱ-저]", "", str(x).lower())  # noqa: E731 - literal wording, not the canonical key
    return lit(a) == lit(b)


class _Builder:
    """Collects canonical contributions of every product of one major group, then renders rows."""

    def __init__(self, major: str, ps: list[ProductRecord], cz: canon_mod.Canonicalizer):
        self.major, self.ps, self.cz = major, ps, cz
        self.cells = _Cells(len(ps))

    # --- canon helpers (never raise: a canonicalizer failure must not lose the whole comparison)
    def _attr(self, label: str, value, section: Optional[str]) -> Canon:
        try:
            return self.cz.canonicalize(label, category=self.major, value=value, section=section)
        except Exception as exc:  # noqa: BLE001
            print(f"[compare] canonicalize failed for {label!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return Canon(canon_mod.kebab(label), label, "", "기능", "text", False, "new", 0.0)

    def _item(self, label: str, section: Optional[str] = None) -> Canon:
        try:
            return self.cz.canonicalize_item(label, category=self.major, section=section)
        except Exception as exc:  # noqa: BLE001
            print(f"[compare] canonicalize_item failed for {label!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return Canon(canon_mod.kebab(label), label, "", "기능", "list-item", False, "new", 0.0)

    def _part(self, base_id: str, qual: str, like: Canon) -> Canon:
        got = self.cz.get(f"{base_id}:{qual}" if qual else base_id, category=self.major, method=like.method, score=like.score)
        return got or Canon(base_id, base_id, "", "치수·무게", "numeric", False, like.method, like.score, "in")

    # --- contributions
    def fields(self) -> None:
        for pi, p in enumerate(self.ps):
            for field_name, ids in _FIELD_IDS:
                v = getattr(p, field_name)
                cid = ids if isinstance(ids, str) else ids.get(self.major)
                if v is None or not cid:
                    continue
                c = self.cz.get(cid, category=self.major)
                if not c:
                    continue
                shown = v if isinstance(v, (int, float)) and c.kind == "numeric" else str(v)
                self.cells.add(c, pi, _Contrib(_FIELD_LABEL[field_name] + " (field)", str(v), "seed", 1.0, shown, rank=(3,)))
            for field_name, cid in _FLAG_FIELDS:
                v = getattr(p, field_name)
                c = self.cz.get(cid, category=self.major)
                if v is None or not c:
                    continue
                note = p.wifi_evidence if field_name == "wifi_supported" and v else None
                self.cells.add(c, pi, _Contrib(_FIELD_LABEL[field_name] + " (field)", "Yes" if v else "No", "seed", 1.0, bool(v), bool(v), note, (3,)))

    def _acc_unknown(self, key: str, value) -> bool:
        """An accessory-looking spec row whose label no seed / registry entry knows: it becomes an accessory row, never a new attribute."""
        sec, label = _split_key(key)
        v = str(value).strip() if value not in (None, "") else ""
        return bool(v) and features.is_accessory_spec(sec, label, v) and not self.cz.known(label, category=self.major, section=sec or None)

    def specs(self, specs: list[dict]) -> None:
        self.cz.prime([(_split_key(k)[1], self.major, _split_key(k)[0] or None, "attr") for x in specs for k in x if not self._acc_unknown(k, x[k])])
        for pi, x in enumerate(specs):
            for key, raw in x.items():
                value = str(raw).strip() if raw not in (None, "") else ""
                if not value:
                    continue
                sec, label = _split_key(key)
                if self._acc_unknown(key, value):
                    self._accessory(pi, label, key, value)
                    continue
                c = self._attr(label, value, sec or None)
                self._spec_value(pi, key, label, sec, c, value, features.is_accessory_spec(sec, label, value))

    def _accessory(self, pi: int, text: str, via: str, value: Optional[str] = None, group: str = "", group_ko: str = "") -> None:
        """Accessory / consumable: its own row in the ACCESSORY section (long tail), never a registry attribute."""
        text = re.sub(r"\s+", " ", str(text)).strip()
        self.cz.tally("rule", ("rule", self.major, canon_mod.kebab(text)))
        c = Canon(f"accessory:{canon_mod.kebab(text)}", text, "", ACCESSORY, "list-item", False, "rule", 1.0)
        shown = value if value and value != text else None
        self.cells.add(c, pi, _Contrib(text, value or text, "rule", 1.0, True, True, shown, (0,), via=via, acc=True), group or "Accessories", group_ko or "액세서리", ACCESSORY)

    def _spec_value(self, pi: int, key: str, label: str, sec: str, c: Canon, value: str, acc: bool = False) -> None:
        base_rank = (0, 1 if "(decimal)" in key.lower() else 0, 1 if sec else 0)
        mk = lambda **kw: _Contrib(key, value, c.method, c.score, rank=base_rank + (-len(label),), acc=acc, **kw)  # noqa: E731
        if c.id == "voltage" and re.search(r"hz", value, re.I) and len(canon_mod.parse_vha(value)) > 1:  # '220V / 60Hz'
            for pid, num in canon_mod.parse_vha(value).items():
                pc = self.cz.get(pid, category=self.major, method=c.method, score=c.score)
                if pc:
                    self.cells.add(pc, pi, mk(shown=num))
            return
        base_id, _, qual = c.id.partition(":")
        # composite values: 'W x H x D' -> three rows, 'voltage / Hz / A' -> three rows
        if c.composite:
            parts = (canon_mod.parse_vha(value) if base_id == "electrical-requirements" else canon_mod.parse_dims(value, f"{sec} {label}"))
            if parts:
                names = _COMP_NAMES.get(base_id)
                for i, pid in enumerate(c.composite):
                    nm = (names[i] if names else pid)
                    if nm in parts:
                        self.cells.add(self._part(pid, qual, c), pi, mk(shown=parts[nm]))
                return
        if c.asitems:
            f = canon_mod.flag_of(value)
            if f is not None and not canon_mod.split_items(value)[1:]:
                self.cells.add(Canon(c.id, c.label_en, c.label_ko, c.section, "flag", c.core, c.method, c.score, order=c.order), pi,
                               mk(shown=f, flag=f, note=_flag_note(value) if f else None))
                return
            self._items(pi, key, c, canon_mod.split_items(value), value, label, acc or c.id == "accessories")
            return
        if c.kind == "flag":
            f = canon_mod.flag_of(value)
            if f is None:
                f = True
            targets = [self.cz.get(i, category=self.major, method=c.method, score=c.score) for i in c.also] if c.also else [c]
            for t in targets:
                if t:
                    self.cells.add(t, pi, mk(shown=f, flag=f, note=_flag_note(value) if f else None))
            return
        if c.kind == "numeric":
            pu = _PAREN_UNIT.search(label)
            m = canon_mod.parse_measure(value, pu.group(1) if pu else ("kw" if re.search(r"\bkw\b", label, re.I) else c.unit))
            if m and (c.unit or not m[2]):
                num = (canon_mod.to_unit(m[0], m[1], c.unit) if m[1] != c.unit else m[0]) if c.unit else m[0]
                if num is None and not m[2]:
                    num = m[0]
                if num is not None:
                    self.cells.add(c, pi, mk(shown=int(num) if float(num).is_integer() else num))
                    return
            if c.cav and self._cav_split(pi, key, c, value, mk):
                return
            self.cells.add(c, pi, mk(shown=value))
            return
        # text attribute
        seeded = c.order < 5000 or c.method == "override"  # seed attributes keep text as text; learned ones may be lists
        items = None if seeded else list_items(value)
        if items:
            self._items(pi, key, c, items, value, label, acc)
            return
        self.cells.add(c, pi, mk(shown=value.replace(ITEM_SEP, ", ")))

    def _cav_split(self, pi: int, key: str, c: Canon, value: str, mk) -> bool:
        hits = list(_CAV_PAIR.finditer(value))
        if len(hits) < 2:
            return False
        base_id = c.id.partition(":")[0]
        for m in hits:
            meas = canon_mod.parse_measure(f"{m.group('n')} {m.group('u')}".strip(), c.unit)
            num = canon_mod.to_unit(meas[0], meas[1], c.unit) if meas and meas[1] != c.unit else (meas[0] if meas else None)
            if num is None:
                return False
        for m in hits:
            q = "upper" if m.group("q").lower() in ("upper", "top") else "lower"
            meas = canon_mod.parse_measure(f"{m.group('n')} {m.group('u')}".strip(), c.unit)
            num = canon_mod.to_unit(meas[0], meas[1], c.unit) if meas[1] != c.unit else meas[0]
            self.cells.add(self._part(base_id, q, c), pi, mk(shown=num))
        return True

    def _items(self, pi: int, key: str, parent: Canon, items: list[str], value: str, label: str, acc: bool = False) -> None:
        group, group_ko = parent.label_en, parent.label_ko or parent.label_en
        for it in items:
            if acc or features.is_accessory_item(it):
                self._accessory(pi, it, key, None, group if acc else "", group_ko if acc else "")
                continue
            base = _QTY.sub("", it).strip() or it  # '2 Heavy-Duty Roller Racks' is the canonical 'Heavy-Duty Roller Racks' item
            ic = self._item(base, label)
            note = None if _same_words(it, ic.label_en) else it
            self.cells.add(ic, pi, _Contrib(it, it, ic.method, ic.score, True, True, note, (0,), via=key), group, group_ko)
        self._derive_rack_count(pi, key, parent, items, label)

    def _derive_rack_count(self, pi: int, key: str, parent: Canon, items: list[str], label: str) -> None:
        """'1 Heavy-Duty Offset Oven Rack | 2 Heavy-Duty Roller Racks' -> Oven racks (count) = 3 so ovens compare on one row."""
        if self.major != "cooking" or not re.search(r"rack", f"{parent.label_en} {label}", re.I):
            return
        hits = [(int(m.group(1)), it) for it in items if (m := _RACK_ITEM.match(it))]
        c = self.cz.get("oven-racks-count", category=self.major)
        if not hits or not c:
            return
        total = sum(n for n, _ in hits)
        note = "derived from item list: " + " + ".join(it for _, it in hits)
        self.cells.add(c, pi, _Contrib(f"{label} (derived)", " | ".join(it for _, it in hits), "derived", 1.0, total, note=note, rank=(-1,), via=key))

    def derived_flags(self, specs: list[dict], raws_by_p: list[list[RawSpec]]) -> None:
        for pi, p in enumerate(self.ps):
            derived = features.derive_flags(p.category or self.major, specs[pi], raws_by_p[pi])
            for label in features.flag_labels(self.major):
                d = derived.get(label)
                f = canon_mod.flag_of(d) if d else None
                if f is None:
                    continue
                c = self._item(label)
                note = _flag_note(d) if f else None
                self.cells.add(c, pi, _Contrib(label, d or "", c.method, c.score, f, f, note, (-1,), acc=bool(note and note.startswith(features.ACCESSORY_NOTE))))

    def pod(self, pod_items) -> None:
        import pod
        for r in pod.compare_rows(self.ps, pod_items):
            if features.is_accessory_item(r["en"]):
                for pi, on in enumerate(r["present"]):
                    if on:
                        self._accessory(pi, r["en"], "POD")
                continue
            c = self._item(r["en"], r.get("category"))
            if c.method == "new" and r.get("ko") and not c.label_ko:
                c = Canon(c.id, c.label_en, r["ko"], c.section, c.kind, c.core, c.method, c.score, order=c.order)
            for pi, on in enumerate(r["present"]):
                if not on:
                    continue
                wording = (r.get("wording") or [None] * len(self.ps))[pi] or ""
                note = None if not wording or _same_words(wording, c.label_en) else wording
                self.cells.add(c, pi, _Contrib(wording or r["en"], wording or r["en"], c.method, c.score, True, True, note, (0,), via="POD"))

    def modes(self, modes: list[ModeRecord]) -> None:
        ids = [(p.brand, p.model_number) for p in self.ps]
        for m in modes:
            if (m.brand, m.model_number) not in ids:
                continue
            c = self._item(m.mode_name.strip(), m.category)
            note = " ".join(x for x in (m.setting_range, f"{m.source_doc} p.{m.source_page}" if m.source_page else m.source_doc) if x) or None
            self.cells.add(c, ids.index((m.brand, m.model_number)), _Contrib(m.mode_name.strip(), m.mode_name.strip(), c.method, c.score, True, True, note,
                                                                             (0,), via="Modes"))

    # --- rendering
    def rows(self) -> list[dict]:
        out = []
        for cid, cell in self.cells.cells.items():
            c = cell.canon
            flag = c.kind in ("flag", "list-item")
            values, notes, sources, any_val = [], [], [], False
            for contribs in cell.per:
                if not contribs:
                    values.append(None)
                    notes.append(None)
                    sources.append([])
                    continue
                if flag:
                    on = [x for x in contribs if x.flag]
                    built_in = [x for x in on if not x.acc]
                    note = " · ".join(dict.fromkeys(x.note for x in (built_in or on) if x.note))
                    if on and not built_in and not cell.route:  # only an optional accessory provides it: never claim it as built in
                        note = features.ACCESSORY_NOTE + (f": {note}" if note else "")
                    values.append(bool(on))
                    notes.append(note or None)
                    any_val = any_val or bool(on)
                else:
                    num = [x for x in contribs if isinstance(x.shown, (int, float)) and not isinstance(x.shown, bool)]
                    pick = max(num or contribs, key=lambda x: x.rank)
                    values.append(pick.shown)
                    notes.append(pick.note)
                    any_val = True
                ordered = sorted(contribs, key=lambda x: x.rank, reverse=True)
                seen, srcs = set(), []
                for x in ordered:
                    k = (x.label, x.value)
                    if k in seen:
                        continue
                    seen.add(k)
                    s = {"label": x.label, "value": x.value, "method": x.method, "score": round(x.score, 2)}
                    if x.via:
                        s["via"] = x.via
                    srcs.append(s)
                sources.append(srcs[:MAX_SOURCES])
            if not any_val:
                continue  # nothing to compare (also drops flag rows no product has)
            has_num = any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
            row = _row(cell.route or c.section, cell.group, c.label_en, c.label_ko or c.label_en, c.unit if has_num else "", values,
                       "flag" if flag else "value", notes, cid, c.core and not cell.route, sources,
                       any(canon_mod.needs_review(s["method"], s["score"]) for ss in sources for s in ss), group_ko=cell.group_ko,
                       child=bool(cell.group and flag and c.order >= 5000 and not cell.route))
            row["method"] = _worst_method(sources)
            row["_order"] = c.order
            out.append(row)
        sec_rank = {s: i for i, s in enumerate(SECTIONS)}
        first = {}  # a group's long-tail children stay together, in the order their group first appears
        for r in out:
            if r["group"] and not r["core"]:
                k = (r["section"], r["group"])
                first[k] = min(first.get(k, r["_order"]), r["_order"])
        grp = lambda r: (1, first[(r["section"], r["group"])], r["group"]) if r["group"] and not r["core"] else (0, 0, "")  # noqa: E731
        out.sort(key=lambda r: (sec_rank.get(r["section"], 99), not r["core"], grp(r), r["_order"], r["key_en"]))
        for r in out:
            del r["_order"]
        return out


_METHOD_RANK = {"llm": 5, "embed": 4, "fallback": 4, "new": 3, "exact": 2, "registry": 2, "seed": 1, "derived": 1, "rule": 1, "override": 0}


def _worst_method(sources) -> str:
    ms = [s["method"] for ss in sources for s in ss]
    return max(ms, key=lambda m: _METHOD_RANK.get(m, 0)) if ms else "seed"


def build_compare(products: list[ProductRecord], documents: Optional[list[DocumentRecord]] = None,
                  raw_specs: Optional[list[RawSpec]] = None, modes: Optional[list[ModeRecord]] = None,
                  pod_items: Optional[list] = None, image_ref: Callable = lambda p: p.image_path,
                  canonicalizer: Optional[canon_mod.Canonicalizer] = None) -> "CompareResult":
    """{major: rows} for the products of each major category (never mixed). `pod_items` is the per-product list of
    pod.PodItem lists aligned with `products` (omit it for no POD rows); `image_ref(product)` supplies the value of
    the 'Image' row (the API passes a '/api/img/...' URL, the Excel writer the stored relative path). Rows are
    canonical attributes (see module docstring); the registry behind them grows and is saved after each build.
    The result is a plain dict plus `.canon_stats[major]` (Canonicalizer.run_stats: per-stage counts, embedding stage state)."""
    cz = canonicalizer or canon_mod.get_default()
    out = CompareResult()
    index = {id(p): i for i, p in enumerate(products)}
    for major, ps in group_products(products).items():
        cz.begin()
        specs = [features.dedupe_specs(p.extra_specs) for p in ps]
        raws_by_p = [[r for r in (raw_specs or []) if (r.brand, r.model_number) == (p.brand, p.model_number)] for p in ps]
        b = _Builder(major, ps, cz)
        b.fields()
        b.specs(specs)
        b.derived_flags(specs, raws_by_p)
        if pod_items is not None:
            b.pod([pod_items[index[id(p)]] for p in ps])
        if major == "refrigerator" and modes:
            b.modes(modes)
        out[major] = _basic_rows(ps, image_ref) + b.rows()
        out.canon_stats[major] = cz.run_stats()  # snapshot before the next major resets it
        cz.save()
    return out


class CompareResult(dict):
    """{major: rows}; `.canon_stats` = {major: run stats} (see canon.Canonicalizer.run_stats)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.canon_stats: dict[str, dict] = {}
