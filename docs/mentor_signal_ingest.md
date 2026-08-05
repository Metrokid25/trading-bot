# Mentor Signal Paper Ingest

`POST /api/signals/mentor`는 Mentor Signal Reader 전용 Paper 관심종목 수신 경로다.
기존 변경 API와 동일하게 `X-Web-Key`를 요구하며 다음을 모두 재검증한다.

- `mode=paper`, `KIS_ENV=PAPER`
- 설정된 `MENTOR_AUTHOR_ID`와 payload 작성자 정확 일치
- `signal_type=ADD_WATCH`
- `confidence >= MENTOR_SIGNAL_CONFIDENCE_THRESHOLD`(기본 0.95)
- StockMaster의 종목코드와 정규화 종목명 정확 일치
- `(article_id, revision hash, stock code, signal type)` 멱등성

검증 후 기존 `SectorStore.upsert_sector`를 호출한다. 따라서 기존 수동 웹 등록과
동시에 사용할 수 있고, `universe_membership_events` 트리거와 Paper Runner의 매 주기
`load_universe` 경로도 그대로 재사용한다. 이 endpoint는 관심종목만 추가하며 주문,
KIS 체결, `main.py` 또는 실전 실행 엔진을 호출하지 않는다.

환경변수와 StockMaster 스냅샷 내보내기:

```text
MENTOR_AUTHOR_ID=
MENTOR_SIGNAL_CONFIDENCE_THRESHOLD=0.95
```

```powershell
.\.venv\Scripts\python.exe scripts\export_mentor_stock_master.py `
  --output C:\bot-shared\mentor-stock-master.json
```
