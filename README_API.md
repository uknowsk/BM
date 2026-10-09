# Gauge API (product groups x 32 brands)

Local only (127.0.0.1:8765). POST needs `Origin: http://127.0.0.1:8765` (unchanged). `FRIDGE_MOCK=1` serves fake data.

Product groups (`catalog.CATEGORY_TREE`): `refrigerator` (french_door, side_by_side, top_freezer, bottom_freezer,
built_in, compact), `washer` (top_load, front_load, dryer, laundry_center), `cooking` (microwave, sco, otr, gas_oven,
gas_cooktop, electric_oven, induction, radiant). Korean labels live in the same tree (`SCO_LABEL_KO`, one place). `sco` = Speed Cook Oven; the retired key `scr` is now unknown (422 / ValueError).

| sub key | what it classifies |
|---|---|
| `sco` | Speed Cook Oven: any oven with a built-in microwave / speed-cook / light-wave function |
| `gas_oven` | gas ranges with an oven (and gas wall ovens where they exist) |
| `gas_cooktop` | gas cooktops / rangetops without an oven (Korean "가스레인지") |
| `radiant` | electric ranges and cooktops with radiant / coil elements |
| `induction` | induction ranges and cooktops |
| `electric_oven` | non-speed wall ovens (single / double) |
| `microwave` | countertop / built-in microwaves (not OTR, not SCO) |
| `otr` | over-the-range microwaves |

## GET /api/categories
`[{"key":"washer","label_ko":"세탁기","enabled":true,"children":[{"key":"front_load","label_ko":"드럼","enabled":true,"brands":["Samsung","LG"]}, ...]}]`
`enabled` = at least one ready brand supports the child.

## GET /api/brands
`[{"name":"Samsung","enabled":true,"note":"","categories":["french_door",...],"majors":["refrigerator","washer"],"group":"Samsung·LG","countries":["us","kr"]}, ...]`
32 brands in display order (`brands.yaml` / `catalog.BRAND_META`): Samsung, LG, KitchenAid, GE, Whirlpool, Bosch, then the 26 new ones
(Maytag, JennAir, Amana, Thermador, Gaggenau, Siemens, Frigidaire, Electrolux, AEG, Café, Monogram, Haier, Fisher & Paykel, Viking, Sub-Zero,
Wolf, Miele, Smeg, Liebherr, Bertazzoni, De Dietrich, Beko, Hisense, Panasonic, Brastemp, Consul).
- `group` = brand family for the picker: `Samsung·LG`, `Whirlpool Corp.`, `BSH`, `Electrolux`, `Haier·GE`, `프리미엄`, `글로벌`.
- `countries` = countries (order us, kr, de, uk, fr, br) for which an adapter module actually exists and imports; `[]` while the brand has none.
- A brand is `enabled` only if at least one adapter module exists, imports and (for `?region=`) sells there. A brand whose module file does not
  exist yet is not an error: `enabled:false, note:"준비 중", categories:[], countries:[]`. Selecting it in `POST /api/search` -> 422.

### Adapter module naming (`catalog.module_name`)
`<brand_slug>_<country>.py` where `brand_slug` = ASCII-fold, lower-case, drop everything except letters/digits (one rule, `catalog.brand_slug`);
country codes `us, kr, de, uk, fr`. The six original brands keep their `ADAPTERS` US modules (`samsung_us`, ...). A module is used only when the file
exists (`importlib.util.find_spec`); other countries are auto-discovered, nothing to register.

| brand | slug | | brand | slug |
|---|---|---|---|---|
| Maytag | `maytag` | | Viking | `viking` |
| JennAir | `jennair` | | Sub-Zero | `subzero` |
| Amana | `amana` | | Wolf | `wolf` |
| Thermador | `thermador` | | Miele | `miele` |
| Gaggenau | `gaggenau` | | Smeg | `smeg` |
| Siemens | `siemens` | | Liebherr | `liebherr` |
| Frigidaire | `frigidaire` | | Bertazzoni | `bertazzoni` |
| Electrolux | `electrolux` | | De Dietrich | `dedietrich` |
| AEG | `aeg` | | Beko | `beko` |
| Café | `cafe` | | Hisense | `hisense` |
| Monogram | `monogram` | | Panasonic | `panasonic` |
| Haier | `haier` | | Fisher & Paykel | `fisherpaykel` |
| Brastemp | `brastemp` | | Consul | `consul` |

e.g. `cafe_us.py`, `fisherpaykel_uk.py`, `dedietrich_fr.py`, `siemens_de.py`.

## POST /api/search -> {"job_id"}
`{"brands":["Samsung","GE"],"subcategories":["front_load","induction"],"limit":30,"band_mode":"auto|custom","thresholds":[t1,t2,t3,t4]}` (`thresholds` only for `custom`: exactly 4 ascending, strictly increasing prices >= 0, <= 1,000,000, else 422)
Legacy `{"category":"refrigerator"}` still works (major = all sub keys the brand supports; a sub key also accepted).
Unsupported combos are skipped with a log line (`Samsung: 라디언트 미지원`). 422: unknown brand/sub, no supported combo.
`brands` takes up to 32 names. Cap: at most `MAX_SEARCH_COMBOS` = **120** (brand x country x sub-group) listings per search (422 above that; was 48);
they run one after another with per-combo progress. `GET /api/meta` reports `max_search_combos` and `max_brands`.
Job result (`GET /api/jobs/{id}` when done):
```
{"groups":[{"category":"washer","label_ko":"세탁기","bands":{"t1":[Candidate],"t2":[],"t3":[],"t4":[],"t5":[],"unknown":[]},
            "labels":{"t1":"보급","t2":"중저가","t3":"중가","t4":"중고가","t5":"프리미엄","unknown":"Price unknown"},
            "thresholds":[c1,c2,c3,c4],"total":12}],
 "bands":<group bands or null>, "labels":..., "thresholds":..., "total":N, "failed_brands":[]}
```
`auto`: 5 tiers (same as the competitor match) from the quintile cut prices `thresholds` (`match.price_tiers`; a price equal to a cut goes to the
higher tier), computed per major category x region (`service.preset_thresholds`, once). With fewer than 5 priced items only n tiers are used
(n priced -> n tiers, n-1 cuts, `t1..tn` filled, the rest empty, `labels` of the used tiers are `"1/3단계"` ...; 0 or 1 priced -> `thresholds` `null`,
a lone item is `t1`). `custom`: the 4 given prices make 5 bands and `labels` are the price ranges (`"< $1,000"`, `"$1,000 - $1,500"`, ..., `">= $3,000"`).
Items without a price go to `unknown`. Top-level `bands/labels/thresholds` are populated only when a single
major was searched (else `null`). Candidate: `{brand,model_number,name,url,price_usd,category,subcategory}`.
Progress items: `{label:"Samsung 드럼",brand,subcategory,status}`.

## POST /api/collect -> {"job_id"}
`{"urls":[{"brand","url","model_number","name","price_usd","subcategory","category"?}],"with_modes":false}`
`subcategory` is REQUIRED for every item (422 if missing, unknown, or unsupported by the brand); the major is derived from it and
a client `category` is never trusted (422 if it disagrees). The sub the user picked is authoritative for the collected product
(`product.subcategory`; `product.category` = its major) even if the product page reports another one (warning logged).
Several models per brand (even in the same group/market) are allowed: comparison is by brand x product group x market. Overall safety cap per job: `service.MAX_COLLECT = 12` (422 above it; `/api/meta` returns `max_collect` and `max_selected`, both = MAX_COLLECT). Items may carry `country` (default `us`; 422 if unknown or unsupported by the brand) and optional `region` (must agree with the country), `price_local`, `currency`.
Result adds `groups`: `[{"category","label_ko","products":[...],"documents":[...],"modes":[...],"pod":{"rows":[...]}}]`
(tree order, one POD matrix per major). Top-level `products/documents/raw_specs/modes/run_log` hold everything; top-level
`pod.rows` is filled only when a single major was collected. Products carry `subcategory` and `extra_specs`
(English label -> string); fridge-only fields stay null for other groups.

## Excel
Products (one row per product, `extra_specs` as "k: v; ..."), Documents, Raw_Specs, Modes, **Category_Specs**
(brand, model_number, product_name, category, subcategory, key, value), Schema, POD_Items, `POD_Compare` (one major) or
`POD_Compare_<category>` per major (never mixed), Run_Log.

## Adapter contract
`discover(subcategory, limit=30)` (raise ValueError for unsupported sub keys), `scrape(url)`, optional
`SUPPORTED_SUBCATEGORIES: set[str]` (missing => the five fridge keys). Cache keys include the sub key (and the country when not `us`).
Adapters are keyed by (brand, country): `<brand>_us` modules are country `us`; a module named `<brand lower-case>_<country>`
(`samsung_kr`, `lg_kr`, `samsung_de`, `lg_uk` ...) is auto-discovered with importlib when it exists (no registration). A module serves ONE
country; its `discover` returns Candidates with `price_local` + `currency` (`price_usd=None`) for non-US markets and may fill `attrs`
(standard units: `capacity_total_cuft`, `width_in`, `energy_kwh_year`, `energy_star`, `wifi`, `finish`, `fuel`, ...; unknown = key absent).
`service.search` stamps region/country. `catalog.supported(brand, country='us')`. New non-US domains must also be added to
`server.EXTRA_DOMAINS` (host allowlist; `lge.co.kr` is already there).

## Images, feature flags, transposed comparison
- `common.download_image(brand, model, url)` -> `downloads/images/<brand>/<model>.jpg|png` (https + `IMAGE_HOST_ALLOW`, public IPs,
  8 MB cap, magic bytes + Pillow decode, longest side <= 800 px). A plain-request 403 (Akamai: KitchenAid/Whirlpool) retries through
  headless Chromium (in-page `fetch`). `service.collect` downloads after the scrape, stores `image_path` in the cache and re-downloads
  when the file is missing; failures never fail the product.
- `GET /api/img/<brand>/<file>`: only `.jpg/.png` directly under `downloads/images/<brand>/`; nosniff, 7-day cache. Products carry `image_src`.
- `features.derive_flags/apply_flags`: 'Yes (<evidence>)' flags (Air fry, Convection, ...) from every spec key/value; overwrites a curated '-'/'No'.
- `compare_model.build_compare(...)` -> `{major: rows}` (products = columns, items = rows; sections 기본정보/스펙/기능/POD/동작 모드;
  multi-value specs exploded to one row per item). Collect result groups carry `compare`; the Excel first sheet `Compare`
  (`Compare_<major>` when several) is built from the same rows. `FRIDGE_PORT` overrides the server port.

## Regions, filters, facets (Phase A)
Regions (`catalog.REGIONS`): `kr` 한국, `na` 북미 (default), `eu` 유럽 (de, uk, fr, es, it), `sa` 남미, `me` 중동, `as` 아시아, `oc` 오세아니아.
A region is `enabled` only when some (brand, country) adapter exists: today only `na` (country `us`); the rest report `enabled:false, note:"준비 중"`.

### GET /api/regions
```
[{"key":"kr","label_ko":"한국","enabled":false,"default":false,"countries":["kr"],"enabled_countries":[],"currency":"KRW","brands":[],"note":"준비 중"},
 {"key":"na","label_ko":"북미","enabled":true,"default":true,"countries":["us","ca"],"enabled_countries":["us"],"currency":"USD",
  "brands":["Samsung","LG","KitchenAid","GE","Whirlpool","Bosch"],"note":""}, ...]
```
Order: kr, na, eu, sa, me, as, oc. `default` is true for `na` only.

### GET /api/categories?region=  /  GET /api/brands?region=
`region` = one key or a comma list (default `na`; unknown -> 422; a disabled region returns everything disabled). `/api/brands` items gain
`"regions":["na"]` (regions where the brand has an adapter, independent of the `region` argument).

### GET /api/filters?subcategory=french_door&region=na
`subcategory` is a sub key (type groups hidden: the sub already says it) or a major key (type groups `door_type`/`machine_type`/`cook_type`
shown). `region` is a comma list (default `na`). 422 for unknown sub/region.
```
{"subcategory":"french_door","region":"na","groups":[
 {"key":"brand","label_ko":"제조사(브랜드)","label_en":"Brand","type":"multi","level":"listing","unit":null,"display_units":[],
  "values":null,"regions":["*"],"default_open":true,"more_after":6,"bucketed":false},
 {"key":"price","label_ko":"가격대","type":"range","level":"listing","unit":"price","values":null,"bucketed":false, ...},
 {"key":"capacity_l","label_ko":"총용량","label_en":"Total capacity","type":"multi","level":"listing","unit":"L",
  "display_units":["L","cu ft"],"bucketed":true,
  "values":[{"key":"lt400","label_ko":"~400L (~14.1 cu ft)","min":null,"max":400},
            {"key":"400_500","label_ko":"400~500L (14.1~17.7 cu ft)","min":400,"max":500}, ...,
            {"key":"700_plus","label_ko":"700L~ (24.7 cu ft~)","min":700,"max":null}]},
 {"key":"width_class", ... "values":[{"key":"w36","label_ko":"36in급 (35~38in, 914mm급)","min":35,"max":38}, ...]},
 {"key":"energy", ...},{"key":"dispenser", ...},{"key":"smart", ...},{"key":"finish", ...}]}
```
Group `type`: `multi` (list of value keys; `values:null` = open list such as brands), `boolean` (selection `true`), `range` (price: `{"min","max"}`).
`level`: `listing` (usable right after search, from adapter attrs or the name) or `spec` (needs collected data; otherwise "unknown").
Buckets are half-open `[min,max)`; `min`/`max` are in the group `unit` (width classes in inches). Groups restricted to a region (`kr_grade`,
`eu_class`) appear only when that region is requested.
Groups: refrigerator `door_type` (major only), `capacity_l`, `width_class`, `energy`, `kr_grade`, `eu_class`, `dispenser`, `smart`, `finish`
(+ `install_type` for built_in; compact has its own capacity buckets); washer `machine_type` (major only), `capacity_kg`, `spin_rpm`, `steam`,
`smart`, `energy`, `kr_grade`, `eu_class`; cooking `cook_type` (major only), `fuel`, `width_class`, `oven_capacity`, `burners`, `convection`, `smart` (+ `microwave_power` for sco; sco hides `burners`/`fuel`).
Always: `brand`, `price`. Hidden but valid selection keys: `keyword` (name/model substring) and `region`.

### POST /api/search (additions, all optional)
```
{"brands":["Samsung","LG"],"subcategories":["french_door"],"regions":["na"],"limit":30,
 "filters":{"brand":["LG"],"capacity_l":["700_plus","__unknown__"],"smart":["wifi"],"steam":true,
            "price":{"min":1000,"max":3000},"keyword":"bespoke"}}
```
- `regions`: 1..4 unique keys, each enabled, default `["na"]`. The plan is brand x country (countries of the regions) x sub; unsupported
  combos are skipped with a log line. 422 otherwise (`지원하지 않는 지역입니다.`).
- `filters`: groups AND together, values OR. The pseudo value `"__unknown__"` in a group's list also keeps candidates whose value for that
  group is unknown (default: unknown candidates are excluded once a group is selected). 422 for unknown key/value, wrong shape, a key not
  offered for the searched subs/regions, more than 20 groups, more than 50 values, strings over 64 chars, price outside 0..1e9 or min > max.
  Filters narrow the candidates only: adapter calls and caches are unchanged.
- Result additions: `regions`, `filters` (echo), `facets`, `filtered_total` (== `total`), `unfiltered_total`, `region_totals`
  (`{"na": 12}`, filtered counts); every group gains `region` and `currency`. Groups are per major x region (tree order, then request
  order); with one region they are exactly as before plus those two keys. Bands/terciles never mix regions or currencies.
```
"facets":{"brand":{"Samsung":12,"LG":9,"__unknown__":0},
          "capacity_l":{"lt400":0,"400_500":1,"500_600":2,"600_700":8,"700_plus":5,"__unknown__":7},
          "steam":{"true":3,"__unknown__":20}, "price":{"__unknown__":2}}
```
Facets are disjunctive (a group's counts ignore its own selection but honour every other one), are computed over ALL candidates (not only the
filtered ones), list every bucket (0 included) plus `__unknown__`; `price` only has `__unknown__`; a `region` facet appears with more than one region.

Candidate JSON (new fields last):
```
{"brand":"Samsung","model_number":"RF28...","name":"28 cu. ft. 36\" Smart 3-Door French Door","url":"https://www.samsung.com/us/...",
 "price_usd":1799.0,"category":"refrigerator","subcategory":"french_door","region":"na","country":"us","currency":"USD","price_local":null,
 "attrs":{"capacity_total_cuft":28.0,"width_in":36.0,"wifi":true,"door_type":"french_door"},
 "attrs_src":{"capacity_total_cuft":"name","width_in":"name","wifi":"name","door_type":"name"}}
```
`attrs_src`: `listing` (adapter) | `name` (derived from the name: show as "이름에서 추정") | `detail`. Non-US candidates have `price_usd:null` and
`price_local` in `currency`; bands use `price_usd`, else `price_local` (custom band labels use the currency code instead of `$`).
Products gain `region`, `country`, `currency`, `price_local`.

## Competitor match (`match.py`, page `/match`; design: `docs/MATCHING_DESIGN.md`)
Works on the durable discovery history (`store.seen_models`, filled by every `service.search` / `collect`, seeded once from cached
listings; rows never expire). First sight of a (brand, country, sub) group is a baseline (`baseline=1`); only models that appear after a
COMPLETE listing (shorter than `limit`) count as new discoveries (`baseline=0`), so a larger `limit` never makes old models look new.
Standard signal keys (candidate `attrs` and `ProductRecord`; absent = unknown): `rating` (0-5), `review_count`, `is_new` (site flag only),
`release_date` ('YYYY-MM-DD'|'YYYY-MM'|'YYYY'), `release_src` (`site` | `distribution` (manufacturer first distribution date, GE/Cafe) | `doc` | `sitemap`).
Adapters that request the site's own newest-first sort also stamp `newest_rank` (1 = newest) and `newest_of` (length of that listing) with
`catalog.stamp_newest_order(cands)` (only when the sub maps to a single site category). `match.recency` uses them last (`basis: "site_order"`,
needs `newest_of >= 5`, never counts as a launch). Samsung BR stores the a-vista cash price as `price_local`; Panasonic BR is switched off.

### POST /api/match
```
{"sub":"electric_oven","country":"us","price":1800,
 "specs":{"width_in":30,"capacity_total_cuft":5.0,"fuel":"electric","burners":null,"features":["wifi","air_fry"]},
 "weights":{"price":35,"spec":30,"recency":20,"response":15},"band_pct":25,"tier_window":1,"top":20}
```
`sub` + `country` required (422 for unknown), `price` optional (without it nothing is price-ranked), `band_pct` 5..100, `tier_window` 0..4 or null
(keep only models within +-N price tiers), `top` 1..50, `weights` keys price/spec/recency/response with 0..100. Response:
`target` (+ `tier`, `tier_label`), `tiers` (5 quantile tiers of the sub group's prices: `{tier,label,min,max,count}`; fewer when there are
few prices; the whole major group is used when the sub has < 10 priced models, see `tier_scope`), `counts` (`pool, ranked,
excluded_by_spec, outside_tier_window, price_unknown`), `distrusted_new_flags` (groups whose site NEW flag marks >= 60 % of a listing are
ignored), `data` (readiness: models, with_price, with_rating, with_release_date, site_new_flagged, new_discoveries) and `results[]`:
`{brand, model_number, name, url, price, currency, tier, tier_label, tier_diff, total (0-100), coverage (0-1: share of the weights backed by
real data), is_launch, components:{price:{score,delta_pct}, spec:{score,hard_fail,details[]}, recency:{score,basis,months,evidence},
response:{score,rating,reviews,adjusted}}}`. Price = continuous proximity `max(0, 1 - |d|/band)^2` on top of the 5 tiers. Unknown parts score a
neutral value (never 0, never renormalised away); a width outside +-1.5 in of the wanted width excludes the model; models without a price are
not ranked when a target price is given (counted in `price_unknown`).

### POST /api/match/excel
Same body as `/api/match` plus optional `window` (1..60 months, default 12, the launch period of the launch sheets). Recomputes the ranking and the
launch radar from the same pool and answers an `.xlsx` (`Content-Disposition: attachment; filename="gauge_match_<sub>_<cc>_<time>.xlsx"`). Sheets:
`요약` (target, tier, band, specs, weights, counts, data readiness, caveat), `순위` (one row per ranked model: tier, total, coverage, each component
score with its evidence, rating/reviews, URL), `가격 5단계`, `신제품` (per brand with evidence), `신제품 트렌드`. Built in memory by `match_excel.build`;
scraped text is sanitised (formula-looking strings are stored as text). Same 422 / same-origin rules as `/api/match`.

### GET /api/launches?sub=&country=us&window=12
New models per brand with their evidence (`release_date` | `site_new` | `first_seen`) and a feature trend (share of new vs existing models with
Wi-Fi/convection/air fry/steam/ENERGY STAR, `delta_pts`, `low_sample`), `median_price`, `basis_counts`, `distrusted_new_flags`, `note`.

## Scheduled re-search (`scheduler.py`, section "자동 재검색" on `/match`)
Re-lists chosen brands x subs x markets at a fixed interval so new launches reach the discovery history (new models only show up
after a repeat search). Runs inside the server (needs it running; schedules that came due while it was off start shortly after the next
start); a run is an ordinary job of kind `schedule` in the single job slot, so it never runs beside a user's own search/collect (409
for the user while it runs; the ticker simply retries a minute later), uses a hidden browser only (no windows), bypasses the 1-day
candidate cache and shows progress/cancel like other jobs. Min interval 6 h, max 10 schedules, max 400 listings per run.
`FRIDGE_SCHEDULER=0` or `FRIDGE_MOCK=1` turn the runner off (schedules are only stored).
- `GET /api/schedules` -> `{"schedules":[{id,name,config:{brands,subcategories,regions,limit},interval_h,enabled,created_at,last_run,
  next_run,last_status(ok|partial|failed|cancelled),last_summary:{status,started,finished,duration_s,brands,brands_failed[],candidates,
  new_discoveries,cancelled,log[]},running,combos}],"runner":bool,"limits":{max_schedules,min_interval_h,max_interval_h,default_interval_h,
  limit_min,limit_max,max_combos}}`
- `POST /api/schedules` `{"name","brands":[..],"subcategories":[..],"regions":["na"],"limit":30,"interval_h":168}` -> the schedule (first
  run at the next tick); 422 with a Korean message for unknown brand/sub/region, interval < 6 h, no supported combination, too many combos.
- `POST /api/schedules/{id}/toggle` `{"enabled":bool}`, `POST /api/schedules/{id}/run` -> `{"job_id"}` (409 while another job runs; 422 in
  mock mode), `POST /api/schedules/{id}/delete`. Unknown id -> 404. All mutations are POST (same-origin guard).

### Units (`units.py`)
`cuft_to_l/l_to_cuft`, `in_to_mm/mm_to_in`, `lb_to_kg/kg_to_lb`, `f_to_c/c_to_f`, `fmt_dual(value, kind, primary)` ->
`"28.0 cu ft (793 L)"`, `"36 in (914 mm)"`, `"300 lb (136 kg)"` (kinds `capacity|length|weight|temp`; primary `cuft|L|in|mm|lb|kg|F|C`).
Washer kg vs US drum cu ft and DOE/EU/KS energy figures are never converted.
