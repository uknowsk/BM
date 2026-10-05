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

## 남은 일
1. 사용자 항목 엑셀 템플릿 기반 정리 기능 (사용자가 샘플 엑셀을 줄 예정).
2. 스펙 수준 필터(Phase B), 유럽(de/uk)·남미(br) 어댑터(Phase D), 환율표, 어댑터가 목록 단계 `attrs`를 채우도록 개선.
3. 실제 서버로 새 비교 패널(핵심/전체 토글, 원문 툴팁)을 검색-수집 흐름에서 눈으로 확인 (스크린샷 도구가 불안정해 아직 못 함).
4. 가정 확인 필요: 복합 연료 레인지=가스오븐, 라디언트=인덕션/가스 제외 모든 전기 레인지, Samsung KR 콤팩트 오븐(NQ50)=SCO, GE 오버더레인지 Advantium=SCO.

## 알려진 한계
- 필터 대부분은 이름에서 추정한 값 또는 상세 수집된 제품에만 정확함.
- 한국어 번역·모드 추출은 로컬 LLM 속도에 좌우됨 (제품당 수 분).
- 스크린샷 도구가 불안정해 UI는 텍스트·DOM 위주로 확인했음.
