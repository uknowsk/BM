# 인수인계 (새 대화에서 이어가기용)

작성 시점 기준 상태 요약. 새 대화는 이 문서와 `docs/DESIGN_FILTERS.md`, `README_API.md`, `README_WEB.md`를 먼저 읽는다.

## 프로젝트
Gauge: 경쟁사 가전 제품 스펙·POD·모드를 수집해 비교하는 로컬 웹앱 (Python FastAPI + 바닐라 JS).
위치: `C:\Users\uknow\AI\Bench marking\fridge_scraper`. 실행: `python server.py` 또는 `run_server.bat` -> http://127.0.0.1:8765

## 사용자 지시 (지켜야 할 규칙)
- 답변은 한국어. 사용자는 Windows PowerShell 5.1 사용 (`&&` 불가, 명령은 줄 나눠서 안내).
- 크롤링은 headless 우선, 막히면 headless=False 폴백 (`FRIDGE_BROWSER_MODE=auto|headless|visible`).
- 쿠키 배너: 거절 먼저, 진행을 막을 때만 허용.
- 기본 시장 = 북미(na). 1차 국가 = kr, us, de/uk. 브랜드당 1개 모델 제한은 제거됨 (한 번에 최대 12개, `MAX_COLLECT`).
- 컨텍스트가 85%에 가까우면 압축하지 말고 이 문서를 갱신한 뒤 새 대화로 이어간다.
- 작업이 끝나면 `https://github.com/uknowsk/BM.git` 에 반영 (git 저장소 아직 없음: init, .gitignore, 커밋, 원격 연결, 푸시).
- 비교표는 의미 기반으로 항목을 묶어야 한다 (Size/Dimensions 같은 동의어를 다른 행으로 두지 말 것).
- SCO = Speed Cook Oven (SCR 아님). 사용자가 정정함.
- 나중에: 사용자가 항목 목록 엑셀을 주면 그 항목 기준으로 경쟁사 스펙·기능·모드를 정리하는 기능.

## 구조
- 어댑터: `{samsung,lg,kitchenaid,ge,whirlpool,bosch}_us.py`, `{samsung,lg}_kr.py` (`discover(subcategory, limit)`, `scrape(url)`).
- 핵심: `catalog.py`(제품군 트리·시장·어댑터 레지스트리), `schema.py`, `service.py`, `server.py`, `store.py`(SQLite 캐시, `PARSER_VERSION`), `filters.py`, `units.py`, `pod.py`, `features.py`, `compare_model.py`, `canon.py`(의미 기반 항목 통합), `excel_writer.py`, `common.py`(다운로드 보안), `ko_en.py`(한국어 번역 계층), `modes.py`/`ocr.py`/`llm.py`(로컬 LLM).
- LM Studio: `qwen/qwen3-8b`(추출·번역·판정), `qwen2.5-vl-7b-instruct`(OCR), `text-embedding-bge-m3`(임베딩).
- 엑셀: 첫 시트 `Compare`(제품이 열, 항목이 행), `Mapping`(항목 매핑 검수), 그 뒤 데이터 시트들.
- 게이트 훅(GateGuard): 새 파일 생성·편집·서버 종료 전에 사실 4가지(영향 범위, 호출 위치, 데이터 구조, 사용자 지시 원문)를 먼저 제시해야 통과.

## 완료된 작업 (2026-10-05 기준)
- 의미 기반 항목 통합 (`canon.py`, `compare_model.py`, 엑셀 `Mapping` 시트, UI 토글): 정밀도 1.000 / 재현율 0.997 (사례 96건). 실제 임베딩으로 전후 행 수를 다시 재려면 LM Studio에 bge-m3를 올리고 `python canon_check.py` 실행.
- `scr` -> `sco`(Speed Cook Oven) 정정 완료 (핵심 코드, 어댑터 8개, 캐시 버전 4).
- 한국 어댑터(samsung_kr, lg_kr)와 번역 계층(`ko_en.py`) 완료.
- 전체 테스트 23개 스크립트 통과. git 저장소 초기화, 원격 `https://github.com/uknowsk/BM.git` 푸시는 이 문서가 커밋될 때 진행.

- 비교표 정리: 액세서리는 `액세서리·옵션` 섹션으로 분리, 하위 값은 부모 그룹으로 묶음, 랙 수 등은 목록에서 산출. 임베딩 단계 사용 여부는 Mapping 시트와 `canon_stats`로 표시. 실제 임베딩 기준 정밀도 1.000 / 재현율 1.000 (사례 105건).
- 사용자 양식 기반 분류 (`template.py`, `template_writer.py`, `web/js/template.js`, `/api/template/*`): 구분/항목/동의어/단위/유형 열을 읽어 제품 데이터를 양식 순서로 채워 엑셀 출력. 값 상태는 found/absent/unknown/derived. 샘플 양식은 `make_template_sample.py` 또는 `/api/template/sample`. 실제 양식 샘플을 받으면 열 구성에 맞춰 다듬을 것.
- 전체 테스트 24개 스크립트 통과.

- 실제 LM Studio 모델로 양식 매칭 검증 (`python template_check.py`, `--offline`, `--probe 항목`): 40개 항목 중 37개는 동의어/정확 일치/표준 항목 사전으로 해결. 미해결 항목은 상위 5개 후보를 LLM이 고르는 방식, 예/아니오 항목은 관련 행 묶음(`family`)으로 판정. 임베딩은 후보 탐색용으로만 쓴다.

## 모델 사용 시 알게 된 점
- 임베딩 점수는 기준이 일정하지 않다 (bge-m3: Size~Dimensions 0.704는 합쳐야 하고, 냉장실~냉동실 용량 0.892는 합치면 안 됨). 점수 문턱만으로 병합하지 말고 값 형태/단위/구분 단어 가드와 LLM 판정을 함께 쓴다.
- `text-embedding-bge-m3`가 기본이 맞다. `bge-reranker-v2-m3`는 임베딩이 아니라 재순위 모델이라 유사도에 쓰면 안 되고, qwen3-embedding과 nomic은 한국어-영어 쌍과 속도에서 밀린다.
- 로컬 qwen3-8b는 보수적으로 "없음"이라고 답하는 경향이 있다. 사용자 양식의 동의어 칸이 가장 확실한 정밀도 수단이다.
- 예/아니오 항목의 LLM 선택은 항목과 구분되는 단어를 공유해야만 인정한다 (코셔 인증 행이 ENERGY STAR의 답으로 선택된 오탐을 막기 위함).

## 작업 시 주의
- 셸 heredoc으로 파이썬 파일을 만들면 `\b` 같은 백슬래시 정규식이 백스페이스 문자로 깨진다. 파일은 Write/Edit 도구로만 쓰고 raw 문자열을 쓸 것.
- 서버를 종료할 때(Stop-Process)와 새 파일/편집 시 GateGuard 훅이 사실 4가지를 먼저 요구한다.

## 남은 일
1. 사용자가 실제 분류 양식 엑셀을 주면 양식 파서를 그 형식에 맞춰 보강하고, 실제 LM Studio 모델로 양식 매칭을 검증.
2. 스펙 수준 필터(Phase B), 유럽(de/uk)·남미(br) 어댑터(Phase D), 환율표, 어댑터가 목록 단계 `attrs`를 채우도록 개선.
3. 실제 서버로 새 비교 패널(핵심/전체 토글, 원문 툴팁)을 검색-수집 흐름에서 눈으로 확인 (스크린샷 도구가 불안정해 아직 못 함).
4. 가정 확인 필요: 복합 연료 레인지=가스오븐, 라디언트=인덕션/가스 제외 모든 전기 레인지, Samsung KR 콤팩트 오븐(NQ50)=SCO, GE 오버더레인지 Advantium=SCO.

## 브랜드 확장 진행 상황 (2026-10-06, 워크트리 claude/suspicious-swanson-8f4490, 미커밋)
- 계획/결정: `docs/BRAND_EXPANSION.md`, 피드백: `docs/TODO.md`. 사용자 지시: 제품 검색 등은 우선 **조리기기만** 진행(냉장고·세탁기 어댑터는 후순위, TODO로).
- 단계 0 완료: `gas_cooktop` 소분류(US 어댑터 6개 지원, 한국 2개 미지원), 필터 정리, 브랜드 30개 메타/그룹 UI/검색 상한 120, `i18n.py`+de/fr 용어집, 유럽 가격·에너지 등급 파서. 테스트 27개 통과. `filters._WIDTH_RE`에 `''`, `″` 추가(30인치 0건 보고 대응).
- 단계 1 완료(조리기기만): 신규 어댑터 `<slug>_<cc>.py` 28개(+공용 `_electrolux_common.py`, `_subzerowolf_common.py`; maytag/jennair/amana, thermador/gaggenau us·de/siemens_de, frigidaire/electrolux us·de/aeg de·uk, cafe/monogram/haier/fisherpaykel, viking/wolf/bertazzoni/smeg, miele_de, beko us·uk/hisense/panasonic). 미지원: Sub-Zero(냉장고 전용), Liebherr(냉장고 전용, `liebherr_de.py`는 SUPPORTED 비어 있음), De Dietrich(사이트 다운). 활성 브랜드 27/30.
- 단계 2 완료: 허용 호스트 반영(`common.py`, 정확한 호스트만), 전체 테스트 51개 통과, 비밀 정보 검사 이상 없음, 브랜드 x 국가 33개 조합 end-to-end(검색 1건 -> 수집) 전부 성공(워크트리 서버 8799). 유럽 전용 브랜드(Miele, Siemens, AEG)는 `/api/brands?region=eu`에서만 활성(설계). 사용자 서버(8765)는 아직 이전 코드: 메인 체크아웃에 브랜치를 병합한 뒤 재시작해야 반영됨.
- 남은 일: 메인 병합 + 8765 재시작(사용자 확인), 후속 과제는 `docs/TODO.md` F절(약관 확인, 냉장고·세탁기 확장 등).

## 경쟁 모델 선별 기능 (2026-10-07)
- 요청: 당사 제품/개발 스펙의 최적 경쟁 모델 선별(가격 유사, 최근 출시, 소비자 호응, 브랜드별 신제품 구분과 트렌드). 사용자 결정: 스펙·가격 직접 입력, 출시는 근사 조합(근거 표기), 호응은 브랜드 사이트 평점만, 가격은 **5단계(5분위) + 근접도 점수**.
- 구현: `match.py`(순수 함수: 5단계, 근접도, 스펙 적합, 최근성, 베이지안 평점, 신제품 레이더), `store.py`의 발견 이력(`seen_models`/`seen_groups`, 만료 없음, baseline 규칙), `service.py`가 목록/수집 때 기록, `server.py`의 `POST /api/match`, `GET /api/launches`, 페이지 `/match`(`web/match.html`, `js/match.js`, `css/match.css`), 신호 필드(`schema.ProductRecord`와 `Candidate.attrs`: rating, review_count, is_new, release_date, release_src), 어댑터 신호 추출(6개 에이전트), `PARSER_VERSION`=5. 문서: `docs/MATCHING_DESIGN.md`, `README_API.md`.
- 설계 결정: 미확인은 중립 점수(재분배하면 빈 모델이 이김), 가격 없는 모델은 순위 제외, 폭이 허용 오차를 벗어나면 제외, 한 그룹 NEW 표시가 60% 이상이면 무시.
- 남은 일: `docs/TODO.md` G절(주기적 재검색, 최신순 정렬 활용, 5단계를 검색 화면에도 적용할지 등).

## 브라질(남미) 추가 (2026-10-08)
- 시장 `sa`(남미)는 어댑터가 하나라도 있으면 자동 활성화(원래는 '준비 중'). 브라질 어댑터 8개(조리기기만): `samsung_br`, `lg_br`, `electrolux_br`, `brastemp_br`, `consul_br`(공유 `_whirlpool_br_common.py`, VTEX), `smeg_br`, `miele_br`, `panasonic_br`. 새 브랜드 Brastemp, Consul(총 32개). 번역 `i18n.get('pt')`, 단위 `units.br_energy_class/parse_kwh_per_month`, 국가 `catalog.ADAPTER_COUNTRIES`에 br. 허용 호스트는 `common.py`(정확한 호스트만).
- 검증: 전체 테스트 61개 통과, 브랜드별 end-to-end(검색 1건 → 수집) 8/8 성공. 후속은 `docs/TODO.md` H절(가격 해석 확인, 약관, 미지원 브랜드 등).

## 다음 대화 시작 안내 (2026-10-09)
- **현재 상태**: 메인 = 원격 `main` = 브랜치 `claude/suspicious-swanson-8f4490` = `690b822`. 사용자 서버(8765)는 메인 폴더에서 `python server.py`로 떠 있음(Claude 앱이 재시작되면 꺼짐: 다시 켜야 함. 로그는 `server.log`/`server.err`, `.gitignore` 대상). 기능: 30→32 브랜드, 가스 쿡탑, 조리기기 어댑터(미국·한국·독일·영국·브라질), 경쟁 모델 선별(`/match`), 5단계 가격, 주기적 재검색, 남미(브라질).
- **사용자 답변 대기**: (1) Samsung BR 가격 기준(현재 18회 무이자 가격, 현금가는 extra_specs) (2) Panasonic BR을 별도 공식 스토어 소스로 쓰는 것 수용 여부 (3) 이용약관 확인(TODO F·H절) (4) User-Agent를 앱을 밝히는 값으로 바꿀지(지금은 일반 브라우저 값 `common.UA`).
- **사용자가 할 일**: `/match` 맨 아래에서 첫 자동 재검색 예약 만들기(만들면 약 1분 안에 실제 사이트 검색이 시작되고 그 결과가 신제품 판단의 기준선이 됨).
- **후속 후보**(상세는 `docs/TODO.md`): 냉장고·세탁기 어댑터 확장(Sub-Zero/Liebherr 포함), 선별 결과 Excel 출력과 '수집된 제품을 기준 모델로' 입력, 사이트별 '최신순 정렬'을 최근성 근사로 활용(G절), 좁은 화면(390px) 가로 스크롤, FastAPI `on_event` 사용 중단 경고를 lifespan으로, 남미 나머지 국가(스페인어), 서버 자동 시작(윈도우 시작 시).
- **작업 방식 메모**: 워크트리에서 개발 → 전체 테스트(`$env:FRIDGE_I18N_LLM=0` 후 `tests\test_*.py`를 하나씩, 61개 모두 통과가 기준) → 커밋(attribution 줄 없이) → 메인에서 `git merge --ff-only <브랜치>` → `git push origin main` → 8765 재시작. 사용자 서버(8765)를 건드리기 전 반드시 사용자 요청 확인. 시험 서버는 `FRIDGE_PORT=8799 FRIDGE_SCHEDULER=0`(예약이 실제 사이트로 나가지 않게)로. GateGuard 훅이 새 파일·편집·삭제·서버 종료 전에 사실(importer, 영향 함수, 데이터 구조, 사용자 지시 원문)을 요구함. 브랜드별 end-to-end 확인 스크립트는 세션 임시 폴더에 있었음(없으면 `/api/search` → `/api/collect`로 브랜드별 1건 수집하는 짧은 스크립트를 다시 작성).
- **규칙 재확인**: 크롤링은 robots.txt/약관 존중, 요청 간격 1초 이상(어댑터가 `REQUEST_DELAY_S` 선언 시 그대로, Viking 10초), 차단은 우회 금지·미지원 보고, 쿠키 배너는 거절 먼저, 비밀/캐시/다운로드 커밋 금지, 파이썬은 heredoc 금지(Write/Edit만).

## 알려진 한계
- 필터 대부분은 이름에서 추정한 값 또는 상세 수집된 제품에만 정확함.
- 한국어 번역·모드 추출은 로컬 LLM 속도에 좌우됨 (제품당 수 분).
- 스크린샷 도구가 불안정해 UI는 텍스트·DOM 위주로 확인했음.
