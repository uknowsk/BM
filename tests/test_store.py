"""Plain-assert tests (pytest not installed). Run: python tests/test_store.py"""
import sqlite3
import sys
from contextlib import closing
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog import Candidate
from schema import DocumentRecord, ModeRecord, ProductRecord, RawSpec
import store
from store import Store


def _data():
    p = ProductRecord(brand="X", model_number="M1", product_name="X M1", product_url="http://x/1",
                      price_usd=999.0, pod_features=["Ice", "WiFi"], wifi_supported=True)
    d = [DocumentRecord(brand="X", model_number="M1", doc_type="Manual", source_url="http://x/m.pdf",
                        local_path="downloads/m.pdf", sha256="a" * 64, size_bytes=5)]
    r = [RawSpec(brand="X", model_number="M1", source="web", key="Width", value="36 in")]
    m = [ModeRecord(brand="X", model_number="M1", mode_name="Vacation", description="d", source_doc="m.pdf")]
    return p, d, r, m


def test_product_round_trip_and_modes_none():
    p, d, r, m = _data()
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        assert s.get_product("http://x/1") is None
        s.put_product("http://x/1", p, d, r)
        hit = s.get_product("http://x/1")
        assert hit.product == p and hit.documents == d and hit.raw_specs == r and hit.modes is None
        s.put_product("http://x/1", p, d, r, m)
        assert s.get_product("http://x/1").modes == m
        s.put_product("http://x/2", p, d, r, [])
        assert s.get_product("http://x/2").modes == []  # empty list != never extracted


def test_product_ttl():
    p, d, r, _ = _data()
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        t0 = datetime(2026, 1, 1)
        s.put_product("u", p, d, r, now=t0)
        assert s.get_product("u", now=t0 + timedelta(days=6, hours=23)) is not None
        assert s.get_product("u", now=t0 + timedelta(days=8)) is None
        assert s.get_product("u", ttl=timedelta(days=30), now=t0 + timedelta(days=8)) is not None


def test_candidates_cache_ttl_and_key():
    cs = [Candidate(brand="X", model_number="M1", name="n", url="u1", price_usd=10.0),
          Candidate(brand="X", model_number="M2", name="n2", url="u2")]
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        t0 = datetime(2026, 1, 1)
        s.put_candidates("X", "refrigerator", cs, now=t0)
        assert s.get_candidates("X", "refrigerator", now=t0 + timedelta(hours=23)) == cs
        assert s.get_candidates("X", "refrigerator", now=t0 + timedelta(days=2)) is None
        assert s.get_candidates("Y", "refrigerator", now=t0) is None
        assert s.get_candidates("X", "vacuum", now=t0) is None


def test_candidates_cache_is_per_country_and_keeps_new_fields():
    us = [Candidate(brand="X", model_number="U", name="n", url="u1", price_usd=10.0)]
    kr = [Candidate(brand="X", model_number="K", name="n", url="k1", region="kr", country="kr", currency="KRW",
                    price_local=2e6, attrs={"width_in": 35.8}, attrs_src={"width_in": "listing"})]
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_candidates("X", "refrigerator", us, subcategory="french_door")
        assert s.get_candidates("X", "refrigerator", subcategory="french_door", country="kr") is None
        s.put_candidates("X", "refrigerator", kr, subcategory="french_door", country="kr")
        assert s.get_candidates("X", "refrigerator", subcategory="french_door", country="kr") == kr
        assert s.get_candidates("X", "refrigerator", subcategory="french_door") == us  # default country 'us'
    assert Store._key("X", "r", 5, "s") == Store._key("X", "r", 5, "s", "us")  # US key format unchanged


def test_corrupt_rows_self_heal():
    p, d, r, m = _data()
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_product("u", p, d, r, m)
        for col, bad in (("spec_json", "{not json"), ("spec_json", '{"brand": 1}'), ("docs_json", "[{}]"),
                         ("raw_specs_json", "nope"), ("modes_json", '[{"x": 1}]'), ("fetched_at", "garbage")):
            s.put_product("u", p, d, r, m)
            with closing(sqlite3.connect(s.path)) as c, c:
                c.execute(f"UPDATE products SET {col}=? WHERE url='u'", (bad,))
            assert s.get_product("u") is None, col
        s.put_candidates("X", "refrigerator", [Candidate(brand="X", model_number="M", name="n", url="u")], limit=30)
        with closing(sqlite3.connect(s.path)) as c, c:
            c.execute("UPDATE candidates SET candidates_json='[{\"bad\": 1}]'")
        assert s.get_candidates("X", "refrigerator", limit=30) is None


def test_parser_version_invalidates_old_rows():
    import store
    p, d, r, _ = _data()
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_product("u", p, d, r)
        assert s.get_product("u") is not None
        saved = store.PARSER_VERSION
        store.PARSER_VERSION = "999"
        try:
            assert s.get_product("u") is None  # written by an older parser
        finally:
            store.PARSER_VERSION = saved
        assert store.PARSER_VERSION == saved == "5"  # restored after the temporary bump


def test_migrates_legacy_db_without_parser_version():
    p, d, r, _ = _data()
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "c.db"
        with closing(sqlite3.connect(path)) as c, c:
            c.executescript("""CREATE TABLE products(
                url TEXT PRIMARY KEY, brand TEXT, model_number TEXT, spec_json TEXT, docs_json TEXT,
                raw_specs_json TEXT, modes_json TEXT, fetched_at TEXT, content_hash TEXT);""")
            c.execute("INSERT INTO products VALUES('old','X','M1',?,'[]','[]',NULL,?,'h')",
                      (p.model_dump_json(), datetime.now().isoformat(timespec="seconds")))
        s = Store(path)
        assert s.get_product("old") is None  # legacy row ignored
        s.put_product("new", p, d, r)
        assert s.get_product("new") is not None


def test_candidate_key_includes_limit_and_empty_not_cached():
    cs = [Candidate(brand="X", model_number="M1", name="n", url="u1")]
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_candidates("X", "refrigerator", cs, limit=30)
        assert s.get_candidates("X", "refrigerator", limit=30) == cs
        assert s.get_candidates("X", "refrigerator", limit=10) is None
        s.put_candidates("Y", "refrigerator", [], limit=30)
        assert s.get_candidates("Y", "refrigerator", limit=30) is None  # empty results are never cached


def test_candidate_key_includes_subcategory():
    fd = [Candidate(brand="X", model_number="M1", name="n", url="u1", subcategory="french_door")]
    sbs = [Candidate(brand="X", model_number="M2", name="n", url="u2", subcategory="side_by_side")]
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_candidates("X", "refrigerator", fd, limit=30, subcategory="french_door")
        s.put_candidates("X", "refrigerator", sbs, limit=30, subcategory="side_by_side")
        assert s.get_candidates("X", "refrigerator", limit=30, subcategory="french_door") == fd
        assert s.get_candidates("X", "refrigerator", limit=30, subcategory="side_by_side") == sbs
        assert s.get_candidates("X", "refrigerator", limit=30, subcategory="top_freezer") is None
        assert s.get_candidates("X", "refrigerator", limit=30) is None  # legacy key is a different entry


def test_product_extra_specs_round_trip():
    p = ProductRecord(brand="X", model_number="W1", product_name="W", product_url="http://x/w", category="washer",
                      subcategory="front_load", extra_specs={"Spin speed (rpm)": "1300", "Steam": "Yes"})
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_product("w", p, [], [])
        hit = s.get_product("w").product
        assert hit == p and hit.extra_specs["Steam"] == "Yes" and hit.subcategory == "front_load"


def test_update_modes_keeps_fetched_at():
    p, d, r, m = _data()
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        t0 = datetime(2026, 1, 1)
        s.put_product("u", p, d, r, now=t0)
        s.update_modes("u", m)
        hit = s.get_product("u", now=t0 + timedelta(days=1))
        assert hit.modes == m and hit.fetched_at == t0.isoformat(timespec="seconds")
        assert s.get_product("u", now=t0 + timedelta(days=8)) is None  # modes update did not extend the TTL


def test_candidate_key_embeds_parser_version():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "c.db")
        cs = [Candidate(brand="X", model_number="M", name="n", url="u")]
        s.put_candidates("X", "refrigerator", cs, limit=30)
        assert s.get_candidates("X", "refrigerator", limit=30) == cs
        saved = store.PARSER_VERSION
        store.PARSER_VERSION = "999"
        try:
            assert s.get_candidates("X", "refrigerator", limit=30) is None  # listing cached under another parser version
        finally:
            store.PARSER_VERSION = saved
        assert s.get_candidates("X", "refrigerator", limit=30) == cs


def test_image_fields_round_trip_and_update_product_keeps_age():
    p, d, r, m = _data()
    p.image_url, p.image_path = "https://images.salsify.com/a.jpg", "downloads/images/x/M1.jpg"
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        t0 = datetime(2026, 1, 1)
        s.put_product("u", p, d, r, now=t0)
        hit = s.get_product("u", now=t0)
        assert hit.product.image_url == p.image_url and hit.product.image_path == p.image_path
        q = p.model_copy(update={"image_path": "downloads/images/x/M1.png"})
        s.update_product("u", q)
        hit = s.get_product("u", now=t0)
        assert hit.product.image_path == "downloads/images/x/M1.png" and hit.fetched_at == t0.isoformat(timespec="seconds")
        assert hit.documents == d and hit.raw_specs == r
        s.update_product("missing", q)  # no row: no-op, no error


def test_stale_scr_subcategory_is_a_miss_not_a_crash():
    p, d, r, _ = _data()
    stale = p.model_copy(update={"category": "cooking", "subcategory": "scr"})
    ok = p.model_copy(update={"category": "cooking", "subcategory": "sco", "product_url": "http://x/2"})
    with tempfile.TemporaryDirectory() as t:
        s = Store(Path(t) / "c.db")
        s.put_product("u-scr", stale, d, r)
        s.put_product("u-sco", ok, d, r)
        assert s.get_product("u-scr") is None  # retired sub key: re-scrape
        assert s.get_product("u-sco").product.subcategory == "sco"
        old = Candidate(brand="X", model_number="M", name="n", url="http://x/c", category="cooking", subcategory="scr")
        s.put_candidates("X", "cooking", [old], subcategory="scr")
        assert s.get_candidates("X", "cooking", subcategory="scr") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
