"""Streamlit app: pick brand -> product group -> price bands -> search -> select -> collect -> Excel.
Run: streamlit run app.py"""
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

import service
from store import DEFAULT_DB, Store

BROWSER_MODES = {
    "Auto (headless, fallback visible)": ("1", "auto"),
    "Headless only": ("1", "headless"),
    "Visible only": ("0", "visible"),
}
SPEC_COLS = ["brand", "model_number", "product_name", "price_usd", "door_style", "capacity_total_cuft",
             "width_in", "height_in", "depth_in", "weight_lb", "energy_kwh_year", "ice_maker",
             "water_dispenser", "wifi_supported"]


@dataclass
class Job:
    """Shared between the worker thread and the UI; the worker never touches st.*."""
    total: int
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: int = 0
    log: list = field(default_factory=list)
    finished: bool = False
    error: Optional[str] = None
    products: list = field(default_factory=list)
    run_log: list = field(default_factory=list)
    xlsx_path: Optional[Path] = None

    def on_progress(self, done: int, total: int, msg: str) -> None:
        with self.lock:
            self.done = done
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")


def _run_job(job: Job, cands, with_modes: bool, store: Store) -> None:
    try:
        products, docs, specs, modes, run_log = service.collect(
            cands, progress_cb=job.on_progress, cancel_event=job.cancel, with_modes=with_modes, store=store)
        job.products, job.run_log = products, run_log
        if products:
            job.xlsx_path = service.export_excel(products, docs, specs, modes, run_log)
    except Exception as exc:  # noqa: BLE001 - surface to UI instead of dying silently
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished = True


@st.cache_resource
def get_store() -> Store:
    return Store(os.environ.get("FRIDGE_DB", DEFAULT_DB))


def render_results(job: Job) -> None:
    st.subheader("4. 결과")
    if job.cancel.is_set():
        st.warning("사용자가 취소했습니다. 완료된 제품만 표시합니다.")
    if job.error:
        st.error(job.error)
    failed = [r for r in job.run_log if r[1] in ("failed", "partial")]
    for _, status, msg in failed:
        st.warning(f"{status}: {msg}")
    if not job.products:
        st.info("수집된 제품이 없습니다.")
        return
    df = pd.DataFrame([p.model_dump() for p in job.products])
    st.dataframe(df[[c for c in SPEC_COLS if c in df.columns]], use_container_width=True, hide_index=True)
    if job.xlsx_path and job.xlsx_path.exists():
        st.markdown("**POD 비교표 (POD Compare)**")
        try:
            st.dataframe(pd.read_excel(job.xlsx_path, sheet_name="POD_Compare"),
                         use_container_width=True, hide_index=True)
        except Exception as exc:  # noqa: BLE001 - sheet may be absent/empty
            st.caption(f"POD 비교표를 표시할 수 없습니다: {exc}")
        st.download_button("엑셀 다운로드 (Excel)", job.xlsx_path.read_bytes(), file_name=job.xlsx_path.name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def main() -> None:
    st.set_page_config(page_title="가전 제품 비교 수집기", layout="wide")
    st.title("가전 제품 검색 · 수집")
    cfg = service.load_config()
    ss = st.session_state

    with st.sidebar:
        st.header("설정")
        mode_label = st.selectbox("Browser mode", list(BROWSER_MODES))
        headless_env, mode_name = BROWSER_MODES[mode_label]
        if mode_name == "visible":
            st.caption("브라우저 창이 이 PC에 실제로 열립니다.")
        limit = st.number_input("브랜드당 후보 수 (limit)", 5, 100, 30, step=5)
        use_cache = st.checkbox("후보 목록 캐시 사용 (1일)", value=True)

    # 1-2. brand / product group
    st.subheader("1. 브랜드 · 제품군")
    brands = st.multiselect("브랜드", [b["name"] for b in cfg["brands"]], default=[b["name"] for b in cfg["brands"]][:1])
    enabled = [c for c in cfg["categories"] if c.get("enabled")]
    cat = st.radio("제품군", [c["key"] for c in enabled], format_func=lambda k: next(c["label"] for c in enabled if c["key"] == k),
                   horizontal=True)
    soon = [c["label"] for c in cfg["categories"] if not c.get("enabled")]
    if soon:
        st.caption("준비 중 (coming soon): " + " · ".join(soon))

    # 3. price band
    st.subheader("2. 가격대 분류")
    band_mode = st.radio("분류 방식", ["preset", "custom"], horizontal=True,
                         format_func=lambda m: "프리셋 (보급 ~ 프리미엄, 5단계)" if m == "preset" else "직접 입력 (USD 경계값)")
    thresholds: list[float] = []
    if band_mode == "custom":
        raw = st.text_input("경계값 (쉼표로 구분, 예: 1000, 1500, 2000, 3000)", "1000, 1500, 2000, 3000")
        try:
            thresholds = [float(x) for x in raw.replace("$", "").split(",") if x.strip()]
        except ValueError:
            st.error("숫자만 입력하세요.")
    per_band = st.number_input("밴드별 기본 선택 개수 (N)", 1, service.MAX_SELECTED, 3)

    # 4. search
    if st.button("검색 (Search)", type="primary", disabled=not brands):
        with st.spinner("후보 제품 검색 중..."):
            cands, log = service.search(brands, cat, int(limit), store=get_store(), use_cache=use_cache)
        ss.candidates, ss.search_log, ss.job = cands, log, None
        for k in [k for k in ss if str(k).startswith("sel_")]:
            del ss[k]
        for url in service.default_selection(service.classify_bands(cands, band_mode, thresholds), int(per_band)):
            ss[f"sel_{url}"] = True

    if "candidates" not in ss:
        return
    for brand, status, msg in ss.search_log:
        (st.caption if status == "ok" else st.warning)(f"{brand}: {msg}")
    if not ss.candidates:
        st.info("검색된 후보가 없습니다.")
        return

    # 5. candidates by band with checkboxes
    st.subheader("3. 후보 제품 선택")
    bands = service.classify_bands(ss.candidates, band_mode, thresholds)
    for name, lst in bands.items():
        with st.expander(f"{name} ({len(lst)})", expanded=bool(lst)):
            for c in lst:
                price = f"${c.price_usd:,.0f}" if c.price_usd is not None else "가격 미확인"
                st.checkbox(f"{c.brand} · {c.model_number} · {c.name} · {price}", key=f"sel_{c.url}")
    selected = [c for c in ss.candidates if ss.get(f"sel_{c.url}")]
    over = len(selected) > service.MAX_SELECTED
    st.write(f"선택됨: {len(selected)} / 최대 {service.MAX_SELECTED}")
    if over:
        st.error(f"최대 {service.MAX_SELECTED}개까지 선택할 수 있습니다.")
    with_modes = st.checkbox("Extract operating modes with local LLM (slow)", value=False)

    job: Optional[Job] = ss.get("job")
    running = job is not None and not job.finished
    if st.button("상세 수집 (Collect details)", disabled=over or not selected or running):
        os.environ["FRIDGE_HEADLESS"], os.environ["FRIDGE_BROWSER_MODE"] = headless_env, mode_name
        job = ss.job = Job(total=len(selected))
        threading.Thread(target=_run_job, args=(job, selected, with_modes, get_store()), daemon=True).start()
        running = True

    if job is None:
        return
    # progress / live log / cancel
    with job.lock:
        done, log_lines = job.done, list(job.log)
    st.progress(min(done / max(job.total, 1), 1.0), text=f"{done} / {job.total}")
    st.code("\n".join(log_lines[-15:]) or "...", language=None)
    if running:
        if st.button("취소 (Cancel)"):
            job.cancel.set()
        time.sleep(1)
        st.rerun()
    else:
        render_results(job)


main()
