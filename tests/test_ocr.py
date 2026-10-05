"""Plain-assert tests. Run: python tests/test_ocr.py"""
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz
import llm
import modes
import ocr
from schema import DocumentRecord, ProductRecord

GARBLED = "\u00e4\u00f6\u00fc\u00e9\u00e8 \u4e2d\u6587 123 456 ###"


def _pdf(d, texts, name="M1_Manual.pdf"):
    pdf = Path(d) / name
    doc = fitz.open()
    for t in texts:
        doc.new_page().insert_text((72, 72), t)
    doc.save(pdf)
    doc.close()
    return pdf


def _rec(pdf, doc_type="Manual"):
    return DocumentRecord(brand="GE", model_number="M1", doc_type=doc_type, source_url="u",
                          local_path=str(pdf), sha256="a", size_bytes=1)


def _prod():
    return ProductRecord(brand="GE", model_number="M1", product_name="x", product_url="http://x")


def test_ocr_cache_and_payload():
    with tempfile.TemporaryDirectory() as d:
        pdf = _pdf(d, ["hello"])
        cd = Path(d) / "c"
        fake = mock.Mock(return_value="Vacation mode")
        with mock.patch.object(llm, "chat_vision", fake):
            assert ocr.ocr_page(pdf, 0, cache_dir=cd) == "Vacation mode"
            assert ocr.ocr_page(pdf, 0, cache_dir=cd) == "Vacation mode"
        assert fake.call_count == 1 and (cd / "M1_Manual_p1_150.txt").exists()
        assert "transcribe" in fake.call_args[0][0].lower() and len(fake.call_args[0][1]) > 100


def test_ocr_failure_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        pdf = _pdf(d, ["hello"])
        with mock.patch.object(llm, "chat_vision", lambda *a, **k: ""):
            assert ocr.ocr_page(pdf, 0, cache_dir=Path(d) / "c") == ""
            assert not list((Path(d) / "c").glob("*.txt")) if (Path(d) / "c").exists() else True
        assert ocr.ocr_page(pdf, 99, cache_dir=Path(d) / "c") == ""  # bad page index
        import requests
        with mock.patch.object(llm.requests, "post", side_effect=requests.ConnectionError("down")):
            assert llm.chat_vision("p", "AAAA") == ""


def test_candidate_routing_and_cap():
    assert modes.MAX_OCR_PAGES == 8 and modes.TOP_PAGES == 8
    pages = ["Vacation mode mode SuperCool"] + [GARBLED] * 40
    assert modes.pick_ocr_candidates(pages) == [1, 2]  # only within +-2 of the high-scoring clean page
    pages = [GARBLED] * 10 + ["mode mode mode"] + [GARBLED] * 10
    assert modes.pick_ocr_candidates(pages) == [8, 9, 11, 12]
    assert modes.pick_ocr_candidates(pages, cap=2) == [9, 11]
    assert modes.pick_ocr_candidates(["clean text only"] * 6) == []
    assert modes.pick_ocr_candidates([GARBLED] * 40) == []  # no signal -> no OCR
    toc = ["Contents\nVacation mode .......... 30"] + [GARBLED] * 40
    assert 29 in modes.pick_ocr_candidates(toc)
    many = ["mode " * 5] * 3 + [GARBLED] * 30 + ["mode " * 5] * 3 + [GARBLED] * 3
    assert len(modes.pick_ocr_candidates(many)) <= modes.MAX_OCR_PAGES
    assert modes.pick_ocr_candidates([GARBLED, "x"]) == [0]  # small doc: all garbled pages


def test_small_doc_read_in_full_and_early_stop():
    with tempfile.TemporaryDirectory() as d:
        items = [{"mode_name": "Zone", "category": "Other", "description": "z.", "page": 1}]
        out, oc, chat = _run(d, ["plain unrelated words", GARBLED], {1: "ocr text"}, items, "SpecSheet", "M1_SpecSheet.pdf")
        assert oc.call_count == 1 and "PAGE 2" in chat.call_args_list[0][0][0] and "PAGE 1" in chat.call_args_list[0][0][0]
    with tempfile.TemporaryDirectory() as d:  # early stop: all topics found -> no second chunk / second doc
        txt = "Vacation Turbo Cool Fast Freeze Sabbath Eco Lock Door Alarm Temperature Control mode"
        pdf = _pdf(d, [txt] * 6)
        names = ["Vacation", "Turbo Cool", "Fast Freeze", "Sabbath", "Eco", "Lock", "Door Alarm", "Temperature Control"]
        items = [{"mode_name": n, "category": "Other", "description": f"{n} control lock alarm.", "page": 1} for n in names]
        replies = [items]
        chat = mock.Mock(side_effect=lambda prompt: replies.pop(0) if replies else [])
        with mock.patch.object(llm, "chat_json", chat), mock.patch.object(ocr, "ocr_page", lambda *a, **k: ""):
            out = modes.extract_modes(_prod(), [_rec(pdf), _rec(pdf)])
        assert len(out) == 8 and chat.call_count <= 2  # first chunk (+ temp prompt) only



def _run(d, texts, ocr_map, reply_items, doc_type="Manual", name="M1_Manual.pdf"):
    pdf = _pdf(d, texts, name)
    replies = [reply_items]
    chat = mock.Mock(side_effect=lambda prompt: replies.pop(0) if replies else None)
    oc = mock.Mock(side_effect=lambda p, i, *a, **k: ocr_map.get(i, ""))
    with mock.patch.object(llm, "chat_json", chat), mock.patch.object(ocr, "ocr_page", oc):
        return modes.extract_modes(_prod(), [_rec(pdf, doc_type)]), oc, chat


def test_ocr_text_feeds_pipeline_and_marks_source():
    with tempfile.TemporaryDirectory() as d:
        items = [{"mode_name": "Fast Freeze", "category": "Freezing", "description": "Freezes quickly.", "page": 2},
                 {"mode_name": "Turbo Teleport", "category": "Cooling", "description": "Invented.", "page": 2}]
        out, oc, _ = _run(d, ["Vacation mode intro", GARBLED], {1: "Press Fast Freeze to freeze quickly. mode"}, items)
        assert oc.call_count == 1
    assert [(m.mode_name, m.source_page, m.source_doc) for m in out] == [("Fast Freeze", 2, "M1_Manual.pdf (OCR)")]


def test_ocr_unavailable_still_works():
    with tempfile.TemporaryDirectory() as d:
        items = [{"mode_name": "Vacation mode", "category": "Away", "description": "Away.", "page": 1}]
        out, _, _ = _run(d, ["Vacation mode intro", GARBLED], {}, items)
    assert [(m.mode_name, m.source_doc) for m in out] == [("Vacation mode", "M1_Manual.pdf")]


def test_junk_filters():
    with tempfile.TemporaryDirectory() as d:
        items = [{"mode_name": "Energy", "description": "x", "page": 1},
                 {"mode_name": "Lock", "description": "x", "page": 1},
                 {"mode_name": "Lock", "description": "Enables control lock.", "page": 1}]
        out, _, _ = _run(d, ["Energy and Lock mode"], {}, items)
        assert [(m.mode_name, m.description) for m in out] == [("Lock", "Enables control lock.")]
    with tempfile.TemporaryDirectory() as d:  # spec sheet marketing: no 'mode'/'function' on page
        items = [{"mode_name": "VitaFreshPro", "category": "Freshness", "description": "Keeps fresh.", "page": 1}]
        out, _, _ = _run(d, ["VitaFreshPro drawer keeps food fresh"], {}, items, "SpecSheet", "M1_SpecSheet.pdf")
        assert out == []
    with tempfile.TemporaryDirectory() as d:  # ...but kept if page says 'mode'
        out, _, _ = _run(d, ["VitaFreshPro mode keeps food fresh"], {}, items, "SpecSheet", "M1_SpecSheet.pdf")
        assert len(out) == 1


def test_decode_font_encoded_text():
    enc = lambda w: "".join(chr(ord(c) - 29) if c != " " else "" for c in w)
    line = enc("Press") + " and hold 3 seconds " + enc("to reset") + " the Filter."
    assert modes.decode_text(line) == "Press and hold 3 seconds to reset the Filter."
    assert modes.decode_text("THANK YOU 37°F") == "THANK YOU 37°F"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
