# 해야 할 일 (사용자 테스트 후 반영)

사용자가 직접 테스트하는 동안 프로그램 수정은 보류. 아래 항목은 테스트 피드백과 함께 한 번에 처리한다.
우선순위: 상 / 중 / 하

## A. 사용자 요청
- [ ] 상 | 조리기기 소분류에 **가스 쿡탑**(가스 레인지탑/rangetop 포함) 추가. 지금은 `gas_oven`(가스오븐)만 있고, 가스 쿡탑은 어느 소분류에도 들어가지 않는데 오른쪽 필터 "열원"에는 `가스`가 있어 서로 맞지 않는다. 해외에서는 가스오븐·가스 쿡탑이 모두 판매된다.
  - 필요 작업: `catalog.CATEGORY_TREE`에 새 소분류(예: `gas_cooktop`) 추가, 전체 어댑터의 분류 규칙(현재는 가스 쿡탑을 의도적으로 제외)과 `discover` 카테고리 질의 확장 (GE, Whirlpool, KitchenAid, Bosch, Samsung US, LG US, 한국은 LG/삼성 가스레인지·가스쿡탑 판매 여부 확인), 필터 스키마(열원 그룹을 소분류별로 고정/숨김), POD 조리 분류표, 화면 메뉴·지원 매트릭스, 테스트.
  - **정의 확정(사용자 확인)**: `가스 쿡탑`은 오븐이 없는 쿡탑이며 한국의 "가스레인지"에 해당. `가스오븐`은 가스 레인지(오븐 포함)와 가스 월오븐. 상세 계획과 단계는 `docs/BRAND_EXPANSION.md`의 "가스 쿡탑" 절 참고.
- [ ] 상 | **브랜드 6개 -> 30개 확장** (신규 24개, 유럽 사이트·번역 포함, 지금 시작으로 결정). 목록·묶음·단계·파일 소유권은 `docs/BRAND_EXPANSION.md`.

## B. 화면 점검에서 발견한 결함 (2026-10-06, 실제 서버)
- [ ] 상 | **전기오븐·전자레인지·OTR·SCO에서 "버너/화구 수"와 "열원" 필터가 그대로 보임.** 오븐에는 화구가 없고 열원도 이미 정해져 있다. `filters.FILTER_EXCLUDE`를 소분류별로 확장 (지금은 SCO만 적용). 가스 쿡탑 추가 시 함께 정리.
- [ ] 중 | 조리기기 소분류 칩의 **"SCO (스피드쿡 오븐)" 라벨이 3줄로 줄바꿈**되어 칸 높이가 들쭉날쭉함. 라벨 길이/칸 너비 조정.
- [ ] 중 | **한국 시장을 선택하면 지원 브랜드 안내가 없음.** 한국 어댑터는 Samsung·LG뿐인데 상단은 "브랜드 6/6"로 보인다. 시장별 지원 브랜드만 선택 가능하게 하고 나머지는 비활성/안내.
- [ ] 하 | "후보 검색" 버튼이 검색 중에 같은 자리에서 "검색 취소"로 바뀌어, 두 번 연속 누르면 곧바로 취소됨(점검 중에 실제로 발생). 확인 단계나 위치 분리 검토.
- [ ] 하 | 소분류 펼침 메뉴가 호버 중에 가운데 설정 영역을 덮는다 (마우스를 치우면 닫힘). 의도된 동작이면 그대로 두고, 아니면 위치 조정.

- [ ] 상 | **"30인치 미국 전기오븐" 검색 결과 0건 (사용자 보고, 2026-10-06).** 캐시 기준 원인: 폭(width_in)을 상품명에서만 추정하는데 Samsung/LG 월오븐 이름에는 폭이 없고(0/10), Whirlpool은 3/10, Bosch는 `30''` 표기를 못 읽었음(정규식에 `''`, `″` 추가함: `filters._WIDTH_RE`). 폭 필터를 고르면 "미확인" 후보가 기본 제외되어 다른 필터(버너 수 등)와 겹치면 0건이 된다. 후속: (1) 월오븐은 어댑터가 목록 단계에서 폭을 채우게(모델번호/카테고리/스펙 페이지), (2) 필터 결과가 0건이거나 많이 줄면 "미확인 N개 제외됨 - 포함해서 보기" 안내와 현재 적용된 검색 기준 요약을 화면에 표시, (3) 어떤 필터 조합이었는지 사용자에게 확인.

## C. 데이터·품질
- [ ] 중 | 일부 항목이 엉뚱한 섹션에 배치됨 (예: "End-of-cycle signal"이 세탁·건조). 표준 항목 시드의 섹션 지정 보강.
- [ ] 중 | `data/canon_registry.json`에 예전의 잘못된 병합이 남아 있다가 다음 실행에서 정리됨. 정리 확인.
- [ ] 중 | 필터 대부분이 "미확인"으로 나옴. 어댑터가 목록 단계에서 용량·폭 같은 속성(`attrs`)을 채우도록 개선 (Phase B: 스펙 수준 필터).
- [ ] 중 | 한국어 번역이 느림 (첫 수집 50~220초, 캐시 후 빠름). 용어 사전 확장과 배치 호출 최적화.
- [ ] 하 | 단일 오븐과 이중 오븐의 상·하단 값이 한 행에 합쳐 표시되는 경우가 있음 (`2200 W (하단) / 1700 W (상단)`, 검토 표시됨).
- [ ] 하 | 임베딩·LLM이 꺼져 있을 때 결정적 폴백의 재현율이 낮음(0.982). 사용자에게 상태가 보이는지 확인.

## D. 분류 가정 확인 (틀리면 알려 주세요)
- [ ] 복합 연료 레인지(가스 쿡탑 + 전기 오븐)는 `가스오븐`으로 분류했다.
- [ ] `라디언트`는 인덕션·가스를 제외한 모든 전기 레인지/쿡탑(코일 포함)으로 정의했다.
- [ ] GE 오버더레인지 Advantium은 OTR이 아니라 `SCO`(속조리 오븐)로 분류했다.
- [ ] Samsung 한국 콤팩트 오븐(NQ50)은 오븐+마이크로웨이브 복합으로 보고 `SCO`로 분류했다 (스펙 페이지로 확인하지 않음).
- [ ] LG 한국 하이브리드 레인지는 `라디언트`, 광파오븐은 `SCO`로 분류했다.

## E. 다음 기능
- [ ] 상 | **사용자 분류 양식(엑셀) 실제 샘플 반영**: 샘플을 받으면 열 구성에 맞춰 파서를 보강하고 실제 모델로 매칭 검증. 양식의 동의어 칸이 로컬 LLM의 한계를 보완하는 가장 확실한 수단.
- [ ] 중 | 유럽(독일·영국), 남미(브라질) 어댑터 + 환율/현지 가격 표시 + 다국어 번역 (설계는 `docs/DESIGN_FILTERS.md`).
- [ ] 중 | 새 비교 화면(핵심/전체 토글, 원문 툴팁, 내 양식 기준 전환)을 실제 검색→수집→비교 흐름에서 눈으로 확인 (이번 점검은 검색 단계까지만 확인).
- [ ] 하 | 서버가 종료 코드 4로 끝나는 경우가 있었음(원인 미확인, 로그 없음). 재현되면 원인 조사.
- [ ] 하 | `bind()`의 `pod_items` 보정은 적용됨. 다른 호출부에도 같은 방어가 필요한지 점검.

## F. 브랜드 확장 후속 (2026-10-07, 단계 1 에이전트 보고)
범위 결정: 이번에는 **조리기기만** 구현. 냉장고·세탁기 어댑터는 후순위.
- [ ] 상 | **이용약관 미확인 브랜드**: Maytag/JennAir/Amana(약관 미열람), Café/Haier/Monogram/Fisher & Paykel, Thermador/Gaggenau/Siemens, Frigidaire/Electrolux/AEG(키워드 검색만, 금지 문구 못 찾음), Viking(AI 학습 봇 차단 표기 있음: 사용자 요청 기반 조회라 해당 안 한다고 판단, 확인 필요), Bertazzoni/Wolf(약관 페이지 못 찾음), Hisense/Beko UK/Panasonic(약관 열람 불가·미확인), Miele/Liebherr. robots.txt 는 모두 확인·준수.
- [ ] 중 | **냉장고·세탁기 확장**: 각 에이전트가 판매 소분류를 확인해 둠(Maytag/JennAir/Amana 냉장·세탁은 `Site.codes`에 추가, Sub-Zero는 `_subzerowolf_common.py`로 가능, Liebherr는 `liebherr_de.py`가 냉장고용 코드를 갖고 있고 `SUPPORTED_SUBCATEGORIES=set()`로 비활성).
- [ ] 중 | De Dietrich(프랑스): dedietrich-electromenager.fr·de-dietrich.com 둘 다 Cloudflare 522, 모회사 Brandt 청산 보도. 미지원. 나중에 사이트가 살아나면 재확인.
- [ ] 중 | Miele DE: 레인지(Herd)는 열원 구분이 필요해 미분류, 스팀 전용기는 제외. 번역은 로컬 LLM이 꺼져 있으면 일부 독일어 원문이 남음. Siemens/Gaggenau DE도 같음(스크랩 1건 약 275초, LLM 사용 시).
- [ ] 중 | Hisense US: 상품 페이지에 스펙표가 없고 가격이 플레이스홀더라 스펙 1항목·가격 None. Panasonic US: 스펙표 클라이언트 렌더라 9항목. 필요하면 헤드리스 렌더링으로 보강.
- [ ] 중 | Smeg/Bertazzoni/Wolf/Gaggenau는 가격 비공개(None) → 가격 밴드 "미확인". Viking은 discover 가격 없음(scrape에서만). Viking은 robots Crawl-delay 10초를 지켜 느림(실측: 소분류 8개 목록 전부 약 150초, 제품 1개 수집 약 14초). 어댑터가 `REQUEST_DELAY_S`를 선언하면 `/api/brands`의 `delay_s`/`note`와 화면(브랜드 행 "요청 간격 10초 · 느림", 검색 요약 경고)에 사전 안내가 뜬다(5초 이상, `catalog.SLOW_DELAY_S`). 다른 어댑터가 느려지면 같은 상수만 선언하면 됨. User-Agent는 아직 일반 브라우저 값(`common.UA`): 앱을 밝히는 UA로 바꿀지는 사용자 결정 대기.
- [ ] 하 | 문서 폴더명: `common.download_pdf`가 브랜드명을 정규화해서 Café는 `downloads/caf_/`, Fisher & Paykel은 `fisher___paykel/`. Gaggenau US/DE가 같은 모델이면 파일명이 겹쳐 덮어쓸 수 있음(현재는 모델이 달라 미발생).
- [ ] 하 | Beko UK 설명서 PDF는 `bekoplc.blob.core.windows.net`이 허용 목록에 없어 제외(Azure 호스트라 열지 않기로 함). 필요하면 이 호스트 하나만 추가.
- [ ] 하 | 가스 쿡탑: BTU 필터는 미추가, `convection` 필터는 쿡탑에서도 보임(인덕션·라디언트와 동일).
- [ ] 하 | Café/Haier는 평문 requests에 Cloudflare 챌린지(403): headless 브라우저로만 접근(챌린지 우회 아님, 통과 못 하면 실패 처리). Frigidaire/Electrolux US는 Akamai로 headless 실패 → 창 표시(visible) 폴백.

## G. 경쟁 모델 선별 후속 (2026-10-07, 설계 `docs/MATCHING_DESIGN.md`, 화면 `/match`)
- [ ] 상 | **신제품 판단은 시간이 지나야 쌓인다.** 출시일을 공개하는 사이트가 거의 없어(LG KR `modelReleaseDate`, GE/Café/Haier 최초 유통일뿐) 앱의 최초 발견 시점에 의존한다. 처음 조회한 모델은 기준선이라 신제품이 아니다. 같은 소분류를 며칠~몇 주 간격으로 다시 검색해야 하므로 주기적 재검색(예약/배치) 기능이 필요.
- [ ] 상 | **사이트 '최신순 정렬'을 최근성 근사로 사용**(목록 순서=최신순 순위): Samsung US `sort=newest`(확인), LG US Coveo `sortCriteria="@ec_creation_date descending"`(확인), LG KR 본문 `sortType="sort_new"`(확인), GE Searchspring `sort.product_first_distribution_date=desc`(미확인), OCC(Whirlpool/KitchenAid/Maytag 계열) 쿼리의 `relevance` 자리를 `newestProduct`로(미확인), Beko UK `?sort=age`(확인), Siemens DE `ONLINE_DATE` 정렬(파라미터 미확정), Samsung KR `sortType=20`(미확인). Bosch US/AEG/Miele/Electrolux/Frigidaire/Viking/Bertazzoni/Smeg는 최신순 없음.
- [ ] 중 | **검색 화면의 3분위(Budget/Mid/Premium)를 5단계로 바꿀지 확인.** 지금은 선별 기능(`/match`)만 5단계 + 근접도 점수. 검색 결과 밴드는 그대로.
- [ ] 중 | 평점·리뷰 수 미제공 브랜드: Thermador, Gaggenau, Monogram, Viking, Wolf, Smeg, Bertazzoni, Hisense, Panasonic, Fisher & Paykel(Bazaarvoice 위젯이 클라이언트 렌더). Panasonic의 Bazaarvoice는 passkey가 필요한 제3자 호출이라 쓰지 않았다(허용할지 결정 필요). electrolux_de 목록 평점 필드는 있으나 값이 전부 0. Frigidaire/Electrolux US는 평점·NEW 필드 양성값을 라이브로 못 봄(OCC 표준명 가정).
- [ ] 중 | NEW 표시 양성 사례를 못 본 어댑터: Viking, Hisense, Smeg, Wolf(`news_to_date`), Frigidaire/Electrolux, Thermador/Gaggenau/Bosch/Siemens(`isNewProduct`). 코드는 있으나 라이브 검증 불가. Bertazzoni는 레인지 26개 전부 NEW라 엔진이 자동 무시(`distrusted_new_flags`).
- [ ] 중 | 선별 결과 Excel 출력, '수집된 제품을 기준 모델로 선택'하는 입력 방식은 미구현(이번에는 스펙·가격 직접 입력만).
- [ ] 하 | LG US 목록에는 리뷰 수가 없어 상세 수집 후에만 채워짐. GE/Café 날짜는 `release_src='distribution'`(출시일과 다를 수 있음). Fisher & Paykel `ARRIVING NOV 2026`은 출시 예정이라 신호로 쓰지 않음.
- [ ] 하 | 이력(`seen_models`)은 만료되지 않고 사라진 모델도 남는다(`last_seen`으로 단종 추정은 가능). 가격 변동 이력은 아직 저장하지 않음.

## 참고
- 인수인계: `docs/HANDOFF.md`, 설계: `docs/DESIGN_FILTERS.md`.
- 서버 실행: `run_server.bat` 또는 `python server.py` -> http://127.0.0.1:8765
- 실제 모델 검증 스크립트: `python canon_check.py`, `python template_check.py [--offline|--probe 항목]`
