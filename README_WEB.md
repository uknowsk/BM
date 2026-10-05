# Gauge web UI

Single-page web app over `service.py` (search -> select -> collect -> compare -> Excel).

    pip install fastapi uvicorn      # already installed here (fastapi 0.141, uvicorn 0.52)
    python server.py                 # http://127.0.0.1:8765  (loopback only)
    FRIDGE_MOCK=1 python server.py   # fake brands/products/jobs, no network, no LLM

Files: `server.py` (API + static), `web/index.html`, `web/css/app.css`, `web/js/app.js`, `web/js/theme.js`, `web/css/fonts.css` + `web/fonts/`, `tests/test_server.py`.
Test: `python tests/test_server.py` (FastAPI TestClient, mock mode).

## API
- `GET /api/brands`, `/api/categories`, `/api/meta`
- `POST /api/search` `{brands, category, limit, band_mode:'auto'|'custom', thresholds:[lo,hi]|null, browser_mode}` -> `{job_id}`
- `POST /api/collect` `{urls:[{brand,url,...}], with_modes, browser_mode}` -> `{job_id}`; max `MAX_PER_BRAND = 1` URL per brand (400 otherwise)
- `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`, `GET /api/jobs/{id}/excel`, `GET /api/doc?path=downloads/...`

Safety: only known brands, https URLs on the brand's own domain, host/origin checks, documents served only from `downloads/`,
one running job at a time (409; a job declared stale after 30 min without progress is marked `error` + `stale`, can no longer
write status/result, and the slot stays locked (409) until its thread has really exited — a truly hung worker needs a server restart), `FRIDGE_HEADLESS` / `FRIDGE_BROWSER_MODE` set from the chosen browser mode before each job.
Env: `FRIDGE_MOCK`, `FRIDGE_OUTPUT_DIR` (default `output/`), `FRIDGE_DB`, `FRIDGE_TESTING=1` (also accept Host `testserver`; tests only).

Security notes: POSTs need `Origin` exactly `http://127.0.0.1:<port>` or `http://localhost:<port>` (and `Sec-Fetch-Site` same-origin/none if sent).
CSP forbids inline scripts (theme bootstrap lives in `web/js/theme.js`) and all third-party origins; `/api/doc` serves `.pdf` only, with
`Content-Security-Policy: sandbox` (if a browser's built-in PDF viewer refuses to render under it, relax `DOC_CSP` in `server.py`, or use the download).
PDF downloads (`common.download_pdf`) only follow https links on `common.DOWNLOAD_HOST_ALLOW` (redirects re-checked, private IPs rejected).
Job errors shown in the UI are exception type names only; details go to the server log. A job with no progress for 30 min is marked failed.

Fonts are self-hosted (IBM Plex Sans KR / Plex Mono / Instrument Serif woff2 in `web/fonts/`, `web/css/fonts.css`).
Regenerate with `python fetch_fonts.py` (one-off; needs internet).
Cache: `store.PARSER_VERSION` is stored with each product row; bump it when adapter output changes to invalidate old cache rows.


## UI cascade (brand -> product group -> sub-category)
Contract the UI expects: `/api/brands[].categories` (sub-category keys), `/api/categories[].children[{key,label_ko}]`,
`POST /api/search {brands, subcategories, limit, band_mode, thresholds, browser_mode}` -> `{groups:[{category,label_ko,bands,thresholds}]}`
(legacy `{bands}` still works), collect items carry `category`/`subcategory`; (v2 UI: several models per brand are allowed; the cap is `/api/meta.max_selected`);
products may carry `extra_specs` (object or list) and results may carry `groups[{category,label_ko,products,pod:{rows}}]`.
Selections persist in localStorage (`gauge.sel`, `gauge.recent`). Mock UI: run `FRIDGE_MOCK=1 python server.py` and open `/?mock=1`;
`web/js/mock.js` is served (and its script tag injected) ONLY when the server runs with `FRIDGE_MOCK=1` (404 otherwise). Mock uses
`gauge.mock.*` storage keys, shows no Excel download, and LG's 2nd model per sub-category fails to scrape.

## Comparison tab (canonical rows)
The 비교 tab renders the group's `compare` rows (same rows as the Excel `Compare` sheet). A row is one CANONICAL attribute
(`canon.py`): `Size` / `Dimensions` / `Overall dimensions` / `크기` are one row, a composite `W x H x D` string becomes width /
height / depth rows, `Convection Bake` / `Convect Bake` / `컨벡션 베이크` share a row. Row fields: `id` (canonical id, e.g.
`width`, `bake-wattage:upper`), `section` (fixed taxonomy: 기본정보 / 치수·무게 / 용량 / 전기·에너지 / 성능 / 기능 / 디자인 / 연결성 /
조리(오븐·쿡탑) / 세탁·건조 / 냉각·신선 / 보증·기타), `key_en`/`key_ko`, `unit`, `values[]`, `kind`, `differs`, `same` (identical for every
product: shown quiet), `notes[]`, `core`, `uncertain` (+ `method`) and `sources[][]` = per product `{label, value, method, score, via?}` (the
ORIGINAL wording; `method` = override | seed | exact | embed | llm | new).
UI: image row on top, section index (구분), 필터 box, `다른 값만 보기`, `전체 항목 보기` (default = 핵심 항목 only; long-tail rows are added,
collapsed per section), `English 항목명` (KO/EN label), `원문 항목명 보기` (each product's original wording under its value; clicking a
label opens the per-product source list, hover shows it as tooltip) and a `검토` badge on rows whose mapping is uncertain (LLM decided,
or an embedding match < 0.90). Results without `compare` fall back to the older 사양 비교 table.

## Canonical spec mapping (`canon.py`)
Pipeline (first hit wins): user overrides -> normalization (lowercase, strip (R)(TM), unit parentheses, `Section > Label` leaf, stems,
`EN(...)` wrappers, upper/lower/second-cavity and burner-position qualifiers become an id suffix) -> seed ontology
`data/canon_seed.json` (~235 attributes, ~1600 EN/KO synonyms mined from the fixtures; this file is the committed source of truth) ->
registry `data/canon_registry.json` -> embeddings (LM Studio `text-embedding-bge-m3`, fallback `text-embedding-qwen3-embedding-0.6b`;
cosine >= 0.84 merges, 0.72-0.84 asks `qwen/qwen3-8b`, below creates a NEW attribute; numeric / flag / text kind and unit family must be
compatible and discriminating words such as width/height, fridge/freezer, bake/broil, net/gross, lock/alarm block a merge) -> a strict
token+difflib fallback when LM Studio is unreachable (never blocks). List items (cooking modes, features) use the same pipeline in their
own item space.

Runtime caches (git-ignored, recreated when missing): `data/canon_registry.json` (learned labels, new attributes, LLM verdicts - mappings stay
stable across runs) and `data/embed_cache.json` (one embedding per text, float16). Env: `FRIDGE_CANON_DIR` (data dir), `FRIDGE_CANON_OFFLINE=1`
(no LM Studio; also implied by `FRIDGE_MOCK=1` and by running a `tests/test_*.py` script). `python canon_check.py` prints precision / recall of
`tests/fixtures/canon_cases.json` against the real embeddings (the unit test uses a deterministic fake embedder).

### Fixing a wrong mapping: `data/canon_overrides.json`
Overrides win over everything else. Excel's `Mapping` sheet (canonical id, labels, section, core, per product source label / value /
method / score, `검토` flag) shows what to fix. Example:

```json
{
  "merge": [
    {"labels": ["Overall Appliance Size", "Gesamtmaße"], "into": "dimensions"},
    {"labels": ["Fridge volume"], "into": "capacity-fridge", "category": "refrigerator"},
    {"ids": ["door-alarm"], "into": "door-lock"}
  ],
  "split": [
    {"labels": ["Net capacity"], "as": "net-capacity", "label_en": "Net capacity", "label_ko": "순 용량", "section": "용량", "kind": "numeric"}
  ]
}
```
`merge`: every listed label (or every label that resolves to one of `ids`) goes to the canonical id `into` (optionally only for one
`category`). `split`: the labels get their own canonical id `as` (created from `label_en` / `label_ko` / `section` / `kind` if it does not
exist) and are never merged with the seed attribute they would otherwise match. Core vs long tail is set per attribute in the seed
(`core`: list of majors); new attributes are long tail.

## v2 shell (default): mega-menu + filter panel + regions (Phase A, docs/DESIGN_FILTERS.md)
`localStorage['gauge.ui']='v1'` brings back the old 4-step stepper (`#setup`); anything else renders the v2 shell. Both share the same
state/search/collect/results code, so v1 stays a safe fallback until the v2 shell is signed off.

- **Top bar** (`#subbar`): 출향지(시장) chips from `GET /api/regions` (`[{key,label_ko,enabled,note,brands[]}]`; default 북미, 1-4 selectable,
  disabled = dashed chip + "준비 중"; 중동/아시아/오세아니아 collapsed behind "+"), and the brand multi-select popover. Brands not sold in the
  selected regions (`regions[].brands`) are disabled with "선택한 출향지 미판매". Without `/api/regions` (404) a built-in list is used (only 북미 enabled).
- **Rail** (`#rail`): major groups; hover (120 ms in / 250 ms out, diagonal-safe because the fly-out is a child of the rail) or focus opens the fly-out with
  that group's sub-types (multi-select checkboxes + "지원 항목 전체 선택"). Keyboard: Up/Down/Home/End between groups, Right/Enter/Space into the fly-out,
  Up/Down/Home/End inside it, Left/Esc back (and close). Touch (`hover:none`) and < 900px: inline accordion; < 900px the rail is a slide-in drawer
  (focus trap, Esc/scrim/완료 close, focus returns to the 제품군 button).
- **Search** is explicit: the 검색 button posts `{brands, subcategories, regions, limit, band_mode, thresholds, browser_mode}` (`regions` only when the server
  has `/api/regions`). Changing regions/brands/subs after a search shows a "조건이 바뀌었습니다" banner instead of re-searching.
- **Filter panel** (`#fpanel`): groups = synthetic ones built from the loaded candidates (제조사, 소분류, 출향지 if >1, 가격대 min/max) + groups from
  `GET /api/filters?subcategory=<sub|major>&region=na,kr` (`multi` / `range` buckets or min-max / `boolean`; keys `brand,region,price,keyword,
  door_type,machine_type,cook_type` are skipped as duplicates of the menu/top bar). Counts are disjunctive facet counts computed client-side
  over the loaded candidates (the server `facets` field is not used); filtering never calls the network. Candidate values are read from
  `cand.attrs[group.key]`, then `group.source[]` paths (`attrs.` prefix stripped); numeric values go into bucket `min <= v < max`
  (`max: null` = open end). A candidate with no value is *unknown*: hidden once the group has a selection unless the group's "미확인 포함" box is
  checked (the count of unknowns is shown). `level:"spec"` groups carry a 스펙 badge. < 1100px the panel is a bottom sheet (필터 button, live apply).
- **Selection bar**: chips `Brand · 소분류 · model · 시장`, counter `n / max` (`/api/meta.max_selected`, default 12); candidates are keyed by URL, so several
  models per brand work. Collect items additionally carry `region`/`country` when the candidate has them.
- **Depends on the backend's final shapes**: field names of `/api/regions` and `/api/filters`, `Candidate.region/country/price_local/currency/attrs`,
  whether `/api/collect` still rejects several models of one brand, and the real `attrs` keys (must match each filter's `key`/`source`).
- Mock (`/?mock=1` with `FRIDGE_MOCK=1`): `web/js/mock.js` also answers `/api/regions` (한국 + 북미 enabled), `/api/filters`, candidates with `region/attrs`.
