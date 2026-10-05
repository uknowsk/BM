"""Extract refrigerator operating modes from manual PDFs via a local LLM (never invents data)."""
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

import llm
import ocr
from schema import MODE_CATEGORIES, DocumentRecord, ModeRecord, ProductRecord

ROOT = Path(__file__).parent
DOC_TYPES = {"Manual", "QuickSpecs", "SpecSheet"}
KEYWORDS = ("mode", "supercool", "super cool", "fast freeze", "turbo cool", "vacation", "holiday",
            "sabbath", "shabbos", "eco", "temperature control", "set point", "setpoint", "door alarm",
            "lock", "ice", "filter", "humidity", "vitafresh", "home connect", "wi-fi", "wifi")
TOP_PAGES = 8  # pages sent to the LLM per long document
MAX_OCR_PAGES = 8  # cap on OCR'd pages per document (OCR is slow)
SMALL_DOC_PAGES = 4  # spec-type docs this short are read in full
OCR_NEAR = 2  # garbled page is an OCR candidate if within this many pages of a high-scoring clean page
HIGH_SCORE = 3
TOPICS = {"vacation": r"vacation|holiday", "cool": r"super ?cool|turbo cool|express cool", "freeze": r"fast freeze|quick freeze|super ?freeze",
          "sabbath": r"sabbath|shabbos", "eco": r"\beco\b", "lock": r"lock", "alarm": r"alarm",
          "temp": r"temperature control|set ?point|temperature"}
PAGES_PER_CALL = 2
CHAR_CAP = 6000
MIN_ALPHA_RATIO = 0.5
GENERIC_NAMES = {"energy", "lock", "ice", "water", "filter", "light", "lights", "alarm", "temperature",
                 "mode", "modes", "settings", "setting", "features", "feature", "control", "controls", "humidity"}

PROMPT = """You are extracting refrigerator operating modes/features from manual pages.
Return ONLY a JSON array (English). Each item: {{"mode_name": str, "category": one of {cats}, \
"description": str (1 sentence, from the text), "setting_range": str or null (e.g. "34-44 F", only if stated), \
"how_to_activate": str or null (only if stated), "page": int (the page number given in the header)}}.
Cover temperature controls, SuperCool/Fast Freeze/Turbo Cool, Vacation/Holiday, Sabbath, Eco, ice and water options, \
door alarm, child/control lock, filter reminder, smart/Wi-Fi/Home Connect functions, fresh-food zones/humidity drawers.
Only include items explicitly described in the text. If none, return [].

{pages}"""

TEMP_PROMPT = """From these refrigerator manual pages, return ONLY JSON {{"fridge_temp_range_f": str or null, \
"freezer_temp_range_f": str or null}} giving the adjustable setpoint ranges in Fahrenheit (e.g. "34-44 F"), \
ONLY if literally stated in the text, else null.

{pages}"""


def alpha_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    return sum(c.isascii() and c.isalpha() for c in chars) / len(chars) if chars else 0.0


_KW_RX = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in KEYWORDS) + r")s?\b")


def score_page(text: str) -> int:
    hits = _KW_RX.findall(text.lower())  # whole-word ('eco' must not match 'recommended')
    return len(hits) + 3 * len(set(hits))  # reward variety of distinct modes over repeats


def pick_pages(pages: list[str], top: int = TOP_PAGES) -> list[int]:
    """0-based indexes of the best-scoring readable pages, returned in page order."""
    scored = [(score_page(t), i) for i, t in enumerate(pages) if alpha_ratio(t) >= MIN_ALPHA_RATIO]
    best = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))[:top]
    return sorted(i for _, i in best)


COMMON_WORDS = frozenset("the and to of is in for on or be you your with this that it as are can use from if when not by will".split())


def _shift(s: str) -> str:
    return "".join(chr(ord(c) + 29) if 3 <= ord(c) <= 98 else c for c in s)


def decode_text(text: str) -> str:
    """Undo the GE font encoding (glyph ids = ASCII - 29) token-by-token. Applied to a token when it holds a
    control char, or (inside an otherwise garbled line) when it has no lowercase and shifts to a word."""
    out = []
    for ln in text.split("\n"):
        ctrl = any(ord(c) < 32 for c in ln)
        toks = []
        for t in re.split(r"( )", ln):
            garbled = bool(t) and t != " " and (
                any(ord(c) < 32 for c in t)
                or (len(t) >= 2 and not re.search(r"[a-z°]", t) and all(3 <= ord(c) <= 98 for c in t)
                    and re.fullmatch(r"[A-Za-z][a-z]*[.,;:!?]?", _shift(t))
                    and (ctrl or _shift(t).lower().strip(".,;:!?") in COMMON_WORDS)))
            toks.append(_shift(t) if garbled else t)
        out.append("".join(toks))
    return "\n".join(out)


def is_garbled(text: str) -> bool:
    return alpha_ratio(text) < MIN_ALPHA_RATIO


def toc_pages(pages: list[str], n_pages: int) -> set[int]:
    """0-based pages cited by 'feature ..... 12' style index/TOC lines in the first pages."""
    kw = "|".join(re.escape(k) for k in KEYWORDS)
    found = set()
    for t in pages[:6]:
        for m in re.finditer(rf"(?im)^.*(?:{kw})[^\d\n]*?[ .\u2026]+(\d{{1,3}})\s*$", t):
            if 1 <= int(m.group(1)) <= n_pages:
                found.add(int(m.group(1)) - 1)
    return found


def pick_ocr_candidates(pages: list[str], cap: int = MAX_OCR_PAGES) -> list[int]:
    """Garbled/empty pages worth OCR: within OCR_NEAR of a high-scoring readable page, or cited by the
    TOC/index. Ranked by nearby score; at most cap; page order out. Small docs: every garbled page."""
    garbled = [i for i, t in enumerate(pages) if is_garbled(t)]
    if len(pages) <= SMALL_DOC_PAGES:
        return garbled[:cap]
    high = [(i, score_page(t)) for i, t in enumerate(pages) if not is_garbled(t) and score_page(t) >= HIGH_SCORE]
    toc = toc_pages(pages, len(pages))
    cand = {g: sum(sc / (1 + abs(g - i)) for i, sc in high if abs(g - i) <= OCR_NEAR) + (100 if g in toc else 0)
            for g in garbled}
    ranked = sorted((g for g, v in cand.items() if v > 0), key=lambda g: (-cand[g], g))
    return sorted(ranked[:cap])


def topics_found(modes: list[ModeRecord]) -> set[str]:
    text = " ".join(f"{m.mode_name} {m.description}" for m in modes).lower()
    return {k for k, rx in TOPICS.items() if re.search(rx, text)}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def grounded(item: dict, page_text: str) -> bool:
    """mode_name must literally appear on the cited page (case/punctuation-insensitive)."""
    name = _norm(str(item.get("mode_name", "")))
    return bool(name) and name in _norm(page_text)


def _pages_block(idxs: list[int], pages: list[str]) -> str:
    out = "\n\n".join(f"=== PAGE {i + 1} ===\n{pages[i]}" for i in idxs)
    return out[:CHAR_CAP]


def _clean(v) -> str | None:
    v = None if v is None else str(v).strip()
    return v or None


def junk(item: dict, page_text: str, strict: bool) -> bool:
    """Bare generic names (except Lock mentioning control lock) and spec-sheet marketing rows."""
    name = _norm(str(item.get("mode_name", "")))
    desc = str(item.get("description", "")).lower()
    if name in GENERIC_NAMES and not (name == "lock" and "control lock" in desc):
        return True
    t = page_text.lower()
    return strict and "mode" not in t and "function" not in t


def _records(items, idxs, pages, product, doc_name, ocr_idx=frozenset(), strict=False) -> list[ModeRecord]:
    recs = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            page = int(it.get("page"))
        except (TypeError, ValueError):
            page = None
        if page is None or page - 1 not in idxs:
            continue
        if not grounded(it, pages[page - 1]) or not _clean(it.get("description"))                 or junk(it, pages[page - 1], strict):
            continue
        cat = it.get("category") if it.get("category") in MODE_CATEGORIES else "Other"
        rng = _clean(it.get("setting_range"))
        if rng and not re.search(r"\d", rng):
            rng = None
        recs.append(ModeRecord(brand=product.brand, model_number=product.model_number,
                               mode_name=str(it["mode_name"]).strip(), category=cat,
                               description=_clean(it["description"]), setting_range=rng,
                               how_to_activate=_clean(it.get("how_to_activate")),
                               source_doc=f"{doc_name} (OCR)" if page - 1 in ocr_idx else doc_name,
                               source_page=page))
    return recs


def _literal_range(val, pages_text: str) -> str | None:
    """Accept a temp range only if its numbers appear in the page text."""
    val = _clean(val)
    nums = re.findall(r"-?\d+", val or "")
    return val if len(nums) >= 2 and all(n.lstrip("-") in pages_text for n in nums) else None


def extract_modes(product: ProductRecord, documents: list[DocumentRecord]) -> list[ModeRecord]:
    modes: list[ModeRecord] = []
    seen: set[tuple[str, str]] = set()
    for doc in documents:
        if len(topics_found(modes)) == len(TOPICS):
            print("[modes] all taxonomy topics found; stopping early", file=sys.stderr)
            break
        name = Path(doc.local_path).name
        if doc.doc_type not in DOC_TYPES and "homeconnect" not in name.lower():
            continue
        path = Path(doc.local_path)
        path = path if path.is_absolute() else ROOT / path
        try:
            with fitz.open(path) as d:
                pages = [decode_text(p.get_text()) for p in d]
        except Exception as exc:  # noqa: BLE001 - unreadable pdf: skip, keep going
            print(f"[modes] cannot read {name}: {exc}", file=sys.stderr)
            continue
        ocr_idx: set[int] = set()
        for i in pick_ocr_candidates(pages):
            text = ocr.ocr_page(path, i)
            if text.strip():
                pages[i] = text
                ocr_idx.add(i)
        if len(pages) <= SMALL_DOC_PAGES:
            idxs = [i for i, t in enumerate(pages) if t.strip()]
        else:
            idxs = pick_pages(pages)
        strict = doc.doc_type != "Manual" and "homeconnect" not in name.lower()
        for k in range(0, len(idxs), PAGES_PER_CALL):
            if len(topics_found(modes)) == len(TOPICS):
                break
            chunk = idxs[k:k + PAGES_PER_CALL]
            block = _pages_block(chunk, pages)
            reply = llm.chat_json(PROMPT.format(cats="|".join(MODE_CATEGORIES), pages=block))
            if reply is None:
                print(f"[modes] WARNING: LLM unreachable/unusable for {name} pages "
                      f"{[i + 1 for i in chunk]}; skipping", file=sys.stderr)
                continue
            for rec in _records(reply, chunk, pages, product, name, ocr_idx, strict):
                key = (rec.mode_name.lower(), name)
                if key not in seen:
                    seen.add(key)
                    modes.append(rec)
            if product.fridge_temp_range_f is None or product.freezer_temp_range_f is None:
                t = llm.chat_json(TEMP_PROMPT.format(pages=block))
                if isinstance(t, dict):
                    if product.fridge_temp_range_f is None:
                        product.fridge_temp_range_f = _literal_range(t.get("fridge_temp_range_f"), block)
                    if product.freezer_temp_range_f is None:
                        product.freezer_temp_range_f = _literal_range(t.get("freezer_temp_range_f"), block)
    if not modes:
        print(f"[modes] WARNING: no modes extracted for {product.brand} {product.model_number}", file=sys.stderr)
    return modes
