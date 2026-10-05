# Brand search app

## Run
```
pip install streamlit pyyaml
set PYTHONIOENCODING=utf-8
streamlit run app.py
```
Flow: brand -> product group (only Refrigerator enabled) -> price band (Budget/Mid/Premium terciles or custom USD thresholds; no-price items go to "Price unknown") -> Search -> tick candidates (default first N per band, max 10 total) -> Collect details (background thread, progress, live log, Cancel) -> results table, POD compare matrix, Excel download (saved under `output/search_*.xlsx`).

Operating-mode extraction (local LLM) is an optional checkbox, default off; it is slow.

## Browser mode (sidebar)
- Auto (headless, fallback visible): sets `FRIDGE_HEADLESS=1`, `FRIDGE_BROWSER_MODE=auto`.
- Headless only: `FRIDGE_HEADLESS=1`, `FRIDGE_BROWSER_MODE=headless`.
- Visible only: `FRIDGE_HEADLESS=0`, `FRIDGE_BROWSER_MODE=visible`. **Browser windows will pop up on this PC while collecting.**

The env vars are set when you press Collect details. Adapters read `FRIDGE_HEADLESS` via `common.launch_browser`; the visible fallback in Auto mode is the adapters' responsibility.

## Add a brand
1. Write `<brand>_us.py` exposing `discover(category, limit)` and `scrape(url)` (contract in `catalog.py`).
2. Add `- name: Brand` / `module: brand_us` to `brands.yaml` (registered into `catalog.ADAPTERS` at startup).

To add a product group, add it to `categories` in `brands.yaml` with `enabled: true` (adapters must support the category key).

## Cache
`data/cache.db` (SQLite): products 7 days, candidate listings 1 day. Delete the file to force a refresh. Cached products skip scraping; modes are only extracted for cached products when the checkbox is on and none were stored.

## Tests
`python tests/test_store.py`, `python tests/test_service.py` (plain asserts, fake adapter, includes a Streamlit AppTest smoke).

## Known limits
- Max 10 products per collection; 1.5 s polite delay between live scrapes.
- Cancel takes effect between products (an in-flight scrape finishes).
- Progress refreshes by a 1 s sleep + `st.rerun()` loop; interacting with the page mid-run just triggers a refresh.
- Closing the browser tab does not stop the worker thread.
- Band thresholds for presets are computed from the discovered set, not the market.
