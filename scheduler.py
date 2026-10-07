"""Periodic re-search: re-lists chosen brand x sub groups on a schedule so the discovery history (and with it the
launch radar in match.py) stays fresh. server.py drives it (one Job of kind "schedule" at a time, which also
keeps it from running beside a user's own search/collect); everything here is plain and testable.

Needs the server to be running: a schedule that comes due while it is off runs shortly after the next start.
Scheduled runs never use the 1-day candidate cache (it would hide exactly the new models we look for).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

import catalog

logger = logging.getLogger("gauge")

MIN_INTERVAL_H = 6.0  # politeness: never hammer the brand sites
MAX_INTERVAL_H = 24 * 60.0
DEFAULT_INTERVAL_H = 24 * 7.0
MAX_SCHEDULES = 10
MAX_COMBOS = 400  # brand x country x sub listings per run (they run one after another)
LIMIT_MIN, LIMIT_MAX = 5, 100
CHECK_EVERY_S = 60
STARTUP_DELAY_S = 45  # let the server settle (and a user start working) before an overdue schedule kicks in
LOG_KEEP = 20


def plan(brands: list[str], subs: list[str], regions: list[str]) -> list[tuple[str, str, str]]:
    """(brand, country, sub) listings a run would perform: only combinations an adapter supports."""
    out = []
    for brand in brands:
        for region in regions:
            for cc in catalog.countries_of(region):
                supported = catalog.supported(brand, cc)
                out.extend((brand, cc, s) for s in subs if s in supported)
    return out


def normalize(cfg: dict, known_brands) -> dict:
    """Validated schedule config {name, brands, subcategories, regions, limit, interval_h, combos}.
    Raises ValueError with a message the UI can show."""
    name = str(cfg.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("이름을 입력하세요.")
    brands = list(dict.fromkeys(cfg.get("brands") or []))
    if not brands or any(b not in known_brands for b in brands):
        raise ValueError("브랜드를 하나 이상, 사용 가능한 것으로 선택하세요.")
    subs = list(dict.fromkeys(cfg.get("subcategories") or []))
    if not subs or any(not catalog.is_sub(s) for s in subs):
        raise ValueError("소분류를 하나 이상 올바르게 선택하세요.")
    regions = list(dict.fromkeys(cfg.get("regions") or [catalog.DEFAULT_REGION]))
    if any(not catalog.is_region(r) for r in regions):
        raise ValueError("알 수 없는 시장입니다.")
    try:
        limit = int(cfg.get("limit") or 30)
        interval = float(cfg.get("interval_h") or DEFAULT_INTERVAL_H)
    except (TypeError, ValueError):
        raise ValueError("숫자 입력이 올바르지 않습니다.") from None
    if not LIMIT_MIN <= limit <= LIMIT_MAX:
        raise ValueError(f"브랜드당 후보 수는 {LIMIT_MIN}~{LIMIT_MAX} 사이여야 합니다.")
    if not MIN_INTERVAL_H <= interval <= MAX_INTERVAL_H:
        raise ValueError(f"재검색 간격은 {MIN_INTERVAL_H:g}시간 이상 {MAX_INTERVAL_H:g}시간 이하여야 합니다 "
                         "(사이트 부담을 줄이기 위한 최소 간격).")
    combos = plan(brands, subs, regions)
    if not combos:
        raise ValueError("선택한 브랜드·소분류·시장을 지원하는 어댑터가 없습니다.")
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"한 번에 {len(combos)}개 조합은 너무 많습니다. {MAX_COMBOS}개 이하로 줄이세요.")
    return {"name": name, "brands": brands, "subcategories": subs, "regions": regions, "limit": limit,
            "interval_h": interval, "combos": len(combos)}


def is_due(row: dict, now: datetime) -> bool:
    if not row.get("enabled"):
        return False
    if not row.get("next_run"):
        return True
    try:
        return datetime.fromisoformat(row["next_run"]) <= now
    except ValueError:
        return True


def schedule_next(finished: datetime, interval_h: float) -> str:
    return (finished + timedelta(hours=interval_h)).isoformat(timespec="seconds")


def run(cfg: dict, store, search_fn: Callable, progress: Optional[Callable[[int, int, str], None]] = None,
        cancel: Optional[threading.Event] = None, now_fn: Callable[[], datetime] = datetime.now) -> dict:
    """Re-list every brand of the config (cache bypassed) and summarise. One brand failing never stops the others.
    `search_fn` has service.search's signature. Summary status: ok | partial | failed | cancelled."""
    started = now_fn()
    progress = progress or (lambda *_: None)
    by_brand: dict[str, list[tuple[str, str]]] = {}
    for brand, cc, sub in plan(cfg["brands"], cfg["subcategories"], cfg["regions"]):
        by_brand.setdefault(brand, []).append((cc, sub))
    before = store.count_new_discoveries()
    total, seen, failed, log, cancelled = len(by_brand), 0, [], [], False
    for i, (brand, pairs) in enumerate(by_brand.items()):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        progress(i, total, f"{brand} 조회 중 ({i + 1}/{total})")
        try:
            found, rows = search_fn([brand], sorted({s for _, s in pairs}), cfg["limit"], store=store,
                                    use_cache=False, countries=sorted({c for c, _ in pairs}))
            seen += len(found)
            bad = [m for _, status, m in rows if status == "failed"]
            if bad and not found:
                failed.append(brand)
            log.extend(f"{b}: {m}" for b, status, m in rows if status != "skipped")
        except Exception as exc:  # noqa: BLE001 - isolate per brand
            logger.exception("scheduled search failed for %s", brand)
            failed.append(brand)
            log.append(f"{brand}: 실패 ({type(exc).__name__})")
        progress(i + 1, total, f"{brand} 완료")
    finished = now_fn()
    status = ("cancelled" if cancelled else "failed" if total and len(failed) == total
              else "partial" if failed else "ok")
    return {"status": status, "started": started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"), "duration_s": round((finished - started).total_seconds()),
            "brands": total, "brands_failed": failed, "candidates": seen,
            "new_discoveries": store.count_new_discoveries() - before, "cancelled": cancelled,
            "log": log[-LOG_KEEP:]}


class Ticker(threading.Thread):
    """Calls `tick_fn` every CHECK_EVERY_S seconds (after a startup delay) on a daemon thread."""

    def __init__(self, tick_fn: Callable[[], None], every: float = CHECK_EVERY_S, first_delay: float = STARTUP_DELAY_S):
        super().__init__(daemon=True, name="gauge-scheduler")
        self.tick_fn, self.every, self.first_delay = tick_fn, every, first_delay
        self._halt = threading.Event()

    def run(self) -> None:
        if self._halt.wait(self.first_delay):
            return
        while not self._halt.is_set():
            try:
                self.tick_fn()
            except Exception:  # noqa: BLE001 - a failing tick must not kill the scheduler
                logger.exception("scheduler tick failed")
            if self._halt.wait(self.every):
                return

    def stop(self) -> None:
        self._halt.set()
