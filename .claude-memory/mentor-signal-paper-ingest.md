# Mentor Signal Paper Ingest

- 브랜치: `feature/mentor-signal-ingest`
- 수신: `POST /api/signals/mentor`, 전 변경 API 공통 `X-Web-Key` 보호
- 게이트: `mode=paper`, `KIS_ENV=PAPER`, 정확한 `MENTOR_AUTHOR_ID`,
  `ADD_WATCH`, 기본 confidence 0.95, StockMaster 이름/코드 일치, 게시 24시간 이내
- 멱등키: `(article_id, article_revision_hash, stock_code, signal_type)`
- 저장: payload hash가 붙은 processing 감사 예약 → `멘토 자동픽 ·` 전용 섹터 →
  종목별 `tracking_start_date` 기준 익일 Paper 편입
- 안전: 수동 섹터 TTL 불변, REAL scanner 제외, 주문/KIS/`main.py` 호출 없음.
- 2026-08-07 검증: 전체 483 passed, 1 skipped, FastAPI fixture E2E로 감사행·watchlist·
  당일 제외·익일 Paper universe 확인. 미니PC 배포와 paper 전환은 아직 하지 않음.
