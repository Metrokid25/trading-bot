# Mentor Signal Paper Ingest

`POST /api/signals/mentor`는 Mentor Signal Reader 전용 Paper 관심종목 수신 경로다.
기존 변경 API와 동일하게 `X-Web-Key`를 요구하며 다음을 모두 재검증한다.

- `mode=paper`, `KIS_ENV=PAPER`
- 설정된 `MENTOR_AUTHOR_ID`와 payload 작성자 정확 일치
- `signal_type=ADD_WATCH`
- `confidence >= MENTOR_SIGNAL_CONFIDENCE_THRESHOLD`(기본 0.95)
- StockMaster의 종목코드와 정규화 종목명 정확 일치
- 게시 시각이 현재 기준 24시간 이내이며 미래 5분을 넘지 않음
- `(article_id, revision hash, stock code, signal type)` 멱등성

관심종목 변경 전에 감사행을 `processing`으로 예약한다. UNIQUE 키와 payload hash로
다중 worker의 동일 요청은 합치고, 같은 키의 다른 payload는 409로 거부한다. upsert 뒤
감사 완료가 끊기면 예약 시각과 종목별 `tracking_start_date`로 실제 등록을 찾아 원래의
`registered=true` 결과를 복구한다. 손상된 과거 응답도 안전한 최소 중복 응답을 반환한다.

검증 후 `멘토 자동픽 · <섹터>` 전용 namespace로 `SectorStore.upsert_sector`를 호출해
동명 수동 픽의 만료를 바꾸지 않는다. 표시명이 예약 prefix와 같아도 `[mentor:`
원천 pick만 재사용하므로 수동 pick과 구조적으로 분리된다. 중복 섹터 찾기/병합도
원천 그룹을 보존해 수동과 mentor pick을 합치지 않는다. 일반 관심종목 조회에는 즉시 보이지만 종목별
등록일 당일 Paper replay에서는 제외되고 익일부터 편입된다. REAL 섹터 감지 입력에서도
mentor 원천을 제외한다. 주문, KIS 체결, `main.py` 또는 실전 엔진은 호출하지 않는다.

Reader는 Archive의 레거시 작성시각을 timezone-aware ISO-8601로 변환해 전송한다.
관심종목은 `SectorStore.upsert_sector` 트랜잭션과 기존 membership trigger를 거쳐
저장되며, Paper Runner는 다음 주기 `load_universe`에서 이를 동적으로 읽는다.

2026-08-07 검증 기준: 전체 `484 passed, 1 skipped`(기존 warning 1건), 실제 FastAPI Fixture
E2E에서 감사행 `registered`, 전용 watchlist 등록, 일반 조회 즉시 노출, Paper Runner
당일 제외·익일 편입을 확인했다. 운영 미니PC 재기동과 paper 전환은 수행하지 않았다.

환경변수와 StockMaster 스냅샷 내보내기:

```text
MENTOR_AUTHOR_ID=
MENTOR_SIGNAL_CONFIDENCE_THRESHOLD=0.95
MENTOR_SIGNAL_MAX_AGE_HOURS=24
```

```powershell
.\.venv\Scripts\python.exe scripts\export_mentor_stock_master.py `
  --output C:\bot-shared\mentor-stock-master.json
```
