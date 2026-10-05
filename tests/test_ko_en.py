"""Plain-assert tests (pytest not installed). Run: python tests/test_ko_en.py
Korean -> English glossary/LLM translation, KR quantity parsing, KRW prices, KR energy grades, host allowlists."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
import ko_en


class FakeLLM:
    """Stands in for ko_en._chat: records prompts, answers {ko: en} from `table` (unknown -> omitted)."""
    def __init__(self, table=None, fail=False):
        self.table, self.fail, self.prompts = table or {}, fail, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            return None
        return {k: v for k, v in self.table.items() if k in prompt}


class Env:
    """Temp cache file + fake LLM for the duration of a test."""
    def __init__(self, llm):
        self.llm = llm

    def __enter__(self):
        self.t = tempfile.TemporaryDirectory()
        self.saved = (ko_en.CACHE_PATH, ko_en._chat, dict(ko_en._cache_mem))
        ko_en.CACHE_PATH = Path(self.t.name) / "ko_cache.json"
        ko_en._chat = self.llm
        ko_en._cache_mem.clear()
        return self

    def __exit__(self, *a):
        ko_en.CACHE_PATH, ko_en._chat = self.saved[0], self.saved[1]
        ko_en._cache_mem.clear()
        ko_en._cache_mem.update(self.saved[2])
        self.t.cleanup()


def test_glossary_size_and_shape():
    g = json.loads(ko_en.GLOSSARY_PATH.read_text(encoding="utf-8"))
    assert len(g["labels"]) >= 300, len(g["labels"])
    assert len(g["values"]) >= 60, len(g["values"])
    for sect in (g["labels"], g["values"]):
        for k, v in sect.items():
            assert isinstance(v, str) and v.strip(), k
            assert not ko_en._has_hangul(v), (k, v)  # an English glossary must not contain Hangul


def test_translate_key_glossary_hits_without_llm():
    with Env(FakeLLM(fail=True)) as e:
        assert ko_en.translate_key("총 용량") == "Total capacity"
        assert ko_en.translate_key("총용량") == "Total capacity"  # whitespace-insensitive
        assert ko_en.translate_key("냉장실 용량") == "Fresh food capacity"
        assert ko_en.translate_key("정격전압") == "Rated voltage"
        assert ko_en.translate_key("연간 소비전력량") == "Annual energy consumption"
        assert ko_en.translate_key("에너지소비효율등급") == "Energy efficiency grade (KR)"
        assert ko_en.translate_key("에너지 소비효율등급") == "Energy efficiency grade (KR)"
        assert ko_en.translate_key("인버터 컴프레서") == "Inverter compressor"
        assert ko_en.translate_key("도어쿨링⁺") == ko_en.translate_key("도어쿨링+")  # NFKC superscript plus
        assert e.llm.prompts == []


def test_translate_key_unit_suffix_kept():
    with Env(FakeLLM(fail=True)) as e:
        assert ko_en.translate_key("전체 용량 (L)") == "Total capacity (L)"
        assert ko_en.translate_key("소비전력 (kWh/월)") == "Power consumption (kWh/month)"
        assert ko_en.translate_key("무게 (kg)") == "Weight (kg)"
        assert e.llm.prompts == []


def test_translate_key_llm_fallback_then_cache():
    with Env(FakeLLM({"신기능쿨링": "New cooling function"})) as e:
        assert ko_en.translate_key("신기능쿨링") == "New cooling function"
        assert len(e.llm.prompts) == 1
        assert ko_en.translate_key("신기능쿨링") == "New cooling function"
        assert len(e.llm.prompts) == 1  # second call served from cache
        # persisted on disk, keyed by sha1 (no raw Korean in keys), survives a memory flush
        data = json.loads(ko_en.CACHE_PATH.read_text(encoding="utf-8"))
        assert all(len(k) == 40 for k in data) and "New cooling function" in data.values()
        ko_en._cache_mem.clear()
        assert ko_en.translate_key("신기능쿨링") == "New cooling function"
        assert len(e.llm.prompts) == 1


def test_llm_unreachable_returns_original_and_does_not_cache():
    with Env(FakeLLM(fail=True)) as e:
        assert ko_en.translate_key("알수없는항목") == "알수없는항목"
        assert not ko_en.CACHE_PATH.exists()
        e.llm.fail, e.llm.table = False, {"알수없는항목": "Unknown item"}
        assert ko_en.translate_key("알수없는항목") == "Unknown item"  # failure was not remembered


def test_llm_garbage_is_rejected():
    # reply still containing Hangul, empty, or non-string -> not trusted (never invent), original returned
    for bad in ("알수없는항목", "", 5, None):
        with Env(FakeLLM({"알수없는항목": bad})):
            assert ko_en.translate_key("알수없는항목") == "알수없는항목"
            assert not ko_en.CACHE_PATH.exists()


def test_translate_many_batches_one_call():
    table = {"가나": "Gana", "다라": "Dara", "마바": "Maba"}
    with Env(FakeLLM(table)) as e:
        out = ko_en.translate_many(["있음", "가나", "다라", "가나", "마바", "12 kg"])
        assert out == ["Yes", "Gana", "Dara", "Gana", "Maba", "12 kg"]
        assert len(e.llm.prompts) == 1  # glossary/number hits skip the LLM; unknowns deduped into one batch
        assert ko_en.translate_many([]) == []


def test_translate_value_glossary_and_passthrough():
    with Env(FakeLLM(fail=True)) as e:
        assert ko_en.translate_value("있음") == "Yes"
        assert ko_en.translate_value("없음") == "No"
        assert ko_en.translate_value("O") == "Yes" and ko_en.translate_value("X") == "No"
        assert ko_en.translate_value("지원") == "Supported" and ko_en.translate_value("미지원") == "Not supported"
        assert ko_en.translate_value("스테인리스 스틸") == "Stainless steel"
        assert ko_en.translate_value("매트 블랙") == "Matte black"
        assert ko_en.translate_value("620 x 1,850 x 700") == "620 x 1,850 x 700"  # numbers/units untouched
        assert ko_en.translate_value("220V / 60Hz") == "220V / 60Hz"
        assert ko_en.translate_value("") == ""
        assert e.llm.prompts == []


def test_translate_value_composite_pieces():
    with Env(FakeLLM(fail=True)) as e:
        assert ko_en.translate_value("O (좌/우)") == "Yes (left/right)"
        assert ko_en.translate_value("상냉장/하냉동") == "Top refrigerator / bottom freezer"
        assert ko_en.translate_value("3개") == "3 pcs"
        assert e.llm.prompts == []


def test_translate_value_unknown_goes_to_llm_and_unreachable_keeps_text():
    with Env(FakeLLM({"새로운 마감": "New finish"})) as e:
        assert ko_en.translate_value("새로운 마감") == "New finish"
        assert ko_en.translate_value("새로운 마감") == "New finish" and len(e.llm.prompts) == 1
    with Env(FakeLLM(fail=True)):
        assert ko_en.translate_value("새로운 마감") == "새로운 마감"


def test_parse_kr_quantity_units_and_formats():
    q = ko_en.parse_kr_quantity
    assert q("636L") == {"value": 636.0, "unit": "L"}
    assert q("약 636 리터") == {"value": 636.0, "unit": "L"}
    assert q("1,850 mm") == {"value": 1850.0, "unit": "mm"}
    assert q("약 85.5kg") == {"value": 85.5, "unit": "kg"}
    assert q("85.5㎏") == {"value": 85.5, "unit": "kg"}  # compatibility glyph
    assert q("1,200W") == {"value": 1200.0, "unit": "W"}
    assert q("40.2 kWh/월") == {"value": 40.2, "unit": "kWh/month"}
    assert q("482 kWh/년") == {"value": 482.0, "unit": "kWh/year"}
    assert q("220V / 60Hz") == {"value": 220.0, "unit": "V"}
    assert q("1,400 rpm") == {"value": 1400.0, "unit": "rpm"}
    assert q("39dB") == {"value": 39.0, "unit": "dB"}
    assert q("39 dB(A)") == {"value": 39.0, "unit": "dB"}
    assert q("250℃") == {"value": 250.0, "unit": "C"}
    r = q("40~45dB")
    assert r["value"] == 40.0 and r["max"] == 45.0 and r["unit"] == "dB"
    assert q("") is None and q("없음") is None and q(None) is None
    assert q("약 636") is None  # no unit -> not a quantity


def test_parse_dimensions():
    d = ko_en.parse_dimensions
    assert d("595 x 1,850 x 688 mm") == (595.0, 1850.0, 688.0)
    assert d("595×1850×688") == (595.0, 1850.0, 688.0)
    assert d("가로 595 X 세로 1850 X 깊이 688") == (595.0, 1850.0, 688.0)
    assert d("595 x 1850") is None and d("") is None


def test_convert_to_schema():
    q = ko_en.convert_to_schema("Total capacity", 636, "L")
    assert isinstance(q, ko_en.Quantity)
    assert (q.si_value, q.si_unit, q.unit) == (636, "L", "cu ft") and abs(q.value - 22.46) < 0.01
    q = ko_en.convert_to_schema("Height", 1850, "mm")
    assert (q.si_value, q.si_unit, q.unit) == (1850, "mm", "in") and abs(q.value - 72.83) < 0.01
    q = ko_en.convert_to_schema("Weight", 100, "kg")
    assert q.unit == "lb" and abs(q.value - 220.46) < 0.01 and q.si_value == 100 and q.si_unit == "kg"
    # washer/dryer drum capacity in kg is NOT convertible to cu ft/lb capacity -> kept in kg
    q = ko_en.convert_to_schema("Washer capacity (kg)", 24, "kg")
    assert (q.value, q.unit, q.si_value, q.si_unit) == (24, "kg", 24, "kg")
    q = ko_en.convert_to_schema("Power consumption", 1200, "W")
    assert (q.value, q.unit, q.si_value, q.si_unit) == (1200, "W", 1200, "W")
    q = ko_en.convert_to_schema("Oven max temperature", 250, "C")
    assert q.unit == "F" and q.value == 482 and q.si_unit == "C"


def test_energy_grade_and_annualize():
    assert ko_en.kr_energy_grade("1등급") == "KR grade 1"
    assert ko_en.kr_energy_grade("1 등급") == "KR grade 1"
    assert ko_en.kr_energy_grade("5") == "KR grade 5"
    assert ko_en.kr_energy_grade("에너지소비효율 2등급") == "KR grade 2"
    assert ko_en.kr_energy_grade("6등급") is None and ko_en.kr_energy_grade("해당없음") is None
    assert ko_en.kr_energy_grade("") is None
    assert "ENERGY STAR" not in ko_en.kr_energy_grade("1등급")
    assert ko_en.annualize(40.2) == 482.4 and ko_en.annualize(0) == 0
    assert "estimated" in ko_en.ANNUALIZED_LABEL and "monthly" in ko_en.ANNUALIZED_LABEL


def test_parse_krw():
    p = ko_en.parse_krw
    assert p("1,990,000원") == 1990000
    assert p("1,990,000 원") == 1990000
    assert p("₩1,990,000") == 1990000
    assert p("199만원") == 1990000
    assert p("199만 9천원") == 1999000
    assert p("1억 2천만원") == 120000000
    assert p("최저가 1,990,000원") == 1990000
    assert p("월 51,480 원") == 51480
    assert p(1990000) == 1990000 and p("1990000") == 1990000
    for bad in ("최저가", "최저가 확인", "가격문의", "품절", "", None, "0원", "-5원", True, 0):
        assert p(bad) is None, bad


def test_host_allowlists_korea():
    for host in ("www.lge.co.kr", "static.lge.co.kr", "lge.co.kr", "images.samsung.com", "image.samsung.com",
                 "www.samsung.com", "gscs-b2c.lge.com", "downloadcenter.samsung.com"):
        assert common._host_allowed(host, common.DOWNLOAD_HOST_ALLOW), host
        assert common._host_allowed(host, common.IMAGE_HOST_ALLOW), host
    for host in ("evil-lge.co.kr", "lge.co.kr.evil.com", "notsamsung.com", "co.kr", "evil.co.kr"):
        assert not common._host_allowed(host, common.DOWNLOAD_HOST_ALLOW), host
        assert not common._host_allowed(host, common.IMAGE_HOST_ALLOW), host
    assert common._url_problem("http://static.lge.co.kr/a.jpg", common.IMAGE_HOST_ALLOW) == "non-https url"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} passed")
