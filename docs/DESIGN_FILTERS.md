# DESIGN_FILTERS: 메가메뉴 + 우측 필터 패널 + 판매 지역(Region) 설계

상태: Proposed (ADR 형식 요약은 1.0). 대상: Gauge (FastAPI + vanilla JS, `fridge_scraper/`).
기준 코드: `catalog.py`(CATEGORY_TREE, Candidate, adapter registry), `service.py`(search/collect), `server.py`(/api/*),
`web/js/app.js`(brand -> group -> sub -> price 스테퍼), `ge_us.py`/`samsung_us.py`/`lg_us.py`(discover).
참고 사이트(Enuri)는 레이아웃 참고용일 뿐이며 스크레이핑하지 않는다.

## 0. ADR 요약

| 항목 | 결정 |
|---|---|
| Context | 현재 UI는 4단계 스테퍼. 제품군이 늘고(냉장고/세탁/조리) 지역(북미/한국/유럽/남미) 비교가 필요하다. |
| Decision 1 | 스테퍼(brand->group->sub->price)를 "좌측 메가메뉴 + 상단 Region/Brand 바 + 우측 필터 패널 + 결과 영역" 한 화면으로 교체. 단 search/collect/results 컴포넌트와 API는 재사용. |
| Decision 2 | 필터는 `catalog.FILTER_SCHEMA`(서버 단일 출처)로 선언. 각 필터에 `level: listing|spec` 부여. |
| Decision 3 | 지역은 `catalog.REGIONS`. 어댑터는 `REGIONS_SUPPORTED` + `discover(sub, limit, region)`. 지역 간 비교는 로컬 가격+통화 보존, 단위는 정규화 레이어로 양쪽 단위 병기. |
| Consequence | 리스팅 필터는 즉시(후보 단계), 스펙 필터는 상세 수집 후/상위 N 지연 적용. "알 수 없음(unknown)" 버킷이 UI 상 항상 존재. |
| Rejected | (a) 리테일식 혜택/배송/카드할인 필터: 벤치마크 목적과 무관. (b) 모든 후보 상세 선수집 후 필터: 사이트 부하/Akamai 차단 위험. (c) 환율 실시간 API: 외부 의존, 재현성 저하. |

비목표: 가격 비교 쇼핑 기능, 할인/혜택/배송/리뷰 필터, 실시간 환율.

---

## 1. 정보 구조 (IA)

### 1.1 영역 구성

1. 상단 바 (Top bar): 로고 / **판매 지역(Region) 멀티 칩** / **브랜드 멀티 셀렉트** / 검색 입력(모델·키워드) / 테마. 기존 `.top` 재사용.
2. 좌측 메가메뉴 (Category rail): 대분류 3개(냉장고, 세탁기/건조기, 조리기기) 세로 목록. 호버/포커스 시 오른쪽으로 플라이아웃(소분류). 소분류 클릭 = 제품군 확정.
3. 우측 필터 패널 (Filter panel): 선택된 소분류의 `FILTER_SCHEMA` 렌더. 그룹별 접기, 건수(count), "더보기", "초기화".
4. 결과 영역 (Result area, 중앙): 활성 필터 칩 바 + 정렬 + 후보 카드/테이블(기존 밴드 뷰) + 하단 sticky 수집 바(기존 `.bar`).

벤치마크용 차이점(리테일 사이트와 다름): 카드 할인, 무이자, 혜택가, 배송/설치, 리뷰 수 필터 없음. 대신 스펙 기준(용량 밴드, 폭 클래스, 에너지, 기능 보유 여부), 지역, "스펙 확인됨/미확인" 필터를 둔다.

### 1.2 데스크톱 와이어프레임 (>= 1024px)

```
+--------------------------------------------------------------------------------------+
| Gauge 가전 벤치마크   [한국][북미][유럽][남미][+중동][+아시아][+오세아니아]  [브랜드 v] [검색__] [테마] |
+----------------+--------------------------------------------+------------------------+
| 제품군          |  냉장고 > 프렌치도어     결과 48건          | 필터            [초기화] |
| > 냉장고   [>]  |  활성: [북미 x][Samsung x][600L+ x]        | 제조사(브랜드)  접기 v   |
|   세탁기/건조기 |  정렬: 가격 낮은순 v     밴드: 자동3분위 v   |  [x] Samsung (12)       |
|   조리기기      |  ---- Budget -------------------------      |  [ ] LG (9)             |
|                |  [ ] Samsung RF28...   $2,399  609L  36in   |  [더보기 +3]            |
| +-플라이아웃-+ |  [x] LG LRFXS...       $2,799  ...          | 도어 타입               |
| | 프렌치도어  | |  ---- Mid ----                              |  [x] 프렌치도어 (12)    |
| | 사이드바이  | |  ...                                        |  [ ] 4도어 (5)          |
| | 상냉동      | |  ---- 가격 미확인 ----                       | 총용량 (L / cu ft)      |
| | 하냉동      | |                                              |  [ ]~500L  [ ]500~600L  |
| | 빌트인      | |                                              |  [x]600L~  [ ]미확인(7) |
| | 소형        | |                                              | 폭(width)  24/30/33/36in|
| +------------+ |                                              | 가격대 [min]~[max] USD  |
+----------------+----------------------------------------------+------------------------+
| 선택 2/10   예상 소요 50초   [동작모드 LLM] [브라우저 v]              [상세 수집 ->]      |
+--------------------------------------------------------------------------------------+
```

폭 배분: rail 220px 고정 / 필터 280px 고정 / 결과 가변. 1024~1279px에서는 필터 패널을 접이식(토글 버튼 "필터 (3)")으로 전환.

### 1.3 모바일 와이어프레임 (< 768px)

```
+-------------------------------+     드로어(좌, 햄버거)        필터 시트(하단, 풀높이)
| [=] Gauge        [필터 3] [테마]|    +---------------------+   +---------------------+
| 지역: [한국][북미][유럽][남미] > |    | 제품군               |   | 필터          [닫기] |
| 브랜드: [Samsung x][LG x] [+]  |    | v 냉장고             |   | 제조사 v            |
+-------------------------------+    |    프렌치도어        |   | 도어 타입 v         |
| 냉장고 > 프렌치도어  48건        |    |    사이드바이사이드  |   | ...                 |
| 활성: [북미 x][600L+ x]         |    | > 세탁기/건조기      |   | [결과 48건 보기]    |
| ---- Budget ----               |    | > 조리기기           |   +---------------------+
| [ ] Samsung RF28  $2,399       |    +---------------------+
| ...                            |
+-------------------------------+
| 선택 2/10         [상세 수집 ->] |
+-------------------------------+
```

- 지역 칩은 가로 스크롤 한 줄(스크롤 스냅). 칩 높이 >= 44px(터치 영역).
- 메가메뉴는 드로어 아코디언: 대분류 탭 = 펼침, 소분류 탭 = 선택 후 드로어 닫힘.
- 필터는 하단 시트. 시트 안에서 변경은 임시 상태, "결과 N건 보기"로 적용(건수 즉시 갱신).

### 1.4 메가메뉴 상호작용/접근성

| 입력 | 동작 |
|---|---|
| 마우스 hover (대분류) | 120ms 지연 후 플라이아웃 오픈, 대각선 이동 허용(떠남 지연 250ms). |
| 키보드 | 대분류는 `role="menubar"`가 아닌 단순 `<nav><ul>` + 각 항목 `<button aria-expanded aria-controls>` (메뉴 ARIA 패턴 과적용 금지). 포커스/Enter/Space/ArrowRight = 플라이아웃 오픈, ArrowUp/Down = 대분류 간 이동(roving tabindex, 기존 `roving()` 재사용), ArrowRight로 플라이아웃 첫 항목 이동, ArrowLeft/Escape = 닫고 대분류로 복귀. Home/End 지원. |
| 터치 (hover 없음, `@media (hover:none)`) | 첫 탭 = 펼침(`aria-expanded=true`), 두 번째 탭이 소분류 선택. 플라이아웃은 rail 아래 인라인 아코디언으로 렌더. |
| 모바일 | 드로어(focus trap, Esc/스크림 탭으로 닫기, 닫힘 시 햄버거로 포커스 복귀). |
| 비활성 | 지원 어댑터가 없는 소분류는 `aria-disabled` + "(준비 중/선택 지역 미지원)" 텍스트(색만으로 구분 금지). |
| 모션 | `prefers-reduced-motion`이면 전환 없음. |

데이터 소스: 기존 `GET /api/categories`(children[].brands) + 선택 지역 반영(4절).

### 1.5 기존 4단계와의 합성(회귀 방지)

기존 단계 매핑:

| 기존 | 신규 위치 | 비고 |
|---|---|---|
| 01 브랜드 | 상단 바 브랜드 멀티셀렉트 + 필터 "제조사" 그룹(동일 상태 `S.selBrands`) | 양쪽에서 변경해도 동기화. |
| 02 제품군(대분류) | 메가메뉴 rail | `S.selMajors` 유지. 다중 대분류 선택은 rail에서 Ctrl/Shift 클릭 또는 "여러 제품군 비교" 토글(기본 단일). |
| 03 소분류 | 플라이아웃 항목(체크박스형 다중 선택 가능) | `S.selSubs` 유지. |
| 04 가격대 | 필터 "가격대" 범위 + 밴드 방식(자동/직접) 컨트롤을 결과 헤더로 이동 | `band_mode`, `thresholds` 필드 그대로. |
| 후보 검색 버튼 | 선택 변경 시 "후보 검색" 버튼 활성(자동 검색 안 함: 사이트 부하 때문) | `POST /api/search`. |
| 후보/수집/비교 스테이지 | 그대로(`#candidates`, `#collect`, `#results`) | 결과 영역 컴포넌트로 재사용. |

핵심 원칙:
- 검색(네트워크)과 필터(클라이언트) 분리: **검색 조건 = region + brand + sub (+ limit)**, **필터 = 이미 받은 후보를 좁히는 조건**. 필터 변경은 재검색을 일으키지 않는다(상세 수집/스펙 필터 제외). 검색 조건을 바꾸면 "후보를 다시 검색하세요" 배너.
- 이행: 기능 플래그 `gauge.ui=v2`(기본 off) -> 스테퍼 DOM(`#setup`)을 유지한 채 v2 셸을 별도 컨테이너에 렌더. Phase A 수용 테스트 통과 후 기본 on, 한 릴리스 뒤 `#setup` 제거.
- 저장: `gauge.sel`에 `regions`, `filters` 키를 추가(없으면 기본값). 구버전 저장값은 그대로 읽힌다.
- 유지되는 제약: 브랜드x대분류당 1모델, 최대 10개 수집, `limit`, `browser_mode`, localStorage 키 `gauge.recent`.
- 보안 규칙 유지: 서버 데이터는 `textContent`로만 삽입(필터 라벨/값 포함), CSP 인라인 스크립트 금지.

---

## 2. 필터 스키마 (`catalog.FILTER_SCHEMA`)

### 2.1 스키마 형식

```python
# catalog.py
FILTER_SCHEMA: dict[str, list[dict]] = {   # key = sub key, or "<major>:*" for shared groups
  "refrigerator:*": [ {...}, ... ],
  "french_door": [ {...} ],
}

# 항목 예
{"key": "capacity_l", "label_ko": "총용량", "label_en": "Total capacity",
 "type": "multi|range|boolean",
 "level": "listing|spec",            # 2.3 참조
 "unit": "L",                        # range 기준 단위(내부 저장 단위, 4.0의 정규화 단위)
 "display_units": ["L", "cu ft"],    # UI에 병기
 "values": [{"key": "500_600", "label_ko": "500~600L", "min": 500, "max": 600}],   # multi/버킷 정의
 "source": ["attrs.capacity_total_l", "name"],     # 후보에서 읽는 위치(우선순위)
 "regions": ["*"],                    # 해당 필터가 의미 있는 지역
 "default_open": True, "more_after": 6}
```

- `type=multi`: 값 집합 중 OR(그룹 내) / 그룹 간 AND. `range`: [min,max] 포함 구간(버킷 칩 또는 슬라이더). `boolean`: "있음만"(체크 시 true 필터), unknown은 제외하지 않는 옵션 제공.
- 필터 값 키는 영어 snake_case, 라벨은 한국어(+영어 병기 가능).
- 공통 그룹(`<major>:*`)과 sub별 그룹을 병합해 `GET /api/filters`가 반환.

### 2.2 공통 그룹 (모든 제품군)

| key | type | level | 값/비고 |
|---|---|---|---|
| `region` | multi | listing | `catalog.REGIONS` (상단 바와 동일 상태, 패널에서는 표시 전용 요약) |
| `brand` | multi | listing | 어댑터 브랜드. count는 후보 수. |
| `price_band` | range | listing | USD 환산값(4.0 환산 규칙) 또는 로컬 통화. 기본: 자동 3분위. |
| `keyword` | text(boolean 취급 아님) | listing | 모델명/이름 부분 일치. 검색 입력창과 동일. |
| `spec_known` | boolean | spec | "스펙 확인된 제품만" (상세 수집 후 의미) |

### 2.3 level 정의

| level | 의미 | 데이터 출처(실제 코드 기준) | 적용 시점 |
|---|---|---|---|
| listing | 리스팅 API 응답/후보 필드만으로 판정 | `Candidate`: brand, name, price, url, category, subcategory. GE는 Searchspring raw item(`r`)을 `parse_search`가 이미 받지만 현재 Candidate로 버림. LG Coveo `raw`(`ec_*`), Samsung `searchResults` 항목도 추가 필드가 있다. 어댑터가 `attrs`로 노출하도록 확장. | 검색 직후 클라이언트/서버 즉시 |
| spec | 상세 페이지(PDP) 스크래핑 필요 | `ProductRecord`: capacity_*_cuft, width_in, energy_kwh_year, energy_star, ice_maker, water_dispenser, wifi_supported, finish_color, `extra_specs` | 상세 수집 후, 또는 상위 N 후보에 한해 "스펙 미리보기" 가벼운 수집 |

원칙: 어댑터마다 listing에서 실제로 얻는 값이 다르므로, 필터마다 `level`은 "최선의 경우"를 선언하고, 후보별로는 `attrs` 존재 여부로 known/unknown을 판정한다. 즉 같은 필터가 GE에서는 listing으로 동작하고 Bosch에서는 unknown으로 남을 수 있다. 어댑터별 실제 제공 필드는 Phase A 첫 작업에서 `ADAPTER_ATTRS_MATRIX`(브랜드 x attr) 표로 확정한다(현재 미조사: 위 3개 어댑터의 raw 필드 이름 외 확인 안 됨, 'unverified').

name 키워드 파생(항상 가능, 신뢰도 중): 이름 문자열에서 정규식으로 도어 타입("French Door", "Side-by-Side", "4-Door"), 용량("28 cu. ft."), 폭("36-inch", "30\""), 스마트("Smart", "Wi-Fi", "Family Hub"), 색상("Stainless") 추출. 파생값은 `attrs_src["capacity_total_cuft"]="name"`로 출처를 기록하고 UI에 "이름에서 추정" 점선 마크.

### 2.4 냉장고 (`refrigerator`)

공통(`refrigerator:*`)

| key | 라벨 | type | level | 값 |
|---|---|---|---|---|
| `door_type` | 도어 타입 | multi | listing(이름/서브키 파생) | `french_door`,`side_by_side`,`top_freezer`,`bottom_freezer`,`four_door`,`built_in_panel`. sub 선택과 중복되므로 대분류 전체 검색 시에만 노출. |
| `capacity_l` | 총용량 | range(버킷) | listing(일부)/spec | `~400L`, `400~500`, `500~600`, `600~700`, `700L~` ; 보조 표시 cu ft(= L / 28.3168). 내부 저장은 cu ft 원본 + L 파생(4.2). |
| `width_class` | 폭(width) 클래스 | multi | listing(이름 "36-inch")/spec(`width_in`) | `w24`(<=25in), `w30`(28~31), `w33`(32~34), `w36`(35~37), `w_other`. 한국/유럽은 mm 입력: 595~600mm(60cm) / 700~720 / 795~800 / 900+ 로 `width_in`으로 환산해 같은 클래스에 매핑. |
| `energy` | 에너지 | multi | spec | `energy_star`(boolean), 연간 kWh 버킷 `~350`,`350~450`,`450~`. 한국은 "에너지소비효율등급 1~5"(`kr_grade`), 유럽은 EU label A~G(`eu_class`): 지역별 별도 필터로 `regions` 제한. 등급 간 환산은 하지 않는다(표시만). |
| `dispenser` | 제빙·정수 | multi | spec(일부 listing 이름 "Ice and Water") | `ice_maker`, `water_dispenser`, `none`. |
| `smart` | 스마트 | multi | listing(이름 "Smart/Wi-Fi/Family Hub")/spec(`wifi_supported`) | `wifi`, `app_control`, `screen`(패밀리허브/스마트스크린). |
| `finish` | 마감색 | multi | listing(이름/색상 코드)/spec(`finish_color`) | `stainless`,`black_stainless`,`white`,`black`,`slate`,`panel_ready`,`other`. |
| `cooling` | 컴프레서/냉각 | multi | spec | `inverter_linear`,`dual_evaporator`,`single_evaporator`,`unknown` (용어는 브랜드별 상이, 정규화 사전 필요). |
| `price` | 가격대 | range | listing | 공통 `price_band`. |

sub별 추가: `built_in` -> `install_type`(`fully_integrated`,`panel_ready`,`column`) spec; `compact` -> `capacity_l` 버킷을 `~100,100~200,200~`로 재정의(`FILTER_SCHEMA["compact"]` 오버라이드).

### 2.5 세탁기/건조기 (`washer`)

| key | 라벨 | type | level | 값 |
|---|---|---|---|---|
| `machine_type` | 유형 | multi | listing(sub 키로 확정) | `top_load`,`front_load`,`dryer`,`laundry_center`. 메뉴 선택과 동일. |
| `capacity_kg` | 용량 | range(버킷) | spec(GE는 `Total Capacity (cubic feet)`), listing(이름) | 세탁기 `~9kg`,`9~15`,`15~21`,`21kg~`; 미국은 cu ft(드럼 용량)이므로 kg 환산 불가: 별도 필드 `drum_cuft` 유지, kg는 한국/유럽에서만 known. UI는 지역 선택에 맞는 단위 버킷만 노출하되 "다른 단위로 표기된 제품" 건수를 unknown 대신 `other_unit`으로 별도 표기. |
| `spin_rpm` | 탈수 속도 | range | spec | `~1000`,`1000~1200`,`1200~1400`,`1400~` rpm |
| `steam` | 스팀 | boolean | spec/listing(이름 "Steam") | |
| `stackable` | 스태커블 | boolean | spec | 스택 가능/워시타워 호환 |
| `dryer_tech` | 건조 방식(건조기) | multi | spec | `heat_pump`,`condenser`,`vented`,`gas`,`electric` (sub=dryer일 때만) |
| `smart` | 스마트 | multi | listing/spec | `wifi`,`app_control` |
| `energy` | 에너지 | multi | spec | `energy_star`, 지역별 등급(KR 1~5, EU A~G) |
| `price` | 가격대 | range | listing | |

### 2.6 조리기기 (`cooking`)

| key | 라벨 | type | level | 값 |
|---|---|---|---|---|
| `cook_type` | 유형 | multi | listing(sub 키) | `microwave`,`sco`,`otr`,`gas_oven`,`electric_oven`,`induction`,`radiant` |
| `fuel` | 열원 | multi | listing(sub/이름 "Gas","Electric","Induction","Dual Fuel")/spec | `gas`,`electric`,`induction`,`dual_fuel` |
| `width_class` | 폭 | multi | listing(이름 "30-inch")/spec | `w24`,`w30`,`w36`,`w48`,`other` (cooktop/range 공통). 유럽 `w60cm`,`w90cm` 환산 매핑. |
| `oven_capacity` | 오븐 용량 | range | spec | (sco는 스피드 오븐 캐비티용 별도 버킷 `~1.0`,`1.0~1.5`,`1.5~2.0`,`2.0~` cu ft; burners/fuel 숨김) cu ft(L 병기) `~4.0`,`4.0~5.0`,`5.0~6.0`,`6.0~`; 전자레인지는 `microwave_capacity`로 별도 버킷 `~1.0`,`1.0~1.5`,`1.5~` cu ft. |
| `burners` | 버너/화구 수 | multi | spec/listing(이름 "5 Burner") | `2`,`3`,`4`,`5`,`6+` |
| `microwave_power` | 마이크로웨이브 출력 | range | spec(sco 전용; 이름 "1000W") | `~900 W`,`900~1000 W`,`1000~1100 W`,`1100 W~` |
| `convection` | 컨벡션/에어프라이 | multi | listing(이름 "Convection","Air Fry")/spec | `convection`,`air_fry`,`none` |
| `self_clean` | 자가세척 | multi | spec | `pyrolytic`,`steam_clean`,`none` |
| `smart` | 스마트 | multi | listing/spec | `wifi`,`app_control` |
| `price` | 가격대 | range | listing | |

전자레인지/OTR 등 sub에서 의미 없는 필터(`burners` 등)는 `FILTER_SCHEMA[sub]`에서 `exclude: ["burners"]`로 숨긴다.

### 2.7 후보 데이터 모델

```python
class Candidate(BaseModel):
    brand: str
    model_number: str
    name: str
    url: str
    price_usd: Optional[float] = None       # 하위 호환 유지(NA 기준)
    category: str = "refrigerator"
    subcategory: Optional[str] = None
    # --- 신규 ---
    region: str = "na"                       # catalog.REGIONS key
    country: str = "us"                      # ISO 3166-1 alpha-2 소문자
    price_local: Optional[float] = None
    currency: str = "USD"                    # ISO 4217
    attrs: dict[str, Any] = {}               # 리스팅 사실. 키는 스키마의 source 키(예: capacity_total_cuft, width_in, fuel)
    attrs_src: dict[str, str] = {}           # 키별 출처: "listing" | "name" | "detail"
```

규칙:
- `attrs` 값은 이미 **표준 단위**(cu ft, in, lb, kWh/yr, 스펙에 따라 L/mm/kg도 병렬 키)로 정규화된 숫자/불리언/문자열. 알 수 없으면 키 자체를 넣지 않는다(None 값 금지: 없음 = unknown).
- `price_usd`는 `region=na`에서만 채움(호환). 그 외 지역은 `price_local`+`currency`, 선택적 `price_usd_approx`는 응답 계층에서만 계산(저장 안 함).
- 캐시 키(`store.get_candidates`)에 region 포함. `Store` 스키마는 JSON 컬럼 추가로 마이그레이션(기존 행 region=na 기본).

### 2.8 필터 적용 함수 계약

```python
# filters.py (신규)
def filter_candidates(cands: list[Candidate], selections: dict[str, Any],
                      *, include_unknown: bool | dict[str, bool] = False,
                      products: dict[str, ProductRecord] | None = None) -> list[Candidate]:
    """
    selections: {"brand": ["Samsung"], "capacity_l": ["500_600","600_700"], "smart": ["wifi"],
                 "price": {"min": 1000, "max": 3000, "currency": "USD"}, "keyword": "bespoke"}
    규칙(순수 함수, 입력 불변):
      1. 그룹 간 AND, 그룹 내 OR(multi) / 구간 포함(range) / 값 true(boolean).
      2. 후보의 값 해석 순서: products[url] (spec) -> cand.attrs (listing) -> 없음(unknown).
      3. 필터가 선택된 그룹에서 unknown인 후보는 기본 제외(include_unknown=False). 그룹별 dict로 "미확인 포함" 허용.
      4. 알 수 없는 필터 키/값은 ValueError (서버는 422로 변환).
      5. 정렬 안정: 입력 순서 유지.
    """

def facet_counts(cands, selections, schema_keys, *, products=None) -> dict[str, dict[str, int]]:
    """각 그룹 g의 count는 'g를 제외한 나머지 선택을 모두 적용한 후보' 중 값이 v인 수. unknown 버킷은 '__unknown__'."""
```

### 2.9 건수(count) 계산과 unknown 처리

- **Disjunctive facet counting**: 그룹 g의 각 값 건수 = 다른 모든 그룹의 현재 선택을 적용한 집합에서 g의 값별 개수. g 자신의 선택은 무시해야 사용자가 다른 값을 추가할 때 증가분을 볼 수 있다.
- range 버킷도 동일하게 버킷별 건수. 한 후보는 한 버킷에만 속한다(경계는 `min <= x < max`).
- **unknown 버킷**: 각 그룹 맨 아래 "미확인 (N)" 행을 항상 노출(N=0이면 숨김). 기본 체크 해제 = 해당 그룹 필터 적용 시 미확인 후보는 결과에서 제외된다. 체크하면 포함. 결과 카드에는 해당 속성 옆에 "?" 배지와 `title="리스팅에 정보 없음. 상세 수집 후 확정"`.
- 같은 그룹에서 값과 unknown을 함께 체크하면 합집합.
- 스펙 필터 + 상세 미수집 후보: unknown. 패널 상단 안내: "스펙 필터는 상세 수집된 제품에만 정확히 적용됩니다 (확인됨 X / 전체 Y)". "상위 N개 미리 확인"(N<=10, 기본 off) 버튼으로 `POST /api/collect`와 같은 어댑터 `scrape`를 쓰되 문서(PDF) 다운로드 생략하는 `preview=true` 모드(Phase B).
- 필터는 결과를 줄여도 서버 `MAX_PER_BRAND=1` 선택 규칙과 독립: 필터에서 제외되어도 이미 선택(체크)한 후보는 유지하되 "필터 밖" 배지.
- 클라이언트 필터링 기본(후보 <= 수백 건): `filters.js`가 같은 로직을 구현하고, 서버는 동일 함수를 `POST /api/search` 응답의 `facets` 계산과 검증에 사용한다. 두 구현의 일치를 계약 테스트(공유 JSON 픽스처)로 고정.

---

## 3. 지역 모델 (Region)

### 3.1 `catalog.REGIONS`

```python
REGIONS: dict[str, dict] = {
  "kr":  {"label_ko": "한국",        "countries": ["kr"],                       "currency": "KRW", "enabled": True,  "default": True},
  "na":  {"label_ko": "북미",        "countries": ["us", "ca"],                 "currency": "USD", "enabled": True},
  "eu":  {"label_ko": "유럽",        "countries": ["de", "fr", "uk", "es", "it"],"currency": "EUR", "enabled": False},
  "sa":  {"label_ko": "남미",        "countries": ["br", "ar", "cl", "co", "mx"],"currency": "BRL", "enabled": False},  # mx는 지리상 북미지만 시장은 중남미로 묶음
  "me":  {"label_ko": "중동",        "countries": ["ae", "sa"],                 "currency": "AED", "enabled": False},
  "as":  {"label_ko": "아시아",      "countries": ["jp", "in", "cn"],           "currency": "JPY", "enabled": False},
  "oc":  {"label_ko": "오세아니아",  "countries": ["au", "nz"],                 "currency": "AUD", "enabled": False},
}
COUNTRIES: dict[str, dict] = {   # country -> {"region", "currency", "lang", "voltage": "110/220", "units": "imperial|metric"}
  "us": {"region": "na", "currency": "USD", "lang": "en", "voltage": "120V/60Hz", "units": "imperial"},
  "kr": {"region": "kr", "currency": "KRW", "lang": "ko", "voltage": "220V/60Hz", "units": "metric"},
  "de": {"region": "eu", "currency": "EUR", "lang": "de", "voltage": "230V/50Hz", "units": "metric"},
  "br": {"region": "sa", "currency": "BRL", "lang": "pt", "voltage": "127V|220V/60Hz", "units": "metric"},
  ...
}
```

- 한국이 기본 선택(사용자 위치). `enabled=False` 지역은 칩에 "준비 중"으로 표시되고 선택 불가. 칩 라벨은 국기 없이 텍스트만.
- 지역 칩 멀티선택: 선택된 지역 x 브랜드 x 소분류 조합이 검색 계획(plan). 기존 `MAX_SEARCH_COMBOS=48`은 (brand x sub x country)로 확장해 적용.
- 시장 비교의 기본 단위는 "지역(region)"; 같은 지역 내 복수 국가(예: 미국+캐나다)는 `country` 필터(고급, 패널 내 접힘).

### 3.2 브랜드별 지역 사이트 (조사 요약)

확인 방법: WebFetch로 samsung.com/sec, lge.co.kr 목록 페이지 구조 확인, WebSearch로 samsung.com 국가별 경로 확인(2026-10). 나머지는 URL 패턴 추정이므로 `unverified`.

| 브랜드 | 한국(kr) | 북미(na) | 유럽(eu) | 남미(sa) |
|---|---|---|---|---|
| Samsung | `https://www.samsung.com/sec/refrigerators/all-refrigerators/` (**verified**: 목록 페이지에 필터 패싯 있음: 도어타입, 용량 구간, 색상, 에너지효율, 가격. 목록에는 가격이 보이지 않음 -> PDP 수집 필요) | `samsung.com/us/` (구현 완료) | `samsung.com/de/`, `/uk/refrigerators/all-refrigerators/` (**uk verified**, de 경로 패턴 unverified), `/fr/` unverified | `samsung.com/br/` (경로 패턴 unverified; 품목 슬러그가 포르투갈어일 수 있음 e.g. geladeiras) |
| LG | `https://www.lge.co.kr/refrigerators` (**verified**: 목록에 정가/할인가/최대혜택가가 보임, PDP `/refrigerators/<model>`, 패싯: 용량, 에너지등급 1~4, 설치형태, 도어 수, 색상, 가격) | `lg.com/us/` (구현 완료) | `lg.com/de/`, `lg.com/uk/`, `lg.com/fr/` unverified | `lg.com/br/` unverified |
| Whirlpool | 없음(한국 시장 철수/미판매: unverified) | `whirlpool.com`(구현), `whirlpool.ca` unverified | `whirlpool.de`, `whirlpool.co.uk`, `whirlpool.fr` unverified | `whirlpool.com.br`/Brastemp/Consul 별도 브랜드: unverified |
| KitchenAid | 없음(unverified) | `kitchenaid.com`(구현), `kitchenaid.ca` unverified | `kitchenaid.de`, `kitchenaid.co.uk`, `kitchenaid.eu` unverified | `kitchenaid.com.br` unverified |
| GE (GE Appliances) | 없음 | `geappliances.com`(구현). 캐나다 `geappliances.ca` unverified | 없음(unverified, Haier 브랜드로 대체) | 없음(unverified) |
| Bosch | `bosch-home.co.kr` unverified | `bosch-home.com/us/en`(구현) | `bosch-home.com/de/de`, `bosch-home.co.uk` unverified (기존 글로벌 도메인 패턴 `bosch-home.com/<cc>/<lang>`와 동일할 가능성) | `bosch-home.com.br` unverified |

`BRAND_DOMAINS`(server.py의 호스트 allowlist)는 지역별 도메인 추가 시 동시에 갱신해야 한다(`host_allowed`, `common.DOWNLOAD_HOST_ALLOW`). 도메인 확장은 지역 활성화 PR에 포함한다.

### 3.3 어댑터 계약 변경

```python
# 각 *_<cc>.py 또는 기존 *_us.py 확장
REGIONS_SUPPORTED: dict[str, list[str]] = {"na": ["us"]}      # region -> countries
SUPPORTED_SUBCATEGORIES: set[str]                              # 기존 유지(지역별 다르면 dict 허용)
def discover(subcategory: str, limit: int = 30, region: str = "us") -> list[Candidate]:  # region = country code
def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]          # URL 호스트로 국가 판정
```

- 파라미터 이름은 사용자 요청대로 `region`이지만 어댑터 단위에서는 **country code**('us','kr','de','br')를 받는 것을 권장한다(같은 region에 국가별 사이트가 따로 존재). 서비스 계층이 region -> countries로 펼쳐 호출한다. 기본값 `'us'`는 기존 호출(`adapter.discover(sub, limit)`) 호환.
- 파일 구성 권장: 국가별 신규 모듈(`samsung_kr.py`, `lg_kr.py` ...)을 만들고 `catalog.ADAPTERS`를 `{(brand, country): module}`로 확장(`ADAPTERS[brand]`가 us 모듈을 계속 가리키는 별칭 유지). 한 모듈이 여러 국가를 처리하면 `REGIONS_SUPPORTED`만 늘린다. 이유: 사이트 구조가 국가마다 달라(예: LG US=Coveo, LG KR=자체 렌더링) 분기 난립 방지.
- `catalog.supported(brand)`는 `supported(brand, country=None)`로 확장. 없는 조합은 skipped 로그("<브랜드> <지역> 미지원").
- `service.search(brands, subs, limit, store, regions=["na"])`: (brand, country, sub) 루프. 캐시 키 `(brand, major, sub, country)`. 로그 행에 country 포함.
- `service._tag`가 `cand.region/country`도 찍는다(어댑터가 안 채워도 호스트 기준 보정).
- `ProductRecord` 추가 필드: `region`, `country`, `currency="USD"`, `price_local: Optional[float]`, `price_usd` 유지(NA는 동일값; 타 지역은 None 또는 approx 아님: 저장하지 않음), `lang`, `source_lang_fields`(번역된 필드 목록). Excel `Products` 시트에 열 추가(Region, Country, Currency, Price_local, Price_usd_approx[고정환율, 표기 "≈"]).
- `store.PARSER_VERSION` 증가(캐시 무효화).

### 3.4 통화와 환율

- 항상 **현지 가격 + 통화 코드**를 원본으로 저장. 비교 화면은 기본으로 "현지 통화" 표시.
- 선택 기능 "USD 환산(근사) 보기": 정적 테이블 `fx.py`:

```python
FX_AS_OF = "2026-01-01"        # 문서화된 기준일. 실제 값은 구현 시 사용자 승인 후 기입
FX_USD_PER_UNIT = {"USD": 1.0, "KRW": 0.00070, "EUR": 1.08, "GBP": 1.27, "BRL": 0.18, "CAD": 0.73}   # 예시값(approximate)
```
  - 표 숫자는 **자리표시용 예시**이며 실제 기준일/값은 구현 시 확정해 `docs`에 기록. UI에는 항상 "≈ 환산(고정환율 {FX_AS_OF}, 참고용)" 배지. 가격대 필터/밴드 계산 시 환산은 옵션(`price_basis=local|usd_approx`)이며 기본 local이고 **지역 간 밴드는 지역별로 따로 3분위**(통화가 다르면 한 풀에서 3분위 금지).
  - 세금 표기: 미국 가격은 세전, 한국/EU는 부가세(VAT) 포함인 경우가 많다. `price_tax_included: Optional[bool]` 필드를 `attrs`에 기록하고(알면) 비교표에 각주. 환산 시 세금 보정은 하지 않는다(왜곡 위험).
  - LG KR처럼 정가/할인가/최대혜택가가 다르게 보이면 **정가(list price)와 일반 판매가만 저장**, 카드/멤버 혜택가는 저장하지 않는다(벤치마크 목적, 비교 가능성).

### 3.5 단위 정규화 레이어 (`units.py`, 신규)

저장 원칙: 원본값+원본 단위를 보존하고, 표준 필드에는 정규화 값을 둔다. UI는 두 단위를 병기한다.

| 양 | 내부 표준 | 변환 | 병기 예 |
|---|---|---|---|
| 용량(냉장/조리) | cu ft | L = cu ft x 28.3168 | `28.0 cu ft (793 L)` |
| 길이 | in | mm = in x 25.4 | `36 in (914 mm)` |
| 무게 | lb | kg = lb x 0.45359237 | `300 lb (136 kg)` |
| 세탁 용량 | kg (한/유럽 표준). 미국은 cu ft 드럼 용량 | cu ft와 kg는 상호 환산하지 않음(정의가 다름). | `5.0 cu ft` / `21 kg` 각각 표기 |
| 에너지 | kWh/yr | 측정 조건 차이(미국 DOE vs EU/한국 KS) 존재 -> 환산 금지, `energy_basis` 필드("DOE","EU 2019/2016","KS C IEC 62552")로 출처 표시 | 비교표에 기준 각주 |
| 전압/주파수 | V, Hz | 문자열 보존 | 120V/60Hz vs 230V/50Hz |

```python
def cuft_to_l(x: float) -> float: ...
def l_to_cuft(x: float) -> float: ...
def in_to_mm(x): ...; def mm_to_in(x): ...
def lb_to_kg(x): ...; def kg_to_lb(x): ...
def parse_quantity(text: str, kind: str, lang: str) -> tuple[float, str] | None:
    """'793 L', '28 cu. ft.', '914mm', '36" W', '793 리터', '1.800 W' -> (표준 단위 값, 원 단위). 소수/천단위 구분자는 lang별 처리(de: '1.234,5', en: '1,234.5')."""
def fmt_dual(value, kind, primary: str) -> str: ...   # 병기 문자열
```

- 필터 버킷(2.4~2.6)은 표준 단위에서 정의하고 병기 라벨을 `label_ko`에 함께 쓴다(`"500~600L (17.7~21.2 cu ft)"`).
- 단위 변환 테스트: 왕복 오차 < 0.1%, 경계 버킷 포함 규칙, 쉼표 소수 파싱.

### 3.6 언어/번역

- 수집은 현지 언어 사이트에서 현지어 그대로(`ko`,`de`,`fr`,`pt`) 가져온다. 사양 값은 숫자+단위가 대부분이라 `units.parse_quantity`와 사전(dictionary)으로 먼저 정규화(예: `Edelstahl`->`stainless`, `Abtauautomatik` 등)한다. 사전에 없는 자유 텍스트(특징, 기능 설명, `pod_features`)에만 기존 계획의 **로컬 LLM 번역 경로**(`llm.py`)를 사용한다.
- 표준 스키마(영문 키)는 불변. `RawSpec.key/value`는 원문을 유지하고, 번역본은 `extra_specs`/`pod_features`에 영어로 저장, `source_lang_fields`에 번역 표시. 번역 결과에는 `translated=True` 메타를 붙여 UI/Excel에서 "기계 번역" 각주.
- 캐시: 번역 결과를 `(hash(원문), lang)` 키로 캐시(`store`)해 재번역을 막는다. LLM 미사용 환경(mock)에서는 번역을 건너뛰고 원문 표시.
- 한국어 UI 라벨은 기존대로 `label_ko`. 한국 사이트(kr)의 값은 번역이 필요 없으므로 영어 표준 키로의 매핑 사전만 쓴다(`ko_spec_dictionary`).

### 3.7 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| Akamai/봇 차단 (samsung.com, lge.co.kr 등) | 목록/PDP 403 | 기존 `browser_mode`(headless/visible) 폴백 재사용, 요청 간격 `POLITE_DELAY_S` 유지, 국가당 동시 1건, 차단 시 지역 "부분 실패" 로그. 사이트 구조 변경 감지용 구조 검사(`parse_*`에서 키 존재 확인) 유지. |
| IP 기반 지역 리다이렉트 (samsung.com 등) | 한국 IP에서 /us 접근 시 /sec 리다이렉트 가능 | 어댑터는 명시적 국가 경로로만 요청, 최종 URL 검증(`_check_final_url` 패턴)에 국가 경로 포함, 리다이렉트로 국가가 바뀌면 오류 처리(조용히 다른 지역 데이터를 섞지 않음). 한국 IP에서 미국 사이트 수집이 막히면 기존 NA 어댑터 영향이므로 Phase A 시작 시 현재 동작 점검. |
| 쿠키 동의 배너 (EU/UK/BR) | 브라우저 모드에서 콘텐츠 가림 | 기존 `ge_us._consent(page, accept=False)` 패턴 확장: 비필수 거부 우선, 필수만 허용. 자동 동의 수락 금지(정책). |
| 가격 표기 차이(세금/프로모션) | 오해 소지 | 3.4 정책: 정가 저장, 세금 포함 여부 각주, 환산은 근사 표기. |
| 모델 번호 체계 지역별 상이(Samsung RF28... vs RF70...) | 동일 제품 매칭 불가 | 지역 간 "동일 모델 매칭"은 범위 밖. 비교는 사양 클래스(용량/폭 밴드)로. |
| 법적/약관 | 해외 사이트 robots/ToS | 공개 페이지만, 간격 유지, 지역 활성화 전 robots.txt 확인을 acceptance 항목에 포함. |
| 전압 차이로 사양 비교 왜곡 | 전기 소비/용량 | 3.5 병기 + 기준 각주. |

---

## 4. API 변경 (하위 호환)

기존 요청은 그대로 동작: `regions` 미지정 시 `["na"]`(기존 데이터 동일), `filters` 미지정 시 필터 없음.

### 4.1 신규/변경 엔드포인트

`GET /api/regions`
```json
[{"key":"kr","label_ko":"한국","enabled":true,"default":true,"countries":["kr"],"currency":"KRW",
  "brands":["Samsung","LG"]},
 {"key":"na","label_ko":"북미","enabled":true,"countries":["us","ca"],"currency":"USD","brands":["Samsung","LG","GE","Whirlpool","KitchenAid","Bosch"]},
 {"key":"eu","label_ko":"유럽","enabled":false,"note":"준비 중","countries":["de","uk"],"currency":"EUR","brands":[]}]
```
`enabled` = 해당 지역을 지원하는 어댑터가 하나라도 import 가능(기존 `_brand_support` 방식).

`GET /api/filters?subcategory=french_door&region=na`
```json
{"subcategory":"french_door","region":"na","groups":[
  {"key":"width_class","label_ko":"폭","type":"multi","level":"listing","unit":"in","display_units":["in","mm"],
   "values":[{"key":"w36","label_ko":"36in (914mm)"}],"more_after":6,"default_open":true}]}
```
- 여러 지역: `region=na,kr` (쉼표). `subcategory`는 sub 키 또는 major 키(공통 그룹만). 응답은 `FILTER_SCHEMA` 병합 결과 + 지역별 `regions` 제한 필터 제외. count는 포함하지 않음(검색 결과가 있어야 함).

`GET /api/categories`: `?region=kr` 선택 인자 추가. 각 child의 `enabled/brands`를 해당 지역 기준으로 계산(미지정 시 기존과 동일 = 전 지역 합집합이 아니라 `na`).

`GET /api/brands`: `?region=` 선택 인자, 항목에 `regions: ["na","kr"]` 추가(기존 필드 유지).

`POST /api/search`
```json
{"brands":["Samsung","LG"],"subcategories":["french_door"],"regions":["na","kr"],"limit":30,
 "band_mode":"auto","thresholds":null,"filters":{"capacity_l":["600_700"],"smart":["wifi"]},"browser_mode":"auto"}
```
- 필터는 **검색 결과(후보)에 적용**되고 어댑터 호출 범위를 줄이지 않는다(캐시 재사용을 위해). 단 `keyword`는 어댑터 목록 API에 질의 가능하면 전달(선택, Phase C 이후).
- 결과 JSON 추가: `facets` (각 필터 그룹의 값별 count + `__unknown__`), `filtered_total`, `unfiltered_total`, `region_totals`. Candidate에 `region,country,price_local,currency,attrs,attrs_src` 추가. 가격 밴드는 (major x region)별로 계산하며 `groups[]`에 `region` 추가(지역 1개면 기존 형태와 동일).
```json
"facets":{"brand":{"Samsung":12,"LG":9,"__unknown__":0},
          "capacity_l":{"500_600":8,"600_700":5,"__unknown__":7}}
```
- `POST /api/collect`: CollectItem에 `region`/`country`(선택, 기본 `us`) 추가. 규칙 "브랜드당 1모델(대분류별)"은 (brand, major, region)당 1개로 완화 여부를 **사용자 결정 필요**(7절 Q3). 기본은 현행 유지(지역 무관 1개)가 아니라 (brand, major, region)당 1개 제안.

### 4.2 서버 검증 규칙

| 항목 | 규칙 | 오류 |
|---|---|---|
| `regions` | 목록 크기 1~4, 중복 제거, `catalog.REGIONS` 키이며 `enabled`, 아니면 422 `지원하지 않는 지역입니다.` | 422 |
| 조합 | (brand, region, sub)가 어댑터 지원이 아니면 건너뜀 + log 행 `skipped`. 지원 조합이 0개면 422. | 422 |
| 조합 수 | `len(plan) <= MAX_SEARCH_COMBOS`(brand x country x sub). | 422 |
| `filters` | 키는 `FILTER_SCHEMA`(해당 sub/region)에 존재, 값은 스키마 `values` 키 또는 range `{min,max}` 숫자(0 <= min <= max <= 1e9), 문자열 길이 <= 64, 그룹당 값 <= 50, 총 그룹 <= 20. 위반 422. | 422 |
| `keyword` | 길이 <= 64, 제어문자 제거. 어댑터 질의로 쓰면 URL 인코딩, 임의 URL 불허. | 422 |
| URL/호스트 | `host_allowed(brand,url)`에 지역 도메인 allowlist(brand -> set of domains) 적용. 지역 사이트 추가 시 allowlist와 같이 갱신. | 422 |
| 통화/환산 | 요청이 임의 환율을 전달 불가(서버 정적 테이블만). `price_basis` in {local, usd_approx}. | 422 |
| 하위 호환 | `regions`/`filters` 없음 = 현행 동작. `category` 레거시 필드 유지. | |
| 보안 | 기존 Origin/Host 가드, 동시 1작업(409), CSP 변경 없음. 필터/지역 라벨은 UI에서 `textContent`. | |

---

## 5. 단계별 구현 계획

파일 소유권(제안: 동시 편집 충돌 방지)

| 영역 | 파일 | 소유 |
|---|---|---|
| 카탈로그/스키마 | `catalog.py`, `filters.py`(신규), `units.py`(신규), `fx.py`(신규) | Backend-A |
| 서비스/서버 | `service.py`, `server.py`, `store.py`, `schema.py`, `excel_writer.py` | Backend-B |
| 어댑터 | `*_us.py`(attrs 노출), `samsung_kr.py`, `lg_kr.py`, `*_de.py`, `*_br.py` | Adapter-owner (브랜드별 분담 가능) |
| 프론트 | `web/index.html`, `web/css/app.css`, `web/js/app.js`(분리: `menu.js`, `filters.js`, `region.js`) | Frontend |
| 문서/테스트 | `README_API.md`, `README_WEB.md`, `tests/` | 각 변경 담당자 |

### Phase A: UI 셸 + NA 데이터 + 리스팅 필터 (권장 1순위 착수)

범위: 메가메뉴, 우측 필터 패널, 지역 칩(na만 활성, 나머지 "준비 중"), `Candidate.attrs/region/country`, `GET /api/regions`, `GET /api/filters`, `/api/search`의 `regions`/`filters`/`facets`, 클라이언트 필터링, `gauge.ui=v2` 플래그. 어댑터에서 `attrs` 최소 노출: GE(Searchspring raw 필드), LG(Coveo raw), Samsung(searchResults) + 이름 파생 규칙.
작업량: 백엔드 3~4일, 프론트 5~7일, 테스트 2일. 위험: 중(UI 재구성량, 접근성).
수용 테스트:
1. `FRIDGE_MOCK=1`에서 v2 UI로 brand+sub 선택 -> 검색 -> 후보 -> 수집 -> 결과가 기존 e2e(`e2e_run.py`/`tests/test_server.py`)와 동일 결과(회귀 0).
2. 구 `POST /api/search`(regions/filters 없음) 응답 구조 불변(스냅샷 테스트).
3. 필터 선택별 건수가 `facet_counts`와 일치(디스조인티브 규칙, unknown 포함) - 공유 픽스처 계약 테스트.
4. 메가메뉴: 키보드(ArrowDown/Right/Left/Esc/Home/End), 터치(hover:none 에뮬레이션: 첫 탭 펼침), 모바일 드로어(focus trap) 수동+자동(Playwright) 통과. 대비/포커스 링 WCAG AA.
5. 필터 변경은 네트워크 요청을 발생시키지 않음(DevTools 네트워크 0건).
6. 검증 422: 알 수 없는 region/filter 키/값.

### Phase B: 스펙 레벨 필터 + 건수

범위: 상세 수집 후 `ProductRecord`를 필터 값으로 매핑(`products` 인자), "스펙 확인 X/전체 Y" 표시, 상위 N `preview` 수집(문서 다운로드 생략), `units.py` 병기, 에너지/폭/용량 버킷 정규화, Excel 열 추가.
작업량: 5~7일. 위험: 중(어댑터마다 스펙 키 이름 상이 -> 매핑 사전 유지보수).
수용 테스트: 단위 변환 왕복 테스트, 경계 버킷, 스펙 필터 적용 전/후 count 변화, preview 수집이 PDF를 저장하지 않음, unknown 포함 토글.

### Phase C: 한국 어댑터 (samsung.com/sec, lge.co.kr)

사전 확인(2026-10, WebFetch): samsung.com/sec 냉장고 목록은 도어 타입/용량/색상/에너지효율/가격 패싯이 있으나 목록에 가격이 나오지 않아 PDP 또는 내부 검색 API 확인 필요. lge.co.kr 냉장고 목록은 정가/할인가/혜택가 노출, PDP `/refrigerators/<model>`, 패싯: 용량, 에너지등급, 설치형태, 도어 수, 색상. 두 사이트 모두 실제 내부 API/렌더링 방식(JSON vs SSR)은 **미확인**: Phase C 첫날 `browser_mode`로 네트워크 호출을 관찰해 정한다.
범위: `samsung_kr.py`, `lg_kr.py` (냉장고/세탁/조리 서브셋부터), `ko_spec_dictionary`, KRW 처리, 에너지소비효율등급(KR grade) 필터, mm/L/kg 표준 병기, `BRAND_DOMAINS` 한국 도메인 추가, 번역 불필요.
작업량: 어댑터당 4~6일 + 사전/테스트 3일 = 약 2~3주. 위험: 중상(Akamai, 자체 렌더링, 가격 다변화).
수용 테스트: 각 어댑터 `discover` 3개 sub에서 >= 10 후보(가격 KRW), URL 호스트 allowlist 통과, 리다이렉트로 국가 바뀌면 에러, PDP scrape가 `capacity/width/energy_grade` 최소 3필드 채움, 정가만 저장(혜택가 미저장).

### Phase D: 유럽 (DE/UK) + 남미 (BR), 번역

범위: `samsung_de/uk`, `lg_de/uk`, `samsung_br`, `lg_br` 등 확인된 사이트부터, 쿠키 동의 처리, EU 에너지 라벨 `eu_class`, 로컬 LLM 번역 연계(`pod_features`, 자유 텍스트), `fx.py` 정적 환율과 USD 근사 보기, 지역별 밴드.
작업량: 국가당 3~5일 + 번역/사전 5일 = 약 3~4주. 위험: 높음(미검증 사이트 다수, 쿠키/봇 차단, 언어).
수용 테스트: 현지어 사양에서 용량/폭/에너지 숫자 정규화 정확도(샘플 20건 수동 대조 >= 90%), 번역 캐시 적중, 환산 배지 노출, 지역별 밴드 분리 확인.

### 권장 순서와 근거

| 순서 | Phase | 이유 |
|---|---|---|
| 1 | A | 새 IA/스키마/API 골격을 NA 데이터로 검증. 리스크 낮고 이후 모든 지역의 전제. |
| 2 | C (병행 가능: B 일부) | 사용자가 한국에 있고 원래 계획이 한국어 사이트 중심. 번역 불필요 -> 가치 대비 위험 낮음. |
| 3 | B | 스펙 필터는 C의 한국 데이터로 한국 에너지등급 등 실제 가치 검증 가능. A 직후 B를 먼저 해도 무방(C와 독립). |
| 4 | D | 사이트 미검증, 번역/쿠키/환율 도입. 마지막. |

권장 최종 순서: **A -> C -> B -> D** (B는 A 직후 필요 시 C와 병행).

총 예상: A 약 2주, C 약 2~3주, B 약 1~1.5주, D 약 3~4주 (1인 기준, 사이트 변경에 따른 재작업 별도).

---

## 6. 열린 위험 / 가정

- 가정: `attrs`로 노출 가능한 리스팅 필드는 3개 어댑터 코드에서 확인한 범위(이름, 가격, 일부 카테고리 코드)만 확정이며 용량/폭이 리스팅 API에 있는지는 **미확인**. 없으면 이름 파생 + spec 지연 적용이 기본 경로.
- `mx`(멕시코)를 남미(sa)에 둘지 북미에 둘지는 사용자 결정.
- 환율 값과 기준일은 구현 시 확정(본 문서의 수치는 예시).
- 지역 사이트 URL 중 `unverified` 표시는 구현 전 개별 확인 필수(robots.txt 포함).

## 7. 사용자 결정 필요 사항

1. 지역 기본값: 한국을 기본 선택으로 할지(제안) 북미를 유지할지.
2. 멕시코 분류(남미 vs 북미)와 1차 활성 국가 목록(제안: kr, us, ca 보류, de, uk, br).
3. 수집 제한 "브랜드당 대분류 1모델"을 (brand, major, region)당 1개로 완화할지(제안) vs 현행 유지.
4. 정적 환율 기준일/값 승인(근사 표기 전제), 가격 밴드는 지역별 분리(제안).
5. 다중 대분류 동시 선택을 rail에서 허용할지(제안: 기본 단일 + 토글).
6. 한국 사이트에서 LG 혜택가 미저장(정가/판매가만) 정책 확인.
