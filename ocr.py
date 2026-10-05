"""OCR a PDF page via the local vision model (LM Studio); disk-cached, never raises."""
import base64
import sys
from pathlib import Path

import fitz  # PyMuPDF

import llm

CACHE_DIR = Path(__file__).parent / "ocr_cache"
MAX_SIDE_PX = 1568
INSTRUCTION = ("Transcribe all text on this manual page in reading order; keep headings, bullets and tables "
               "as plain text; do not add commentary.")


def _render_png(pdf_path: Path, page_index: int, dpi: int) -> bytes:
    with fitz.open(pdf_path) as doc:
        page = doc[page_index]
        zoom = dpi / 72
        longest = max(page.rect.width, page.rect.height) * zoom
        if longest > MAX_SIDE_PX:
            zoom *= MAX_SIDE_PX / longest
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")


def ocr_page(pdf_path, page_index: int, dpi: int = 150, cache_dir: Path | None = None) -> str:
    """Return OCR text of a 0-based page, or '' on any failure. Cache hit makes no model call."""
    pdf_path = Path(pdf_path)
    cache = (cache_dir or CACHE_DIR) / f"{pdf_path.stem}_p{page_index + 1}_{dpi}.txt"
    try:
        if cache.exists():
            return cache.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[ocr] cache read failed {cache.name}: {exc}", file=sys.stderr)
    try:
        png = _render_png(pdf_path, page_index, dpi)
    except Exception as exc:  # noqa: BLE001 - bad pdf/page index: report, return ''
        print(f"[ocr] render failed {pdf_path.name} p{page_index + 1}: {exc}", file=sys.stderr)
        return ""
    text = llm.chat_vision(INSTRUCTION, base64.b64encode(png).decode("ascii"))
    if text:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"[ocr] cache write failed {cache.name}: {exc}", file=sys.stderr)
    return text
