"""Plain-assert tests (pytest-compatible). Run: python tests/test_scheduler.py
Scheduled re-search: config validation, due logic, run summaries, schedule storage, ticker (no network)."""
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler
from catalog import Candidate
from store import Store

BRANDS = {"Samsung", "LG", "Amana", "GE"}
NOW = datetime(2026, 10, 7, 12, 0, 0)


def cfg(**kw):
    return {"name": "주간 조리기기", "brands": ["Samsung", "LG"], "subcategories": ["electric_oven", "gas_oven"],
            "regions": ["na"], "limit": 30, "interval_h": 168, **kw}


# ------------------------------------------------------------------ validation
def test_normalize_accepts_a_good_config_and_counts_combos():
    out = scheduler.normalize(cfg(), BRANDS)
    assert out["name"] == "주간 조리기기" and out["limit"] == 30 and out["interval_h"] == 168.0
    assert out["combos"] == len(scheduler.plan(["Samsung", "LG"], ["electric_oven", "gas_oven"], ["na"])) > 0
    assert scheduler.normalize(cfg(interval_h=None, regions=None), BRANDS)["interval_h"] == scheduler.DEFAULT_INTERVAL_H


def test_normalize_rejects_bad_input_with_a_message():
    bad = [cfg(name="  "), cfg(brands=[]), cfg(brands=["Nope"]), cfg(subcategories=[]), cfg(subcategories=["nope"]),
           cfg(regions=["zz"]), cfg(limit=1), cfg(limit=500), cfg(interval_h=1), cfg(interval_h=10 ** 6),
           cfg(limit="x"), cfg(brands=["Amana"], subcategories=["sco"])]  # Amana sells no speed-cook oven: no combo
    for c in bad:
        try:
            scheduler.normalize(c, BRANDS)
        except ValueError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"accepted {c}")


def test_normalize_caps_the_number_of_listings():
    old = scheduler.MAX_COMBOS
    scheduler.MAX_COMBOS = 1
    try:
        try:
            scheduler.normalize(cfg(), BRANDS)
        except ValueError as exc:
            assert "너무 많습니다" in str(exc)
        else:
            raise AssertionError("cap not enforced")
    finally:
        scheduler.MAX_COMBOS = old


def test_plan_only_lists_supported_combinations():
    amana = scheduler.plan(["Amana"], ["sco", "gas_oven", "radiant"], ["na"])
    assert {(b, s) for b, _, s in amana} == {("Amana", "gas_oven"), ("Amana", "radiant")}
    assert all(cc == "us" for _, cc, _ in amana)


# ------------------------------------------------------------------ due logic
def test_is_due_rules():
    due = lambda **kw: scheduler.is_due({"enabled": True, "next_run": None, **kw}, NOW)
    assert due() and due(next_run=(NOW - timedelta(minutes=1)).isoformat()) and due(next_run="garbage")
    assert not due(next_run=(NOW + timedelta(minutes=1)).isoformat()) and not due(enabled=False)
    assert scheduler.schedule_next(NOW, 24) == "2026-10-08T12:00:00"


# ------------------------------------------------------------------ run summaries
def _cand(brand, i, sub="electric_oven"):
    return Candidate(brand=brand, model_number=f"{brand[:2]}{i}", name=f"Oven {brand}{i}", url=f"https://x.test/{brand}/{i}",
                     price_usd=1000.0 + i, category="cooking", subcategory=sub, country="us")


def test_run_records_history_bypasses_the_cache_and_counts_new_models():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.db")
        state = {"n": 4, "calls": []}

        def fake_search(brands, subs, limit, store=None, use_cache=True, countries=None):
            state["calls"].append((tuple(brands), tuple(subs), use_cache, tuple(countries)))
            found = [_cand(brands[0], i, subs[0]) for i in range(state["n"])]
            store.record_seen(found, limit=limit)
            return found, [(brands[0], "ok", f"{len(found)} candidates")]

        config = scheduler.normalize(cfg(brands=["Samsung", "LG"], subcategories=["electric_oven"]), BRANDS)
        first = scheduler.run(config, store, fake_search, now_fn=lambda: NOW)
        assert first["status"] == "ok" and first["brands"] == 2 and first["candidates"] == 8
        assert first["new_discoveries"] == 0  # first look is the baseline
        assert all(c[2] is False for c in state["calls"])  # the 1-day candidate cache is never used
        assert {c[0] for c in state["calls"]} == {("Samsung",), ("LG",)} and state["calls"][0][3] == ("us",)
        state["n"] = 5  # one model appeared in each brand's listing
        second = scheduler.run(config, store, fake_search, now_fn=lambda: NOW)
        assert second["new_discoveries"] == 2 and second["candidates"] == 10 and second["log"]


def test_run_isolates_brand_failures_and_reports_status():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.db")
        config = scheduler.normalize(cfg(brands=["Samsung", "LG"], subcategories=["electric_oven"]), BRANDS)

        def flaky(brands, subs, limit, **kw):
            if brands[0] == "LG":
                raise RuntimeError("boom")
            return [_cand("Samsung", 1)], [("Samsung", "ok", "1 candidates")]

        partial = scheduler.run(config, store, flaky)
        assert partial["status"] == "partial" and partial["brands_failed"] == ["LG"] and partial["candidates"] == 1

        def broken(brands, subs, limit, **kw):
            raise RuntimeError("boom")

        assert scheduler.run(config, store, broken)["status"] == "failed"

        def all_rows_failed(brands, subs, limit, **kw):
            return [], [(brands[0], "failed", "TimeoutError")]

        assert scheduler.run(config, store, all_rows_failed)["status"] == "failed"
        stop = threading.Event()
        stop.set()
        cancelled = scheduler.run(config, store, flaky, cancel=stop)
        assert cancelled["status"] == "cancelled" and cancelled["cancelled"] and cancelled["candidates"] == 0


def test_run_reports_progress_per_brand():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.db")
        config = scheduler.normalize(cfg(brands=["Samsung", "LG"], subcategories=["electric_oven"]), BRANDS)
        seen = []
        scheduler.run(config, store, lambda b, s, l, **kw: ([], []), progress=lambda i, n, m: seen.append((i, n)))
        assert seen == [(0, 2), (1, 2), (1, 2), (2, 2)]


# ------------------------------------------------------------------ storage
def test_schedule_storage_roundtrip_and_updates():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.db")
        created = store.create_schedule("abc123", "주간", {"brands": ["LG"]}, 168.0, NOW.isoformat(), now=NOW)
        assert created["enabled"] is True and created["config"] == {"brands": ["LG"]} and created["last_run"] is None
        assert created["last_summary"] is None and created["interval_h"] == 168.0
        store.update_schedule("abc123", enabled=False, last_run="2026-10-07T13:00:00", next_run="2026-10-14T13:00:00",
                              last_status="ok", last_summary={"new_discoveries": 3})
        got = store.get_schedule("abc123")
        assert got["enabled"] is False and got["last_status"] == "ok" and got["last_summary"] == {"new_discoveries": 3}
        assert [s["id"] for s in store.list_schedules()] == ["abc123"]
        try:
            store.update_schedule("abc123", name="x")
        except ValueError:
            pass
        else:
            raise AssertionError("only whitelisted fields may change")
        assert store.delete_schedule("abc123") is True and store.delete_schedule("abc123") is False
        assert store.get_schedule("abc123") is None and store.list_schedules() == []


# ------------------------------------------------------------------ ticker
def test_ticker_ticks_survives_errors_and_stops():
    count = {"n": 0}

    def tick():
        count["n"] += 1
        if count["n"] == 1:
            raise RuntimeError("first tick fails")

    t = scheduler.Ticker(tick, every=0.02, first_delay=0.01)
    t.start()
    end = time.time() + 3
    while count["n"] < 3 and time.time() < end:
        time.sleep(0.01)
    t.stop()
    t.join(2)
    assert count["n"] >= 3 and not t.is_alive()
    t2 = scheduler.Ticker(tick, every=0.02, first_delay=30)  # stopped before the first tick: never ticks
    before = count["n"]
    t2.start()
    t2.stop()
    t2.join(2)
    assert count["n"] == before


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
