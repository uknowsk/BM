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
ORIGINAL wording; `method` = override | seed | exact | registry | embed | llm | fallback | new | rule | derived), `group` / `group_ko` + `child`
(an item exploded from a list value keeps its parent group; `child` = a learned item, always long-tail; a group's rows are contiguous and
shown once under a collapsible sub-header).
Accessories / consumables (part-number items such as `UXWORXR30 - 30" Never Scrub Roller Rack`, `Optional ...`, kits, prices) go to the last
section `액세서리·옵션` (never core, collapsed, still listed in the Mapping sheet); a feature only an optional accessory provides (an optional
air-fry basket) stays on its own row (`Air fry` = Yes) but is noted `옵션(액세서리): ...`, never as built in. Countable facts are derived from
item lists (`1 Heavy-Duty Offset Oven Rack | 2 Heavy-Duty Roller Racks` -> `Oven racks (count)` = 3, note `derived from item list: ...`, source
method `derived`; an explicit `Number of Oven Racks` wins).
`compare_model.build_compare()` returns a dict subclass with `.canon_stats[major]` (per-stage label counts `override/seed/exact/registry/embed/llm/
fallback/new/rule`, `embed_stage` = used | unavailable | offline | not_needed, `embed_ok/embed_fail/embed_model`, `line_ko`). The API group
should carry it as `canon_stats` (the 비교 tab shows `임베딩 단계: ...`, warning colour when degraded); the Excel `Mapping` sheet shows it in row 2.
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
compatible and discriminating words such as width/height, fridge/freezer, bake/broil, net/gross, lock/alarm, microwave/oven block a merge;
so do a physical-part noun the target lacks and labels that share one word but each add their own ('Slow Roast' vs 'Slow Cook'); learned
merges the guards refuse today are dropped from the registry on load) -> a strict token+difflib fallback (method `fallback`) when LM Studio
is unreachable (never blocks). List items (cooking modes, features) use the same pipeline in their
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
  disabled = dashed chip + "준비 중"; 중동/아시아/오세아니아 collapsed behind "+"), and the brand multi-select popover: brands grouped by family (`/api/brands[].group`), a search box (name or family, accent-insensitive),
  per-group "전체/해제", adapter countries as small tags (`/api/brands[].countries`) and a "n/30" count (selected brands usable in the chosen markets).
  Brands not sold in the
  selected regions (`regions[].brands`) are disabled with "선택한 출향지 미판매"; brands without an adapter yet show "준비 중". Without `/api/regions` (404) a built-in list is used (only 북미 enabled).
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

## 내 분류양식 (classification form): organise competitors by YOUR rows (`template.py`, `template_writer.py`)
Upload an Excel form listing the categories and items you care about; the collected products are then organised by THAT form
(your rows, your category order) instead of Gauge's own taxonomy. Files: `template.py` (parse + bind), `template_writer.py` (filled
.xlsx), `make_template_sample.py` (`python make_template_sample.py` writes `data/template_sample.xlsx` = cooking, 40 items / 5
categories, and `data/template_sample_refrigerator.xlsx`, 30 items), `web/js/template.js` + `web/css/template.css`, `tests/test_template.py`.

**Form format** (`.xlsx` only; header names are matched case-insensitively in KO/EN, e.g. `구분|분류|Category`, `항목|항목명|Item`,
`동의어|유의어|Synonyms`, `단위|Unit`, `유형|타입|Type`, `비고|메모|Notes`): `구분 | 항목(KO) | Item(EN) | 동의어/Synonyms | 단위/Unit | 유형/Type | 비고/Notes`.
Only the item column is required. Synonyms are split on `,` `|` `;`; type = `숫자`(number) / `예/아니오`(flag) / `텍스트`(text) / `목록`(list), inferred
from unit and label when blank (a warning says so). Also accepted: one item column with category header rows (merged / bold / filled /
bulleted, or a blank 구분 that carries the last non-empty one forward), a 2-level `대분류 / 중분류` pair, several sheets (the sheet name,
e.g. `냉장고` / `조리기기` / `SCO`, picks the product group; an unnamed first sheet takes the largest remaining group), and a form with no header row
(the text column is used). Rejected: `.xls` / `.xlsm` / macros, > 5 MB, > 3000 rows, zip bombs (uncompressed > 60 MB or ratio > 100), non-xlsx.
Formula cells are never evaluated (ignored with a warning); all cell text is data; strings are capped.

**Binding** (per form item, never inventing a value): 1 exact / normalised match on the item label or your synonyms (synonyms win) against the
canonical row labels and every product's source wording; 2 `canon.canonicalize` id; 3 embedding similarity (>= 0.84) with the same value-kind /
unit-family guards and `canon.conflicts` words (strict token fallback when LM Studio is down); 4 borderline 0.72-0.84 -> local LLM yes/no, cached in
`data/templates/_cache.json`. Cell status: `found` / `absent` (the spec says No) / `unknown` (nothing found - NOT absent) / `derived` (e.g. rack count
from an item list); numbers are converted to the form's unit (in<->mm<->cm, cu ft<->L, lb<->kg, W<->kW, F<->C); unqualified items bound to upper / lower
cavity rows show every part and are flagged; `needs_review` = LLM / fuzzy match, derived value, unit mismatch. Competitor rows the form does not cover
are listed with a suggested form category (same section name > embedding > token > LLM).

**API** (same rules as the rest: POST/DELETE need the same-origin `Origin`, errors are 422 with a plain message):
`POST /api/template` raw `.xlsx` bytes with `X-Filename` (URL-encoded) or JSON `{filename, content_base64}` -> `{template_id, summary:{filename, item_count,
categories:[{name,items}], sheets:[{name,group,group_ko,sub,items,categories}], warnings:[...], preview:[first 30 items]}}`;
`GET /api/template/{id}` (summary), `DELETE /api/template/{id}`, `GET /api/template/sample?group=cooking|refrigerator` (generated in memory),
`POST /api/template/{id}/apply {job_id}` (a finished collect job; 409 otherwise) -> `{counts:{found,absent,unknown,derived,review,total}, coverage:[per category],
sheets:[{name, group, counts, products, rows:[{item, canon_id, method, score, matched_label, needs_review, cells:[{status,value,display,unit,source_label,source_value,
sources,method,score,needs_review,note}]}], row_count, uncovered:[{id,section,key_en,key_ko,values,suggested_category,suggest_method}], uncovered_count}], warnings,
download_url}` (preview = first 120 rows), `GET /api/template/{id}/download?job_id=` -> the filled `.xlsx`. Forms live in `data/templates/<16 hex>.xlsx`
(+ `.json` meta; id validated, paths confined, max 20, oldest evicted; git-ignored; `FRIDGE_TEMPLATE_DIR` overrides).

**Filled workbook** (`write_filled`): a copy of YOUR workbook (widths, merges, styles, validations kept; non-arithmetic formulas become text) with one column per
product appended after the last used column (header: picture, brand, model, price, market - rows above the header row when there are >= 4, else one multi-line
header cell with the picture), found = number (+unit format) / check mark, absent = `–`, unknown = grey italic `정보 없음`, derived = blue fill, cell comments hold the
original wording / source / method, an amber `검토` column at the far right, plus sheets `매칭 결과` (audit) and `양식 외 항목`. Strings are neutralised against formula injection.

**UI**: a collapsible `내 분류양식` panel above the results (upload with drag & drop or the file button, sample links, parsed summary + warnings + 30-row
preview, `이 제품들에 적용`, `채워진 엑셀 내려받기`, `양식 삭제`, coverage by category) and a `Gauge 기본 비교표 | 내 양식 기준` switch that swaps the comparison for the
bound table (options: original wording, review rows only, hide rows with no information; uncovered items in a details block). The form id is remembered in
`localStorage['gauge.tpl']`. Hook: one line in `renderResults` (`window.GaugeTemplate.mount(body, job, groups)`); no inline script, server text via `textContent` only.
