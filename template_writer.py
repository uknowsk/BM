"""Write a BoundTemplate into the user's OWN form workbook.

The uploaded workbook is copied with openpyxl (formatting, column widths, merged cells, validations survive; formulas stay
text-only and are never evaluated, and any formula that is not plainly arithmetic is turned into text). One column per product
is appended to the right of the form's last used column; each form row is filled in place:

    found    the value (numbers are real numbers shown with the form's unit), flags show a check mark
    absent   '–' (the spec says No)                     unknown  '정보 없음' (grey italic: nothing was found, NOT the same as absent)
    derived  a value computed from an item list (light-blue fill + comment)
    comment  original wording, source label and match method;  '검토' column (far right, amber) = binding needs review

Extra sheets: '매칭 결과' (audit: form item -> canonical id -> per-product source wording / method / score) and
'양식 외 항목' (competitor features the form does not cover, with a suggested form category).
"""
from __future__ import annotations

import math
import os
import re
import sys
from copy import copy
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.cell_range import CellRange

import catalog
import excel_writer
from excel_writer import clean_text, is_formula_like
from template import OTHER_SHEET, BoundSheet, BoundTemplate, Template

ROOT = Path(__file__).resolve().parent
AUDIT_SHEET = "매칭 결과"
REVIEW_HEAD = "검토"
PRODUCT_COL_WIDTH = 24
IMG_PX = 100
NL = chr(10)
FILL_YES = PatternFill("solid", fgColor="C6EFCE")
FILL_NO = PatternFill("solid", fgColor="EDEDED")
FILL_DERIVED = PatternFill("solid", fgColor="DDEBF7")
FILL_REVIEW = PatternFill("solid", fgColor="FFE699")
FILL_HEAD = PatternFill("solid", fgColor="1F4E78")
FONT_YES = Font(color="1F6B47", bold=True)
FONT_NO = Font(color="808080")
FONT_UNKNOWN = Font(color="8C8C8C", italic=True)
FONT_HEAD = Font(bold=True, color="FFFFFF")
FONT_BOLD = Font(bold=True)
_SAFE_FORMULA = re.compile(r"^=\s*(?:(?:SUM|AVERAGE|COUNT|COUNTA|MIN|MAX|ROUND|IF|LEN|TRIM|ABS)\s*\(|[A-Za-z]{1,3}\$?\d{1,5}|[\d(+\-])"
                           r"[A-Za-z0-9_+\-*/().,:$ <>=&\"]*$")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def _neutralize(cell) -> None:
    """Strings that look like formulas are forced to text (excel_writer's helper; same rule as the rest of the app)."""
    fn = getattr(excel_writer, "_neutralize", None)
    if fn is not None:
        fn(cell)
    elif isinstance(cell.value, str) and is_formula_like(cell.value):
        cell.data_type = "s"
        cell.quotePrefix = True


def _put(ws, r: int, c: int, value, *, font=None, fill=None, align=None, fmt: Optional[str] = None):
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    cell = ws.cell(r, c)
    cell.value = clean_text(value) if isinstance(value, str) else value
    if isinstance(cell.value, str):
        _neutralize(cell)
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    if align:
        cell.alignment = align
    if fmt:
        cell.number_format = fmt
    return cell


def _comment(cell, text: str) -> None:
    text = clean_text(text)[:900]
    if text:
        com = Comment(text, "Gauge")
        com.width, com.height = 340, min(70 + 16 * text.count(NL), 320)
        cell.comment = com


def _sheet_title(wb, wanted: str) -> str:
    base = _SHEET_BAD.sub(" ", wanted).strip()[:31] or "sheet"
    title, i = base, 2
    while title in wb.sheetnames:
        title = f"{base[:28]} {i}"
        i += 1
    return title


def _sanitize_form(wb) -> None:
    """The copy of the user's form never carries active content: formulas other than plain arithmetic become text."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    f = cell.value
                    if not (isinstance(f, str) and _SAFE_FORMULA.match(f)):
                        cell.value = str(f) if isinstance(f, str) else ""
                        _neutralize(cell)
        for dv in list(getattr(ws.data_validations, "dataValidation", [])):  # validations may not reference other files
            if dv.formula1 and "[" in str(dv.formula1):
                ws.data_validations.dataValidation.remove(dv)


def _market(p) -> str:
    node = catalog.REGIONS.get(p.region) or {}
    return f"{node.get('label_ko', p.region)} ({str(p.country or '').upper()})" if p.country else str(node.get("label_ko", p.region))


def _price(p) -> str:
    if p.price_usd is not None:
        return f"${p.price_usd:,.0f}"
    if p.price_local is not None:
        return f"{p.price_local:,.0f} {p.currency}"
    return "가격 미확인"


def _picture(p, root: Path):
    """An embedded copy of the product's downloaded picture (only files inside downloads/), or None."""
    if not p.image_path:
        return None
    try:
        from openpyxl.drawing.image import Image as XlImage
        downloads = (root / "downloads").resolve()
        path = (root / p.image_path).resolve()
        if not (path.is_relative_to(downloads) and path.is_file()):
            return None
        img = XlImage(str(path))
        k = min(IMG_PX / img.width, IMG_PX / img.height, 1.0)
        img.width, img.height = max(1, round(img.width * k)), max(1, round(img.height * k))
        return img
    except Exception as exc:  # noqa: BLE001 - Pillow missing / corrupt picture: the column is still useful
        print(f"[template] picture skipped: {type(exc).__name__}", file=sys.stderr)
        return None


def _shift_rows(ws, n: int = 1) -> None:
    """Insert n rows at the top (a form without a header row): merged ranges and row heights move down with the cells."""
    merges = [str(r) for r in ws.merged_cells.ranges]
    heights = {k: d.height for k, d in ws.row_dimensions.items() if d.height}
    for m in merges:
        ws.unmerge_cells(m)
    ws.insert_rows(1, n)
    for m in merges:
        cr = CellRange(m)
        cr.shift(row_shift=n)
        ws.merge_cells(cr.coord)
    for k in sorted(heights, reverse=True):
        ws.row_dimensions[k + n].height = heights[k]
        ws.row_dimensions[k].height = None


def _fill_cell(ws, r: int, c: int, cell, item_unit: str, src) -> None:
    """One bound cell (see module docstring); `src` is the form's last cell of the row (border / font family are copied)."""
    align = Alignment(wrap_text=True, vertical="center", horizontal="center")
    if cell.status == "unknown":
        out = _put(ws, r, c, "정보 없음", font=FONT_UNKNOWN, fill=None, align=align)
    elif cell.status == "absent":
        out = _put(ws, r, c, "–", font=FONT_NO, fill=FILL_NO, align=align)
    else:
        v = cell.value
        fill = FILL_DERIVED if cell.status == "derived" else None
        if v is True:
            out = _put(ws, r, c, "✓", font=FONT_YES, fill=FILL_YES if fill is None else fill, align=align)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            unit = (cell.unit or item_unit or "").replace('"', "")
            out = _put(ws, r, c, v, fill=fill, align=Alignment(wrap_text=True, vertical="center", horizontal="right"),
                       fmt=f'General" {unit}"' if unit else "General")
        elif isinstance(v, list):
            out = _put(ws, r, c, "; ".join(map(str, v)), fill=fill, align=Alignment(wrap_text=True, vertical="top"))
        else:
            out = _put(ws, r, c, str(v if v is not None else cell.display), fill=fill, align=Alignment(wrap_text=True, vertical="center"))
        if cell.status == "derived":
            out.font = Font(italic=True, color="1F4E78")
    if src is not None:
        out.border = copy(src.border)
    lines = []
    if cell.sources:
        lines.append("원문 항목명 = 값")
        lines += [f"  {s['label']} = {s['value']}" for s in cell.sources]
    elif cell.source_label:
        lines.append(f"원문: {cell.source_label} = {cell.source_value}" if cell.source_value else f"원문: {cell.source_label}")
    if cell.method:
        lines.append(f"매칭: {cell.method} {cell.score:.2f}")
    if cell.status == "derived":
        lines.append("계산된 값 (품목 목록에서 개수를 센 값)")
    if cell.note:
        lines.append(f"비고: {cell.note}")
    if cell.needs_review:
        lines.append("검토 필요")
    if cell.status == "absent":
        lines.append("사양에 '없음'으로 명시됨 (정보 없음과 다름)")
    _comment(out, NL.join(lines))


def _product_header(ws, sheet, products: list, c0: int, root: Path, off: int) -> None:
    """Header block of the product columns: picture, brand, model, price, market (rows above the form header when there is
    room, else one multi-line header cell with the picture over it)."""
    h = (sheet.header_row or 0) + (off if sheet.header_row else 0)
    first_item = min((i.row for i in sheet.items), default=h + 1) + off
    if not sheet.header_row:
        h = first_item - 1
    block = h >= 5
    src = ws.cell(h, max(1, c0 - 1))
    title_font = copy(src.font) if src.has_style else FONT_BOLD
    for i, p in enumerate(products):
        c = c0 + i
        ws.column_dimensions[get_column_letter(c)].width = PRODUCT_COL_WIDTH
        img = _picture(p, root)
        center = Alignment(wrap_text=True, vertical="center", horizontal="center")
        if block:
            _put(ws, h - 3, c, p.brand, font=FONT_BOLD, align=center)
            _put(ws, h - 2, c, p.model_number, font=Font(bold=True, size=12), align=center)
            _put(ws, h - 1, c, f"{_price(p)} · {_market(p)}", font=Font(size=10), align=center)
            _put(ws, h, c, f"{p.brand} {p.model_number}", font=copy(title_font), fill=copy(src.fill) if src.has_style else None, align=center)
            if img is not None:
                ws.add_image(img, f"{get_column_letter(c)}{h - 4}")
                ws.row_dimensions[h - 4].height = max(ws.row_dimensions[h - 4].height or 0, (img.height + 6) * 0.75)
        else:
            cell = _put(ws, h, c, NL.join([p.brand, p.model_number, f"{_price(p)} · {_market(p)}"]), font=copy(title_font),
                        align=Alignment(wrap_text=True, vertical="bottom", horizontal="center"))
            if src.has_style:
                cell.fill, cell.border = copy(src.fill), copy(src.border)
            need = 50 + (img.height + 6 if img is not None else 0)
            if img is not None:
                ws.add_image(img, f"{get_column_letter(c)}{h}")
            ws.row_dimensions[h].height = max(ws.row_dimensions[h].height or 0, need * 0.75)
    return None


def _fill_sheet(wb, bs: BoundSheet, root: Path) -> None:
    ws = wb[bs.sheet.name]
    sheet = bs.sheet
    off = 0
    if not sheet.header_row and min((i.row for i in sheet.items), default=2) <= 1:
        _shift_rows(ws, 1)
        off = 1
    c0 = sheet.last_col + 1
    n = len(bs.products)
    c_review = c0 + n
    _product_header(ws, sheet, bs.products, c0, root, off)
    h = ((sheet.header_row + off) if sheet.header_row else min(i.row for i in sheet.items) + off - 1)
    _put(ws, h, c_review, REVIEW_HEAD, font=FONT_BOLD, align=Alignment(horizontal="center", vertical="center"))
    ws.column_dimensions[get_column_letter(c_review)].width = 9
    src_col = max(1, c0 - 1)
    item_rows = {r.item.row + off for r in bs.rows}
    for br in bs.rows:
        r = br.item.row + off
        src = ws.cell(r, src_col)
        for i, cell in enumerate(br.cells):
            _fill_cell(ws, r, c0 + i, cell, br.item.unit, src if src.has_style else None)
        flag = _put(ws, r, c_review, REVIEW_HEAD if br.needs_review else None, font=FONT_BOLD,
                    fill=FILL_REVIEW if br.needs_review else None, align=Alignment(horizontal="center", vertical="center"))
        if br.needs_review:
            reasons = [f"{c.source_label}: {c.note or c.method}" for c in br.cells if c.needs_review and c.source_label][:6]
            _comment(flag, f"항목 '{br.item.label}' 매칭 검토 필요 ({br.method} {br.score:.2f})" + (NL + NL.join(reasons) if reasons else ""))
    # category header rows of the form: carry their fill across the product columns so the block reads as one table
    last_row = max((sheet.last_row + off), h + 1)
    for r in range(h + 1, last_row + 1):
        if r in item_rows:
            continue
        s = ws.cell(r, src_col)
        if s.has_style and s.fill is not None and s.fill.fill_type == "solid":
            for c in range(c0, c_review + 1):
                ws.cell(r, c).fill = copy(s.fill)


def _band(ws, r: int, text: str, width: int) -> None:
    for c in range(1, width + 1):
        ws.cell(r, c).fill = PatternFill("solid", fgColor="F2F2F2")
    _put(ws, r, 1, text, font=FONT_BOLD)


def _head(ws, r: int, headers: list[str]) -> None:
    for c, h in enumerate(headers, 1):
        _put(ws, r, c, h, font=FONT_HEAD, fill=FILL_HEAD, align=Alignment(wrap_text=True, vertical="center", horizontal="center"))


def _audit_sheet(wb, bound: BoundTemplate) -> None:
    ws = wb.create_sheet(_sheet_title(wb, AUDIT_SHEET))
    fixed = ["시트", "구분", "항목", "단위", "유형", "canonical id", "매칭 방식", "점수", "매칭된 항목명"]
    per = ["원문 항목명", "원문 값", "상태", "방식", "점수"]
    maxp = max((len(bs.products) for bs in bound.sheets), default=0)
    _put(ws, 1, 1, bound.stats.get("line_ko") or "매칭 통계 없음", font=FONT_BOLD)
    _head(ws, 2, fixed + [f"{p}" for _ in range(maxp) for p in per] + ["검토 필요"])
    ws.cell(2, 1).comment = Comment("방식: synonym(사용자 동의어) > exact > canon > embed > llm(LLM이 후보 중 선택) > family(같은 계열 행이 근거). 검토 = 임베딩/LLM으로 맞춘 항목 또는 단위 불일치.", "Gauge")
    r = 3
    for bs in bound.sheets:
        _band(ws, r, f"{bs.sheet.name}  →  " + " | ".join(f"{p.brand} {p.model_number}" for p in bs.products), len(fixed) + 5 * maxp + 1)
        r += 1
        for br in bs.rows:
            it = br.item
            vals = [bs.sheet.name, it.category, it.label, it.unit, it.type, br.canon_id, br.method, round(br.score, 2), br.matched_label]
            for c, v in enumerate(vals, 1):
                _put(ws, r, c, v)
            for i, cell in enumerate(br.cells):
                base = len(fixed) + 5 * i + 1
                labels = NL.join(s["label"] for s in cell.sources) or cell.source_label
                values = NL.join(s["value"][:120] for s in cell.sources) or cell.source_value[:200]
                for j, v in enumerate([labels, values, cell.status, cell.method, round(cell.score, 2) if cell.method else ""]):
                    _put(ws, r, base + j, v, font=FONT_UNKNOWN if cell.status == "unknown" and j == 2 else None)
            col = len(fixed) + 5 * maxp + 1
            _put(ws, r, col, REVIEW_HEAD if br.needs_review else None, fill=FILL_REVIEW if br.needs_review else None, font=FONT_BOLD)
            r += 1
    for c, w in enumerate([14, 14, 24, 8, 8, 24, 10, 7, 24] + [30, 28, 9, 9, 7] * maxp + [9], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "D3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(fixed) + 5 * maxp + 1)}{max(r - 1, 3)}"


def _uncovered_sheet(wb, bound: BoundTemplate) -> None:
    ws = wb.create_sheet(_sheet_title(wb, OTHER_SHEET))
    fixed = ["시트", "제안 구분", "제안 방식", "섹션", "항목 (EN)", "항목 (KO)", "단위", "핵심", "원문 항목명"]
    maxp = max((len(bs.products) for bs in bound.sheets), default=0)
    _head(ws, 1, fixed + [f"제품 {i + 1}" for i in range(maxp)])
    ws.cell(1, 2).comment = Comment("양식에 이 항목을 추가한다면 어느 구분이 가장 가까운지 제안합니다 (섹션 일치 > 임베딩 > 토큰 > LLM).", "Gauge")
    r = 2
    for bs in bound.sheets:
        _band(ws, r, f"{bs.sheet.name}  →  " + " | ".join(f"{p.brand} {p.model_number}" for p in bs.products), len(fixed) + maxp)
        r += 1
        for u in bs.uncovered:
            vals = [bs.sheet.name, u["suggested_category"], u["suggest_method"], u["section"], u["key_en"], u["key_ko"], u["unit"],
                    "Y" if u["core"] else "", u["source_label"]] + list(u["values"])
            for c, v in enumerate(vals, 1):
                _put(ws, r, c, v, fill=FILL_REVIEW if c == 2 and not v else None)
            r += 1
    for c, w in enumerate([14, 16, 9, 14, 30, 24, 8, 6, 34] + [22] * maxp, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(fixed) + maxp)}{max(r - 1, 2)}"


def write_filled(template: Template, bound: BoundTemplate, path: str | Path, root: Path = ROOT) -> Path:
    """Fill a copy of the user's form with the bound products and save it to `path` (parent dirs created, atomic)."""
    if not template.raw:
        raise ValueError("template has no workbook bytes")
    wb = load_workbook(BytesIO(template.raw), read_only=False, data_only=False, keep_vba=False, keep_links=False)
    _sanitize_form(wb)
    for bs in bound.sheets:
        if bs.sheet.name in wb.sheetnames and bs.products:
            _fill_sheet(wb, bs, root)
    _audit_sheet(wb, bound)
    _uncovered_sheet(wb, bound)
    return _save(wb, Path(path))


def _save(wb, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
        return path
    except PermissionError:  # open in Excel: keep the work under a timestamped name
        alt = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        os.replace(tmp, alt)
        print(f"[template] {path.name} is locked; saved to {alt.name}", file=sys.stderr)
        return alt
