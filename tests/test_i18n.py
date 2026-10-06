"""Plain-assert tests (pytest-compatible), fully offline: the LLM is a fake or switched off.
Run: python tests/test_i18n.py
Generic source-language -> English translation (de/fr glossaries, cache, LLM fallback) and the ko_en compatibility."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import i18n
import ko_en


class FakeLLM:
    def __init__(self, table=None, fail=False):
        self.table, self.fail, self.prompts = table or {}, fail, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            return None
        return {k: v for k, v in self.table.items() if k in prompt}


def fresh(lang, llm=None):
    """A Translator on the real glossary with a temp cache file and a fake LLM (never the network)."""
    tmp = tempfile.TemporaryDirectory()
    t = i18n.Translator(lang, cache_path=Path(tmp.name) / f"{lang}_cache.json", chat=llm or FakeLLM(fail=True))
    t._tmp = tmp  # keep the directory alive
    return t


def test_glossaries_are_large_and_english_only():
    for lang, floor in (("de", 300), ("fr", 300)):
        t = i18n.get(lang)
        sizes = t.glossary_sizes()
        assert sizes["labels"] >= floor and sizes["values"] >= 100 and sizes["counters"] >= 20, (lang, sizes)
        raw = json.loads(t.glossary_path.read_text(encoding="utf-8"))
        for sect in ("labels", "values", "counters"):
            for k, v in raw[sect].items():
                assert isinstance(v, str) and v.strip() and "\n" not in v, (lang, k)
                assert not any(c in v for c in "äöüßéèêàçôîûœ"), (lang, k, v)  # translations are English


def test_german_labels_and_values_without_llm():
    t = fresh("de")
    for src, en in (("Breite", "Width"), ("Höhe", "Height"), ("Hoehe", "Height"), ("Tiefe", "Depth"),
                    ("Fassungsvermögen", "Capacity"), ("Energieeffizienzklasse", "Energy efficiency class"),
                    ("Backofen", "Oven"), ("Kochfeld", "Hob"), ("Induktion", "Induction"), ("Dunstabzug", "Extractor hood"),
                    ("Breite:", "Width"), ("  breite ", "Width")):
        assert t.translate_key(src) == en, src
    assert t.translate_key("Höhe (mm)") == "Height (mm)"
    assert t.translate_key("Gesamtvolumen (Liter)") == "Total capacity (L)"
    assert t.translate_value("Edelstahl") == "Stainless steel" and t.translate_value("Ja") == "Yes"
    assert t.translate_value("Edelstahl, schwarz") == "Stainless steel, Black"
    assert t.translate_value("4 Kochzonen") == "4 cooking zones" and t.translate_value("60 Liter") == "60 L"
    assert t.translate_value("Ja / Nein") == "Yes / No"


def test_french_labels_and_values_without_llm():
    t = fresh("fr")
    for src, en in (("Largeur", "Width"), ("Hauteur", "Height"), ("Capacité", "Capacity"), ("capacite", "Capacity"),
                    ("Classe énergétique", "Energy class"), ("Four", "Oven"), ("Table de cuisson", "Hob"),
                    ("Induction", "Induction"), ("Hotte", "Extractor hood")):
        assert t.translate_key(src) == en, src
    assert t.translate_key("Hauteur (mm)") == "Height (mm)"
    assert t.translate_key("Capacité nette (litres)") == "Net capacity (L)"
    assert t.translate_value("Acier inoxydable") == "Stainless steel" and t.translate_value("Inox, noir") == "Stainless steel, Black"
    assert t.translate_value("4 foyers") == "4 burners" and t.translate_value("60 litres") == "60 L"


def test_numbers_and_units_pass_through():
    for lang in ("de", "fr"):
        t = fresh(lang)
        for s in ("595 x 1.850 x 688 mm", "230 V", "1.200 W", "45 dB(A)", "12,5 kg", "1 299,00 €", "50 Hz", "A+++"):
            assert t.translate_value(s) == s, (lang, s)
    assert fresh("de").translate_value("") == "" and fresh("de").translate_many([None, "Breite"], "label") == ["", "Width"]


def test_unknown_stays_unchanged_when_llm_off_or_unreachable():
    llm = FakeLLM(fail=True)
    t = fresh("de", llm)
    assert t.translate_key("Zwiebelschneider") == "Zwiebelschneider"  # never invented
    assert not t.cache_path.exists()  # nothing cached on failure
    os.environ["FRIDGE_I18N_LLM"] = "0"
    try:
        calls = len(llm.prompts)
        assert fresh("fr", llm).translate_value("Quelquechose d'inconnu") == "Quelquechose d'inconnu"
        assert len(llm.prompts) == calls  # switched off: the LLM is not even asked
    finally:
        os.environ.pop("FRIDGE_I18N_LLM")


def test_llm_fallback_is_batched_validated_and_cached():
    llm = FakeLLM({"Zwiebelschneider": "Onion slicer", "Brotkasten": "Bread bin\nextra", "Kaffeemühle": "Kaffeemühle ist gut"})
    t = fresh("de", llm)
    out = t.translate_many(["Breite", "Zwiebelschneider", "Brotkasten", "Zwiebelschneider", "Kaffeemühle"], "label")
    assert out[0] == "Width" and out[1] == out[3] == "Onion slicer"
    assert out[2] == "Brotkasten"  # multi-line reply rejected -> original kept
    assert len(llm.prompts) == 1 and "German" in llm.prompts[0] and "Breite" not in llm.prompts[0]  # one call; glossary hit not sent
    cache = json.loads(t.cache_path.read_text(encoding="utf-8"))
    assert "Onion slicer" in cache.values()
    llm2 = FakeLLM(fail=True)
    t2 = i18n.Translator("de", cache_path=t.cache_path, chat=llm2)  # new process: disk cache answers, no LLM call
    assert t2.translate_key("Zwiebelschneider") == "Onion slicer" and llm2.prompts == []


def test_scripted_language_reply_rejected_by_untranslated_hook():
    llm = FakeLLM({"Foo": "한글"})
    tmp = tempfile.TemporaryDirectory()
    t = i18n.Translator("de", cache_path=Path(tmp.name) / "c.json", chat=llm, untranslated=ko_en._has_hangul)
    assert t.translate_key("Foo") == "Foo"
    tmp.cleanup()


def test_registry_and_validation():
    assert set(i18n.LANGUAGES) == {"ko", "de", "fr"} and i18n.get("de") is i18n.get("de")
    assert i18n.get("de").cache_path.name == "de_cache.json" and i18n.get("fr").glossary_path.name == "glossary_fr.json"
    try:
        i18n.get("xx")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert i18n.translate_key("fr", "Largeur") == "Width" and i18n.translate_value("de", "Weiß") == "White"
    assert i18n.translate_many("de", ["Breite", "Höhe"], "label") == ["Width", "Height"]


def test_fold_rules():
    assert i18n.fold("Höhe") == i18n.fold("Hoehe") == "hoehe" and i18n.fold("Größe") == "groesse"
    assert i18n.fold("Classe Énergétique:") == "classeenergetique" and i18n.fold("A B") == "ab"


def test_ko_en_is_a_compatible_wrapper():
    t = i18n.get("ko")
    assert t is ko_en.TRANSLATOR and t.lang == "ko"
    tmp = tempfile.TemporaryDirectory()
    saved = (ko_en.CACHE_PATH, ko_en._chat, dict(ko_en._cache_mem))
    ko_en.CACHE_PATH, ko_en._chat = Path(tmp.name) / "ko_cache.json", FakeLLM(fail=True)
    ko_en._cache_mem.clear()
    try:
        assert ko_en.translate_key("총 용량") == "Total capacity" and i18n.translate_key("ko", "총 용량") == "Total capacity"
        assert ko_en.translate_value("3등급") == "KR grade 3"  # Korean-specific rules stay in ko_en
        assert ko_en.translate_value("알 수 없는 문자열") == "알 수 없는 문자열"
    finally:
        ko_en.CACHE_PATH, ko_en._chat = saved[0], saved[1]
        ko_en._cache_mem.clear()
        ko_en._cache_mem.update(saved[2])
        tmp.cleanup()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
