# Gauge — 가전 경쟁사 벤치마킹

브랜드·제품군·시장을 고르면 경쟁사 제품의 **스펙, POD(핵심 셀링 포인트), 동작 모드**를 수집해 한 표로 비교하고 엑셀로 내보내는 로컬 웹앱입니다. 외부 유료 API 없이 LM Studio의 로컬 모델로 번역·추출·항목 통합을 처리합니다.

## 지원 범위
- 브랜드: Samsung, LG, KitchenAid, GE, Whirlpool, Bosch (미국), Samsung·LG (한국)
- 제품군: 냉장고, 세탁기/건조기, 조리기기 (소분류: 프렌치도어, 드럼, SCO(스피드쿡 오븐), 가스오븐, 인덕션 등)
- 시장: 북미 활성, 한국 활성(Samsung·LG), 유럽·남미 등은 준비 중

## 실행
```powershell
pip install -r requirements.txt
playwright install chromium
python server.py
```
브라우저에서 http://127.0.0.1:8765 를 엽니다. Windows에서는 `run_server.bat` 더블클릭으로도 실행됩니다.
화면 없이 UI만 시험하려면 `FRIDGE_MOCK=1`을 설정하고 실행하세요 (가짜 데이터).

## 로컬 모델 (LM Studio, 선택)
`http://localhost:1234/v1` 에 다음 모델을 올려 두면 번역, 모드 추출, 항목 통합의 정밀도가 올라갑니다. 없으면 사전과 규칙으로만 동작합니다.
- `qwen/qwen3-8b` — 추출·번역·판정
- `qwen2.5-vl-7b-instruct` — 스캔 PDF OCR
- `text-embedding-bge-m3` — 항목 의미 매칭

## 크롤링 모드
`FRIDGE_BROWSER_MODE=auto|headless|visible` (기본 auto: headless 먼저, 막히면 창을 띄운 브라우저로 재시도). 쿠키 배너는 거절을 먼저 시도하고, 진행을 막을 때만 허용합니다.

## 테스트
```powershell
python tests/test_catalog.py
```
`tests/test_*.py` 각각이 단독 실행되는 스크립트입니다 (pytest 불필요).

## 구조
- `*_us.py`, `*_kr.py` — 브랜드·국가별 어댑터 (`discover`, `scrape`)
- `catalog.py` — 제품군 트리, 시장·국가, 어댑터 레지스트리
- `canon.py`, `compare_model.py`, `features.py` — 의미 기반 항목 통합과 비교 모델
- `service.py`, `server.py`, `store.py` — 검색·수집 작업, API, SQLite 캐시
- `excel_writer.py` — 제품이 열로 놓이는 `Compare` 시트와 `Mapping` 검수 시트
- `ko_en.py` — 한국어→영어 번역 계층 (용어 사전 + 로컬 LLM)
- `web/` — 프런트엔드, `docs/` — 설계 문서, `docs/HANDOFF.md` — 작업 인수인계

## 주의
- 각 사이트의 이용약관과 robots.txt를 확인하고 요청 간격을 지켜 사용하세요. 어댑터는 요청 사이에 1초 이상 간격을 둡니다.
- 수집한 PDF·이미지·캐시(`downloads/`, `data/cache.db` 등)는 저장소에 포함하지 않습니다.
- 사용자 항목 정의로 항목을 합치거나 나누려면 `data/canon_overrides.json`을 만드세요 (`README_WEB.md` 참고).
