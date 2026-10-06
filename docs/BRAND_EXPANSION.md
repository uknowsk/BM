# 브랜드 확장 계획 (6개 -> 30개) 와 가스 쿡탑

작성: 2026-10-06. 새 대화는 `docs/HANDOFF.md`, `docs/TODO.md`와 이 문서를 먼저 읽고 시작한다.

## 사용자 결정 (확정)
- 시작: 지금 바로. 서버(8765)는 사용자가 테스트 중이므로 **재시작/기존 파일 수정은 최소화**하고, 신규 어댑터는 새 파일로 추가한다. 기존 화면·필터는 브랜드 확장에 필요한 만큼만 고친다.
- 유럽 전용 브랜드(De Dietrich, AEG, Siemens 등)도 지금 함께: 독일(de)·프랑스(fr)·영국(uk) 어댑터와 독일어·프랑스어 번역 계층까지 포함.
- 신규 24개 브랜드 목록은 제안 그대로 (아래).
- 가스 쿡탑 = 오븐 없는 가스 쿡탑(한국의 가스레인지 개념). 가스 레인지(오븐 포함)와 가스 월오븐은 기존 `gas_oven`에 둔다.
- 모델 수 제한은 없음(한 번에 최대 `MAX_COLLECT`=12). 기본 시장은 북미.

## 브랜드 목록
기존 6: Samsung, LG, KitchenAid, GE, Whirlpool, Bosch.
신규 24 (재사용 가능한 플랫폼 단위로 묶음):

| 묶음 | 브랜드 | 시장 | 재사용/비고 |
|---|---|---|---|
| Whirlpool Corp. | Maytag, JennAir, Amana | us | `whirlpool_us`/`kitchenaid_us`와 같은 플랫폼(OCC `/ws/v2/<site>/products/search`, AEM 사양 JSON). 도메인/사이트 코드만 다름. Akamai: 새 headless(`channel="chromium"`) 사용 |
| BSH | Thermador, Gaggenau, Siemens | us(Thermador, Gaggenau), de(Siemens, Gaggenau) | `bosch_us`와 같은 Next.js 페이로드 구조 가능성. Siemens는 미국에서 가전 판매 없음 -> de |
| Electrolux | Frigidaire, Electrolux, AEG | us(Frigidaire, Electrolux), de/uk(AEG, Electrolux) | 공통 구조 조사 후 어댑터 작성 |
| Haier/GE | Café, Monogram, Haier, Fisher & Paykel | us (+uk/nz) | Café/Monogram은 GE와 별도 사이트(cafeappliances.com, monogram.com). `ge_us` 일부 재사용 |
| 프리미엄 | Viking, Sub-Zero, Wolf, Miele, Smeg, Liebherr, Bertazzoni, De Dietrich | us, de, fr, uk | De Dietrich는 fr 전용. Miele는 가격 비공개(가격 미확인 밴드) |
| 글로벌 | Beko, Hisense, Panasonic | us/uk/de | 사이트별 확인 |

시장 매트릭스(브랜드 x 국가)는 어댑터 모듈 이름 규칙 `<brand>_<country>.py`로 자동 발견된다(`catalog.module_name`). 국가 코드: us, kr, de, uk, fr (es/it은 후순위).

## 가스 쿡탑 (먼저 처리)
- `catalog.CATEGORY_TREE["cooking"]["children"]`에 `gas_cooktop`("가스 쿡탑") 추가, 위치는 `gas_oven` 다음. `electric_oven`/`induction`/`radiant` 정의는 유지 (radiant = 전기 쿡탑/레인지, induction = 인덕션 쿡탑/레인지).
- 모든 어댑터(8개 + 신규)의 `classify()`에서 현재 "어느 소분류에도 안 넣는" 가스 쿡탑/rangetop을 `gas_cooktop`으로 분류하고 `SUPPORTED_SUBCATEGORIES`와 `discover` 질의를 확장한다. 가스 레인지(오븐 포함)는 계속 `gas_oven`.
- 한국 어댑터: 가스레인지(가스쿡탑) 카테고리가 있으면 `gas_cooktop`으로 (LG/삼성 한국 가스레인지 카테고리는 이전 조사에서 404/없음이었으므로 재확인).
- 필터: `열원` 그룹은 혼합 열원 소분류에서만 보이게 하고, `전기오븐`/`전자레인지`/`OTR`/`SCO`에서는 `열원`과 `버너/화구 수`를 숨긴다 (`filters.FILTER_EXCLUDE`). `gas_cooktop`은 열원 고정, 버너 수/폭/BTU 필터 표시.
- POD 조리 분류표에 가스 항목(BTU, 센터 버너, 오토 재점화, 그리들 등) 확인/보강. 테스트(`tests/test_*_us.py`, `test_catalog.py`, `test_filters.py`) 갱신.

## 작업 단계 (병렬, 파일 소유권 분리)
단계 0 — 공통 기반 (한 에이전트, 먼저):
- `catalog.py`: 브랜드 레지스트리 확장(`ADAPTERS`는 us만 담고 다른 국가는 자동 발견), 브랜드 메타(표시명, 계열 묶음, 지원 국가). `server.py`: `/api/brands`에 `countries`, `group` 추가, 검색 상한(현재 brand x sub 48조합)을 브랜드 수에 맞게 조정(예: 120, 순차 실행 + 진행률). `brands.yaml` 갱신.
- 화면(web/): 브랜드 선택창을 계열별 그룹 + 검색창 + 시장별 지원 브랜드만 활성으로 개편, 선택 수 요약. 가스 쿡탑 칩은 API가 내려주는 트리를 그대로 쓰므로 변경 최소.
- 번역 계층 일반화: `ko_en.py`와 같은 구조로 `de_en`/`fr_en` (용어 사전 + 로컬 LLM + 캐시)를 `i18n.py`(언어 코드별 사전 `data/glossary_<lang>.json`)로 일반화하고 `ko_en`은 어댑터 호환 래퍼로 유지. 단위(mm, kg, L, kWh/년, EU 에너지 등급 A~G)는 `units.py`에 확장 (EU 에너지 라벨은 ENERGY STAR와 별개로 `EU class X`로 저장).
단계 1 — 신규 어댑터 (브랜드/묶음별 에이전트 병렬, 각자 새 파일 `<brand>_<country>.py`와 `tests/test_<brand>_<country>.py`, `tests/fixtures/<brand>_<country>/`만 소유):
- 1-A Whirlpool 계열 (maytag_us, jennair_us, amana_us), 1-B BSH 계열 (thermador_us, gaggenau_us, siemens_de, gaggenau_de), 1-C Electrolux 계열 (frigidaire_us, electrolux_us, electrolux_de, aeg_de, aeg_uk), 1-D Haier/GE 계열 (cafe_us, monogram_us, haier_us, fisherpaykel_us), 1-E 프리미엄 (viking_us, subzero_us, wolf_us, miele_de/miele_us, smeg_us/smeg_de, liebherr_de/us, bertazzoni_us, dedietrich_fr), 1-F 글로벌 (beko_uk/us, hisense_us, panasonic_us).
- 각 어댑터 계약은 기존과 동일: `COUNTRY`, `REGION`, `CURRENCY`, `SUPPORTED_SUBCATEGORIES`, `discover(subcategory, limit)`, `scrape(url)`; 후보는 `price_local`/`currency`, 속성 `attrs`, `image_url`, 전체 스펙 표 `extra_specs`('Section > Label'), 원문 `RawSpec`, 제품 분류는 `classify()` 하나를 discover/scrape가 공유, 보안(https, 호스트 허용 목록 `common.DOWNLOAD_HOST_ALLOW`/`IMAGE_HOST_ALLOW`에 필요한 호스트는 단계 0 에이전트가 모아서 처리하거나 각 에이전트가 보고), 파일명 정리, 요청 간 1초 이상, `FRIDGE_BROWSER_MODE`, 쿠키 거절 먼저(막으면 허용), 브라우저 close.
- 브랜드 자체 사이트에 제품이 없거나 로그인/차단이 필요하면 억지로 만들지 말고 해당 브랜드/소분류를 미지원으로 보고 (`SUPPORTED_SUBCATEGORIES`에서 제외).
단계 2 — 통합: 전체 테스트(`for t in tests/test_*.py; python $t`), 서버 재시작, 브랜드별 1개 모델 실제 수집(end-to-end), 비교 시트/양식 흐름 확인, 정리 후 커밋·푸시(`https://github.com/uknowsk/BM.git`, 비밀 정보 검사 먼저).

## 주의
- 각 사이트 이용약관/robots.txt 확인, 요청 간격 준수. 접근 차단(Akamai, Cloudflare)이면 새 headless -> 창 표시 폴백, 그래도 안 되면 미지원으로 보고하고 우회하지 않는다.
- 유럽 사이트는 지오 리디렉션/쿠키 동의가 있을 수 있음. 국가 URL을 명시하고 최종 URL 호스트를 검증.
- 가격은 정가/판매가만 저장 (회원·카드 혜택가 제외). 환율표는 아직 없으므로 `price_local`+`currency`만 저장, 환산 표시는 후속.
- 비용: 어댑터 하나당 사이트 조사와 검증이 20~30분. 병렬 에이전트 6개씩 돌리고 보고는 짧게.
- 새 파일/편집/서버 종료 시 GateGuard 훅이 사실 4가지(호출 위치, 중복 여부, 데이터 구조, 사용자 지시 원문)를 먼저 요구한다. 셸 heredoc으로 파이썬 파일을 만들지 말 것(백슬래시가 깨짐).
