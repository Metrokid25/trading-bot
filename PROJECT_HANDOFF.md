# 트레이딩봇 핸드오프 문서

> 다음 AI 세션이 이 프로젝트를 즉시 이해하고 이어받기 위한 기술 문서.
> 마지막 업데이트: 2026-05-10
> 기준 커밋: `400205b feat(phase2.5): D4 - Codex MEDIUM (silent no-op) 수정 + D4/D5 concurrency contract 명시`

---

## 1. 프로젝트 정체성

- **목적**: 최종 목표는 한국 주식(KOSPI/KOSDAQ) 자동매매 봇이지만, 현재 Phase 2 범위는 PAPER 검증과 알림 신뢰성 확보다. 실전 자동매매 허용 상태로 해석하면 안 된다.
- **연동**:
  - 시세/분봉/일봉/수급: 한국투자증권 KIS Open API
  - 주문/잔고: 한국투자증권 KIS Open API, `.env` 의 `KIS_ENV` 에 따라 PAPER/REAL 분기
  - 알림/제어: Telegram Bot (`@zzapmoneying_bot`)
  - 백테스트 데이터: tvDatafeed, yfinance, KIS 당일 분봉
- **현재 상태**: Phase 2 섹터 감지 + PAPER 트레이딩 런칭 직전/초기 단계. 알림 영속화 안정성 패치가 `b5eafe0` 에 반영됨.
- **즉시 목표**: PAPER 트레이딩 실행, 첫날 장중 모니터링, alert_history/Telegram/로그 동작 검증.

---

## 2. 현재 Phase

현재 주 작업은 **Phase 2: 섹터 쏠림 감지 + PAPER 트레이딩 운영 검증**이다.

### Phase 2 Stage 1

- active sector pick 을 DB에서 로드한다.
- 종목별 KIS 1분봉과 당일 시가를 조회한다.
- 조건 A: 개별 종목이 거래량 배수, 당일 시가 대비 상승률, 양봉 조건을 통과해야 한다.
- 조건 B: 같은 섹터명 기준으로 조건 A 통과 종목 수가 `SECTOR_B_MIN_PASSED` 이상이면 섹터 신호가 발생한다.
- 신호 발생 시 `alert_history` 에 먼저 기록하고, 그 뒤 Telegram 전송을 시도한다.

Stage 1의 목적은 실전 주문 성과가 아니라 **장중 섹터 쏠림 신호가 안정적으로 기록/전달되는지** 검증하는 것이다.

---

## 3. 핵심 불변식

### 실전 주문 안전

- 실전 자금 주문 경로는 owner의 명시 승인 없이 수정하거나 활성화하면 안 된다.
- `sector_detector` 를 `ExecutionAgent` 에 연결해 자동 주문으로 이어지게 하는 작업은 **Phase 3 scope** 다.
- Phase 2에서는 섹터 감지, PAPER 검증, DB/Telegram 알림 신뢰성 검증까지만 다룬다.

### 알림 영속화

반드시 유지해야 하는 순서:

```text
조건 A/B 통과
-> alert_history INSERT with delivery_status='pending'
-> Telegram 설정/전송 시도
-> delivery_status 업데이트
```

- **insert-before-notify** 가 핵심 불변식이다.
- Telegram이 비활성화되어 있거나, `notify()` 가 False를 반환하거나, 전송 중 예외가 발생해도 이미 INSERT 된 DB row가 쿨다운을 소비한다.
- DB INSERT가 실패하거나 재시도 소진으로 `INSERT_FAILED` 가 반환되면 Telegram 전송을 하지 않는다.
- 쿨다운은 메모리 상태가 아니라 `alert_history` DB row 기준이다. 재시작/다중 워커 상황에서도 이 불변식이 깨지면 안 된다.

### delivery_status 생명주기

`alert_history.delivery_status` 는 다음 값만 허용한다.

| 상태 | 의미 |
|---|---|
| `pending` | DB row 생성 완료, 아직 Telegram 결과 미확정 |
| `sent` | Telegram 전송 성공 |
| `failed` | Telegram 설정은 있었지만 `notify()` 실패 또는 예외 |
| `disabled` | Telegram 앱/chat_id 미설정으로 전송 시도 안 함 |
| `crashed` | 예약된 미래 아이디어. Phase 2에서 owner 승인 없이 복구/감사 로직을 구현하면 안 됨 |

기존 `alert_history` row 는 마이그레이션 시 `sent` 로 백필된다.

### 시간 처리

- 모든 신규 timestamp 생성/저장/비교는 `core/time_utils.py` 를 사용해야 한다.
- DB 저장용 ISO 문자열은 `to_db_iso()` 를 사용한다.
- KST 기준 현재 시각은 `now_kst()` 를 사용한다.
- naive `datetime.now()` 를 운영 코드에 새로 추가하지 않는다.

### KIS 서버 분리

- **시장 데이터 API는 항상 REAL 서버**를 사용한다. VTS/PAPER 서버의 분봉 미지원 문제 때문에 시세는 실전 서버로 고정되어 있다.
- **주문/잔고 API는 `KIS_ENV` 로 ENV-gated** 된다.
  - `KIS_ENV=PAPER`: 주문/잔고는 모의투자 서버와 PAPER TR 사용
  - `KIS_ENV=REAL`: 주문/잔고는 실전 서버와 REAL TR 사용
- PAPER 모드에서도 REAL 시세 키가 필요하다.
- 토큰 캐시는 REAL/PAPER 분리 파일을 사용한다.

---

## 4. 주요 파일

```text
C:/trading-bot/
├── main.py                     # 봇 엔트리, Telegram/agents/scheduler 구동
├── config/
│   ├── settings.py             # .env 로드, KIS/Telegram 설정
│   └── constants.py            # 전략/시간/섹터 감지/리스크 상수
├── core/
│   ├── kis_api.py              # KIS REST 래퍼. 시세=REAL, 주문/잔고=ENV gated
│   ├── telegram_bot.py         # Telegram client, is_configured(), notify()
│   ├── time_utils.py           # KST timestamp/DB ISO canonical utilities
│   ├── pick_parser.py          # Telegram sector pick 입력 파서
│   └── pick_handlers.py        # /p /picks /extend /archive 등
├── data/
│   ├── sector_store.py         # sector_picks/sector_stocks/alert_history 저장소
│   ├── sector_models.py        # SectorPick/SectorStock/UpsertResult
│   ├── candle_store.py         # 캔들 SQLite + CandleBuffer
│   ├── daily_data.py           # 일봉 MA gate
│   └── flow_data.py            # 외국인/기관 수급 gate
├── agents/
│   ├── sector_detector.py      # Phase 2 Stage 1 섹터 쏠림 감지
│   ├── analysis_agent.py
│   ├── execution_agent.py
│   └── portfolio_agent.py
├── strategy/
│   └── signal.py               # PULLBACK + BREAKOUT dispatcher
├── backtest/
│   ├── engine.py               # BacktestEngine
│   ├── run_v4.py               # v4 단일/소수 종목
│   ├── run_v5.py               # v5/BREAKOUT 검증
│   └── run_batch_v5.py         # v1/v4/v5 배치 비교
└── tests/
    ├── test_sector_detector.py
    └── test_sector_store.py
```

---

## 5. Phase 2 섹터 감지 세부사항

### `agents/sector_detector.py`

- `scan_once()`:
  - 차단 시간대면 스킵
  - active picks 로드
  - pick별 종목을 섹터명 기준으로 전역 합산
  - 같은 `(stock_code, sector_name)` 은 중복 평가하지 않음
- `evaluate_stock()`:
  - KIS 1분봉 최신봉과 직전 `VOLUME_LOOKBACK` 봉 조회
  - 당일 시가 조회. 당일 시가는 날짜별 메모리 캐시
  - 조건 A 계산: 거래량 배수, 당일 수익률, 양봉
- `_emit_alert()`:
  - `try_insert_alert_with_cooldown(... initial_status='pending')` 먼저 호출
  - `COOLDOWN_ACTIVE`: notify/update 없음
  - `INSERT_FAILED`: notify 억제
  - Telegram 미설정: `delivery_status='disabled'`
  - Telegram 성공: `delivery_status='sent'`
  - Telegram 실패/예외: `delivery_status='failed'`

### `data/sector_store.py`

- `alert_history` schema:

```sql
delivery_status TEXT NOT NULL DEFAULT 'pending'
CHECK(delivery_status IN ('pending','sent','failed','disabled','crashed'))
```

- `_migrate_alert_history_v2()`:
  - `delivery_status` 없는 기존 DB를 새 스키마로 변환
  - 기존 row는 `sent` 로 백필
  - 멱등 실행 가능
- `try_insert_alert_with_cooldown()`:
  - `INSERT ... SELECT ... WHERE NOT EXISTS` 로 쿨다운 체크와 INSERT를 원자화
  - 반환값은 `(AlertResult, row_id | None)`
  - `locked/busy` OperationalError 는 100ms, 300ms, 1000ms 재시도
  - 재시도 소진 시 `INSERT_FAILED`

---

## 6. 전략/백테스트 현재 위치

전략 연구 결과는 여전히 다음 판단을 유지한다.

| 버전 | 정의 | 상태 |
|---|---|---|
| v1 | PULLBACK only | 베이스라인 |
| v4 | v1 + 일봉 MA20>60 + 5일 수급 gate | 현 권장 전략 |
| v5 | v4 + BREAKOUT 채널 | 보류. 과다 발화와 손익비 악화 |

`strategy/signal.py` 의 `evaluate_buy(..., allow_breakout=False)` 가 v4 권장 경로다. BREAKOUT 검증 목적이 아니면 `allow_breakout=True` 로 운영하지 않는다.

`backtest/engine.py` 에는 기존 ATR 기반 손절/익절/트레일링, VWAP_BREAK/MACD_FLIP 청산, `eligible_codes`, `allow_breakout` 토글이 있다. 일부 v6 sizing 실험 필드도 존재하지만 현재 Phase 2 운영의 주 관심사는 아니다.

---

## 7. 테스트 상태

`b5eafe0` 기준 전체 테스트:

```text
python -m pytest -v
57 passed
```

주의:

- sandbox 환경에서는 root `test_telegram.py` 가 import 시점에 실제 Telegram HTTPS 요청을 하므로 네트워크 차단 시 collection error 가 난다.
- 네트워크 허용 상태에서는 전체 suite 가 통과했다.
- 핵심 변경 테스트는 `tests/test_sector_detector.py`, `tests/test_sector_store.py` 에 있다.

검증된 항목:

- insert-before-notify 호출 순서
- `pending -> sent`
- `pending -> failed`
- `pending -> disabled`
- Telegram 예외 시 예외 전파 없음
- Telegram 미설정 PAPER 모드에서도 `alert_history` row 유지
- 쿨다운 원자 INSERT
- concurrent insert 에서 1건만 INSERTED
- DB locked/busy retry
- 마이그레이션 멱등성과 기존 데이터 보존

---

## 8. 운영/환경 주의사항

- OS: Windows 11
- Python: 3.14
- 주요 의존성: `httpx`, `websockets`, `aiosqlite`, `pandas`, `numpy`, `pydantic-settings`, `loguru`, `python-telegram-bot`, `pytz`, `tvDatafeed`, `yfinance`, `apscheduler`
- 기본 계정 상태: PAPER 모의투자
- Git 브랜치: `main`
- 원격: `Metrokid25/trading-bot`
- Windows 콘솔 한글 깨짐 방지를 위해 필요 시 `PYTHONIOENCODING=utf-8` 사용

KIS 관련:

- PAPER 모드에서도 시장 데이터는 REAL 서버를 사용하므로 REAL app key/secret 이 필요하다.
- PAPER 주문/잔고는 PAPER 서버와 PAPER TR을 사용한다.
- REAL 주문/잔고는 REAL 서버와 REAL TR을 사용한다.
- 토큰 발급 쿨다운이 있으므로 캐시 파일을 존중해야 한다.
- KIS 일부 TR은 간헐 500이 있을 수 있으며 현재 주요 조회 함수는 백오프 재시도를 갖는다.

Telegram 관련:

- `TelegramBot.is_configured()` 는 `_app is not None` 이고 `TELEGRAM_CHAT_ID` 가 있을 때만 True다.
- `notify()` 실패가 alert row 삭제/재시도 폭주로 이어지면 안 된다.
- delivery_status update 실패 시 row는 `pending` 으로 남을 수 있다. 이 경우 후속 감사/복구 대상이다.

---

## 9. 알려진 리스크

- Phase 2 Stage 1은 아직 첫 실거래일 장중 관측 전이다.
- `delivery_status='crashed'` 는 예약된 미래 아이디어이며, Phase 2에서 owner 승인 없이 복구/감사 로직을 구현하지 않는다.
- root `test_telegram.py` 는 단위테스트라기보다 live smoke test라서 CI/sandbox에서 취약하다.
- BREAKOUT 채널은 현재 운영 금지/보류다.
- `Position.realized_pnl` 분할익절 합산 vs 최종 청산 PnL 더블카운트 가능성은 별도 점검 필요.
- 일일 손실 한도는 백테스트 엔진과 완전 통합되지 않았다.

---

## 10. 즉시 다음 작업

### 지금 바로

1. PAPER 트레이딩을 장 시작 전에 실행한다.
2. Telegram 봇 시작 여부와 `is_configured()` 상태를 확인한다.
3. `sector_scan` APScheduler job 이 매 분 10초에 실행되는지 로그로 확인한다.
4. 첫 섹터 신호 발생 시 다음을 확인한다.
   - `alert_history` row가 먼저 생기는가
   - 최초 status 가 `pending` 인가
   - Telegram 상태에 따라 `sent`/`failed`/`disabled` 로 바뀌는가
   - 실패/비활성화 상태에서도 같은 섹터/stage 쿨다운이 소비되는가
5. 첫날 장중 로그와 DB row를 저장해 이후 Stage 1 backtest 설계에 사용한다.

### 1주일 뒤

PAPER 데이터가 1주일 이상 쌓이면 다음 major task 는 **Stage 1 backtest** 다.

목표:

- 실제 PAPER 기간의 섹터 감지 입력/출력 데이터를 기준으로 Stage 1 신호 품질을 검증한다.
- 조건 A/B threshold 가 너무 빡빡하거나 느슨한지 확인한다.
- Telegram 실패/비활성화와 무관하게 DB 이력이 backtest/audit 데이터로 쓸 수 있는지 확인한다.
- `delivery_status='crashed'` 복구/감사 로직은 예약된 미래 아이디어일 뿐이다. Phase 2에서 owner 승인 없이 구현하지 않는다.

---

## 11. 다음 세션 시작 체크리스트

1. `git status`
2. `git log --oneline -5`
3. `python -m pytest -v`
4. `.env` 의 `KIS_ENV`, REAL/PAPER key, Telegram token/chat_id 확인
5. `db/trading.db` 의 `alert_history` schema 확인
6. PAPER run 로그에서 `sector_scan`, `alert_history`, Telegram delivery status 확인

---

## 2026-04-27 페이퍼 1일차 결과 + 방향 전환

### 인프라 검증
- KIS API 분봉 파라미터 버그 수정 (commit 3176e86)
  FID_INPUT_HOUR_1은 HHMMSS 6자리, 분봉 간격 아님
  get_minute_candles는 1분봉 30개만 지원 (interval 인자 제거)
  호출처 3곳 동기화: kis_api.py / sector_detector.py / analysis_agent.py(dead code)
- KIS rate limit 슬라이딩 윈도우 대응 (commit f46ce54)
  KIS 공식 한도: 실전 20 TPS, 모의 5 TPS, 슬라이딩 윈도우 방식
  _RateLimiter 추가 (deque 기반, 15 TPS 안전 마진)
  시세 함수 5개에 acquire 게이트 (get_current_price, get_minute_candles,
  get_minute_candles_at, get_investor_trend, get_daily_candles)
  매매/잔고 경로(_trade_client)는 무영향 — 실머니 경로 보호 유지
  세마포어 _KIS_CONCURRENCY 8 → 4 축소 (rate limiter가 주 게이트)
  evaluate_stock에 2회 재시도 (0.3s, 0.6s 백오프) 추가
- 09:55 이후 76종목 안정 가동 확인
  500 에러 99.6% 감소 (산발적 1~2건만 발생, 재시도가 흡수)

### 신호 0건의 진짜 원인 (페이퍼 1일차 핵심 발견)
- 광전자(017900) +13.37%, 대한광통신(010170) +18.47%, 한미반도체(042700) +22.79% 폭등
- sector_detector 알림 0건 (4시간 30분 가동, 270회 스캔)
- 진단 스크립트 v3 (scripts/diag_sector_signal.py): 종일 분봉 페이지네이션
  광통신 섹터: 29개 분봉이 조건 A 통과
  반도체톱10 섹터: 19개 분봉이 조건 A 통과
  그러나 같은 1분봉에 같은 섹터 3종목 동시 통과는 0건
- 결론: 봇 버그 아님. SECTOR_B_MIN_PASSED=3 + 1분 정확 동기화 가정이
  현실에서 거의 발생 불가능
- 진짜 섹터 쏠림은 분 단위 동기화가 아니라 30분~수 시간에 걸친 누적 동조

### 방향 전환 — Phase 2.5 데이터 누적 모드
- 신호 알림 봇 → 픽 사후 추적 + 폭발 직전 시그널 데이터 누적 봇으로 역할 전환
- 이유: 임계치 답을 모른 채 신호 띄우기보다 데이터 누적으로 답을 도출하는 게 우선
- ai-moneyingbot 완성 후 두 봇 데이터 합쳐서 trading-bot 신호 룰 재설계
- ai-moneyingbot의 4만 2천 게시물 RAG = 형의 매매 철학 객관화의 원천
- 두 봇은 데이터 영역에서 자연스럽게 결합

### 데이터 누적 결정 사항
- 추적 윈도우: D+20일 (스승님 단타/스윙 스타일 반영)
- 폭발 정의: +10% (VI 트리거 일치, 깔끔한 임계치)
- 펀더멘털 데이터 제외: 스승님 철학 — "주가는 조작, 업황과 수급만 본다"
  PER, PBR, ROE, 부채비율 모두 스키마에서 제외
- 수급 데이터 강화: 외국인/기관/개인 매매 동향이 핵심 컬럼
- 분봉 raw 저장 전략: 옵션 A 채택
  모든 픽의 모든 거래일 분봉 raw 통째 저장
  KIS API 제약: 분봉은 당일만 제공, D+N에 D 분봉 못 받음
  → 매일 장마감 후 active 픽 분봉 일괄 저장 필수
  용량 추정: 1년 약 2GB, SQLite 운영 한계 ~5GB
  5GB 초과 시 년도별 파일 분리 또는 PostgreSQL 마이그
  config 토글: MINUTE_RAW_ARCHIVE_ENABLED (부담 시 끄고 통계만)
- 재픽업 처리: 별도 pick_id + 재픽업 마킹
  새 컬럼: is_repick, prev_pick_id, days_since_last_pick, total_pick_count
  형 직감: 장기 재픽업(91일+)이 폭발 경향 강함 → 정량 검증 가능
- 섹터 단위 재픽업도 별도 추적: sector_pick_events 신규 테이블

### 새 스키마 (확정 6개 테이블)
1. picks — 기존 테이블, 재픽업 컬럼 추가
   추가: is_repick, prev_pick_id, days_since_last_pick, total_pick_count
   추가: initial_price, initial_market_cap, initial_shares
   추가: initial_52w_high_pct, initial_52w_low_pct
   추가: d_minus_5_avg_volume, d_minus_5_return
   추가: d_minus_5_foreign_net, d_minus_5_inst_net
   추가: sector_d_minus_5_avg_return
2. sector_pick_events — 신규
   event_id, sector_name, registered_at_kst
   is_sector_repick, prev_event_id
   days_since_last_sector_pick, total_sector_pick_count
3. pick_daily_tracking — 신규, D+1~D+20 일봉
   pick_id, trading_day, day_offset
   open, high, low, close, volume, transaction_amount
   return_vs_pick, return_vs_prev_close
   vi_count, vi_first_time, upper_limit_hit, lower_limit_hit
   foreign_net, inst_net, individual_net
   kospi_return, kosdaq_return, relative_strength
   sector_avg_return
4. pick_minute_raw — 신규, 분봉 raw 저장
   pick_id, trading_day, bar_time
   open, high, low, close, volume, transaction_amount
5. pick_daily_minute_stats — 신규, 분봉 집계
   pick_id, trading_day, bars_count
   vol_ratio_max, vol_ratio_avg
   vol_x3_count, vol_x5_count, vol_x10_count
   max_1min_return, min_1min_return
   bullish_bar_count, bearish_bar_count
   morning_volume_pct, lunch_volume_pct, closing_volume_pct
6. explosion_events — 신규, +10% 폭발 마킹
   pick_id, explosion_day, day_offset
   peak_return, peak_time

### 다음 작업 (병행)
- ai-moneyingbot Phase 2: "본인확인" false positive 디버깅 (browser.py _BLOCK_CONTENT)
- trading-bot Phase 2.5: 추적 모듈 신규 개발
  sector_detector 알림 로직은 일단 그대로 둠 (인프라 검증 데이터 누적)
  추적 모듈은 별도 신규 모듈로 추가, 기존 코드 충돌 없이
  Claude Code 2개 동시 실행으로 두 봇 병행 가능

### 페이퍼 1일차 메타 교훈
- 페이퍼 트레이딩의 진짜 가치는 인프라 버그(분봉 파라미터, rate limit)와
  설계 결함(임계치 비현실성)을 실머니 들어가기 전에 잡는 것
- 형의 의심("광전자도 한미반도체도 올랐는데 왜 못 잡냐") 한 마디가
  봇 방향 전환의 결정적 트리거였음
- "신호 0건이지만 시스템 정상"을 자동 인정하지 말고, 실제 시장과 대조 검증 필요
- 임계치/구조 결정은 직관 대신 데이터 누적 후 정량 도출

### 미해결 (의도적 보류)
- SECTOR_B_MIN_PASSED 임계치 / 시간 윈도우 도입
  → 1~2개월 데이터 누적 후 정량적 답 도출
- AnalysisAgent retire → Phase 3 작업
- M1~M4 MEDIUM 이슈 (handoff 이전 섹션 참조)

---

## 2026-04-27 Phase 2.5 작업 1단계 완료: DB 마이그레이션 인프라

### 결정 사항
- "picks 테이블"의 실체 = 기존 sector_stocks 테이블 (per-stock 추적 단위)
- A안 채택: sector_stocks에 추적 컬럼 직접 추가
- 추적 5개 테이블의 FK 컬럼명: stock_pick_id → sector_stocks(id)

### 추가된 파일
- scripts/migrations/__init__.py
- scripts/migrations/migration_runner.py — 백업 + 멱등 + 트랜잭션 보호
- scripts/migrations/m001_phase25_tracking.py — Phase 2.5 스키마

### sector_stocks 추가 컬럼 7개
is_repick, prev_pick_id, days_since_last_pick, total_pick_count,
tracking_status, tracking_start_date, tracking_end_date

### 신규 테이블 6개 (schema_migrations 포함)
- schema_migrations: 마이그레이션 버전 추적
- sector_pick_events (7컬럼): 섹터 단위 재픽업 추적
- pick_daily_tracking (24컬럼): D+0~D+20 일봉, UNIQUE(stock_pick_id, trading_day)
- pick_minute_raw (12컬럼): 분봉 raw, UNIQUE(stock_pick_id, trading_day, minute_idx)
- pick_daily_minute_stats (17컬럼): 분봉 집계, UNIQUE(stock_pick_id, trading_day)
- explosion_events (7컬럼): +10% 폭발 마킹, UNIQUE(stock_pick_id, explosion_day)

### 인덱스 4개
- idx_pdt_pick_day, idx_pmr_pick_day_min, idx_ee_pick, idx_spe_sector_at

### 검증 완료
- 기존 데이터 무결성 OK (sector_picks 13행, sector_stocks 96행 변경 없음)
- DEFAULT 값 96행 모두 적용 OK
- 멱등성 재실행 OK (skip 메시지 확인)

### 백로그
- 마이그레이션 runner: 이미 적용된 상태일 때 백업 skip 옵션 추가 (현재는 매 실행마다 백업)

### 다음 단계
Phase 2.5 작업 2번: /add 핸들러에 재픽업 마킹 로직 추가

---

## 2026-04-29 Phase 2.5 작업 2번 완료: /p 핸들러 재픽업 마킹 로직

### 결정 사항
- pandas_market_calendars 도입 — 거래일 수 계산에 사용
- repick 마킹 로직: 섹터별 cross-sector dedup + pick_date 기준 정렬 채택
- Codex adversarial review HIGH 2건 수정 완료
  - cross-sector dedup 데이터 손상 버그 수정
  - pick_date 기준 정렬 버그 수정

### 추가된 파일 / 변경
- deps: pandas_market_calendars 추가
- core/market_calendar.py — 거래일 수 계산 유틸
- /p 핸들러 — 재픽업 마킹 + cross-sector dedup 정렬 로직

### 검증 완료
- pytest 73 passed (커밋: ac7ba33)
- 수동 검증 7/7 PASS

### 다음 단계
Phase 2.5 작업 3번: sector_pick_events 섹터 재픽업 추적

---

## 2026-05-01 Phase 2.5 작업 3번 완료: 섹터 재픽업 추적 (sector_pick_events)

### 커밋 목록 (7개)
- 87a46bd: db — m002 마이그레이션 (trading_days_since_last_sector_pick 컬럼)
- 1270c1b: feat — SectorStore._record_sector_pick_event 헬퍼 + core/market_calendar.count_trading_days_between
- 4d7d4e6: db — m003 마이그레이션 (pick_date 컬럼)
- 2917003: feat — gap 계산 기준을 registered_at_kst → pick_date로 전환
- 08c2ccf: feat — core/pick_handlers.py:266 호출 사이트 record_pick_event=True + TC-Integration1·2
- 2c508ae: fix — B2-D1: _record_sector_pick_event 트랜잭션 분리, best-effort (H1)
- 67b82fb: fix — B2-D2: pick_date < ? AND IS NOT NULL, total_count MAX 누적 (H2/H3)

### 사양 결정 (작업 4/5/6/7번에서도 참조)
- prev lookup 정책: pick_date < ? AND pick_date IS NOT NULL (같은 날/미래/NULL 제외)
- total_count 계산: COALESCE(MAX(total_sector_pick_count), 0) + 1 (NULL 행 포함 누적)

### 격리 원칙 (불변식 #8)
추적 모듈은 본 기능(/p 픽 등록)과 격리 — 추적 데이터 기록 실패해도 본 기능 저장 유지.
_record_sector_pick_event는 best-effort, 트랜잭션 분리로 구현.

### 검증 완료
- Codex adversarial review B2-D1 + B2-D2 둘 다 통과 (no material findings)
- TC-Integration1·2 신규 통과

### 백로그 (작업 3번 이월)
- M1: fresh DB crash — 사전 마이그레이션 미적용 상태에서 _record_sector_pick_event 호출 시
- M3: pick_date 형식 strict 검증 부재
- M5: 마이그레이션 down 미구현
- L1: 추적 모듈 격리 convention-only (코드 레벨 강제 없음)
- L2: 테스트 공백 (sector_pick_events 단독 테스트 부족)

### 다음 단계
Phase 2.5 작업 4번: D+N 일봉 추적 (pick_daily_tracking) — 사양 결정 후 단계 분할

---

## Phase 2.5 작업 4번 사양 결정 항목 (시작 전 합의 필요)

**목표**: sector_pick_events 픽 기록 후 D+1 ~ D+N일 동안 픽 섹터 종목 일봉 OHLCV 자동 수집해서 pick_daily_tracking 적재.

### 결정 필요 항목 7개
1. N (추적 일수): 7일 / 10일 / 20일?
2. 추적 대상: 섹터 전체 종목 / 픽 시점 sector_stocks 종목만?
3. 일봉 수집 시점: 장 마감 후(15:30) 일괄 / 다음날 장 시작 전(08:30)?
4. KIS API: 실전 서버 사용 (시세 ENV 분기 무관)
5. 실패 처리: 재시도 정책 + 영구 실패 마킹 기준
6. 격리: sector_detector 알림 로직과 격리 (불변식 #8)
7. 마이그레이션: pick_daily_tracking 스키마 (event_id FK, ticker, trade_date, OHLCV, status)

### 다음 단계
Phase 2.5 작업 4번: D+N 일봉 추적 (pick_daily_tracking) — 사양 결정 후 단계 분할

---

## 2026-05-06 Phase 2.5 작업 4번 D1·D2 완료: m004 마이그레이션 + KIS 일봉 어댑터

### 단계 분할표

| 단계 | 내용 | 커밋 |
|---|---|---|
| ✅ D1 | m004 마이그레이션 (status/retry_count/event_id 컬럼 추가) | 52fdc75 |
| ✅ D2 | KIS 일봉 어댑터 (DailyOHLCV + fetch_daily_candles_for_pick) | a749e87 |
| 🔜 D3 | DailyTracker 모듈 (수집 로직 + best-effort + DB 적재) | — |
| 🔜 D4 | 16:00 KST 스케줄러 + 통합 테스트 | — |
| 🔜 D5 | 재시도 정책 + 영구 실패 마킹 | — |
| 🔜 D6 | Codex adversarial review + fix | — |

### D2 결정사항 (작업 4 진행 중 확정)

- **모듈 위치**: `core/daily_tracker.py` 신규. `core/kis_api.py` 확장 안 함 — `get_daily_candles()` 이미 존재하므로 재사용
- **DailyOHLCV dataclass**: `trade_date(str 'YYYY-MM-DD')`, `open/high/low/close(int)`, `volume(int)`, `value(int 거래대금 KRW)`
  - 정수형 이유: KIS 응답이 원 단위 정수 문자열로 옴, 소수 없음
  - `trade_date` 형식: `pick_daily_tracking` 스키마(m004)와 일관성
- **재시도 정책**: KIS 4회 재시도 내장 + DailyTracker는 일별 1회 시도, 3일 연속 실패 → `failed_perm` (D5에서 구현)
- **rt_cd 체계적 처리**: 별도 위생 작업으로 분리. D2에서는 TODO 주석만
- **호출 단위**: D+0 포함, 20일 캘린더 범위 KIS 일괄 호출, incremental + 실패 재시도 결합
- **KIS 서버**: 시세 API 불변식 유지 — 항상 REAL 서버 (`_real_client` 고정, `get_daily_candles` 그대로 사용)

### D2 산출물

- `core/daily_tracker.py` 89줄 — `DailyOHLCV` (frozen, slots) + `fetch_daily_candles_for_pick`
- `tests/test_daily_tracker.py` 153줄 — 16 cases (parametrize 포함)
- commit: a749e87

### D2 검증

- `pytest tests/test_daily_tracker.py`: **16 passed in 0.10s**
- 회귀: 기존 test_indicators 3 + test_risk 2 동일 통과. 본 변경으로 인한 회귀 없음
- 참고: `pandas_market_calendars` 미설치 환경에서 5개 모듈 collection error — 기존 환경 문제, 본 변경 무관
- 불변식 8개 영향 없음 (core/kis_api.py 미수정, DB INSERT 없음, 스케줄러 없음)

### 다음 (D3 사양 결정 항목)

D3 시작 전 형이 결정해야 할 항목:

1. **추적 대상 종목 로딩**: `sector_pick_events`에서 어떻게 끌어올지 — `event_id` 기준? `sector_pick_id` 기준? `sector_stocks` JOIN?
2. **INSERT 정책**: 픽 등록 직후 D+0~D+20 빈 행 21개 미리 생성? vs 수집 성공 시마다 INSERT?
3. **UPSERT 충돌 키**: `event_id + ticker + trade_date`? `stock_pick_id + trading_day`?
4. **best-effort 격리**: 추적 모듈 실패가 본 기능 저장에 영향 없도록 (작업 3번 패턴 동일하게)
5. **D3 입력 인터페이스**: DailyTracker가 받을 입력 — 어디서 호출되는가? D4 스케줄러에서만? D3는 함수 정의만?

---

## 2026-05-10 Phase 2.5 작업 4번 D3 완료: DailyTracker + m005/m006 마이그레이션 (Codex HIGH 2 수정)

### 단계 분할표 업데이트

| 단계 | 내용 | 커밋 |
|---|---|---|
| ✅ D1 | m004 마이그레이션 (status/retry_count/event_id 컬럼 추가) | 52fdc75 |
| ✅ D2 | KIS 일봉 어댑터 (DailyOHLCV + fetch_daily_candles_for_pick) | a749e87 |
| ✅ D3 | DailyTracker 모듈 + m005 UNIQUE 스키마 | 8876975 |
| ✅ D3-fix | Codex HIGH 1 수정: UPSERT event 단위 격리 | 8a8187a |
| ✅ D3-fix2 | Codex HIGH 2 수정: sector_pick_events.pick_id NOT NULL FK + m006 | 2406340 |
| ✅ D4 | 16:00 KST 스케줄러 + Codex 4사이클 통과 | 309eb39 → 032a7a5 → 400205b |
| 🔜 D5 | 재시도 정책 + 영구 실패 마킹 | — |

### D3 Codex 재재리뷰 통과 (commit 2406340)

- **HIGH 2 (event 멤버십 모호성) 해결**: `sector_pick_events.pick_id INTEGER NOT NULL REFERENCES sector_picks(id)` 추가
  - 기존 문제: `sector_pick_events`가 `sector_name + pick_date`만 가지고 있어 동일 (sector_name, pick_date)에 여러 `sector_picks` row가 존재할 경우 DailyTracker가 잘못된 stock universe를 추적할 위험
  - 해결: `pick_id` FK로 owning pick 명시 → DailyTracker JOIN에서 `sector_picks` 테이블 완전 제거, `spe.pick_id` 직접 조인
- **변경 파일 6개**: `m006_phase25_event_pick_id.py` (신규) + `migration_runner.py` + `sector_store.py` + `daily_tracker.py` + `test_daily_tracker_d3.py` + `test_sector_pick_event.py`
- **pytest**: 124 passed, 1 skipped (TC13 신규 포함)
- **운영 DB m006 적용 완료**: `2026-05-10T13:37:23` (db/trading.db)

### TC13 회귀 테스트 (신규)

- 동일 `(sector_name, pick_date)`에 서로 다른 `pick_id`를 가진 두 `sector_picks` row + 각각 다른 stock universe
- `ensure_tracking_rows`를 한 event에 대해서만 호출
- 검증: 해당 event의 `pick_id` stocks만 `pick_daily_tracking`에 생성, 다른 `pick_id`의 stocks는 침범 없음

### 백로그 (신규 항목 추가)

**[DEFERRED] 마이그레이션 backfill 정책 통일 (m005, m006)**

- **현재 상태**: m005 (`pick_daily_tracking` UNIQUE 확장), m006 (`sector_pick_events.pick_id` 추가) 모두 non-empty 테이블에 대해 abort 가드만 존재
- **미적용 이유**: 단일 PC + 노트북 git sync 환경, m005/m006 적용 시점 양쪽 모두 대상 테이블 0 rows, 실운영 미수행 → reproducible하지 않음
- **향후 필요 시점**: 다중 머신 배포 또는 외부 사용자 추가 시
- **작업 내용**: 두 마이그레이션 모두 deterministic backfill + ambiguous case 진단 출력 방식으로 재작성 (Codex 권장: unambiguous rows backfill, ambiguous rows 진단 리스트 출력 후 실패)
- **우선순위**: LOW

---

## 2026-05-10 Phase 2.5 작업 4번 D4 완료: 일일 수집 스케줄러 (Codex 4사이클 통과)

### 단계 분할표 최종

| 단계 | 내용 | 커밋 |
|---|---|---|
| ✅ D1 | m004 마이그레이션 (status/retry_count/event_id 컬럼 추가) | 52fdc75 |
| ✅ D2 | KIS 일봉 어댑터 (DailyOHLCV + fetch_daily_candles_for_pick) | a749e87 |
| ✅ D3 | DailyTracker 모듈 + m005 UNIQUE 스키마 | 8876975 |
| ✅ D3-fix | Codex HIGH 1 수정: UPSERT event 단위 격리 | 8a8187a |
| ✅ D3-fix2 | Codex HIGH 2 수정: sector_pick_events.pick_id NOT NULL FK + m006 | 2406340 |
| ✅ D4 | 16:00 KST 스케줄러 본 구현 | 309eb39 |
| ✅ D4-fix | Codex HIGH 1+2 + MEDIUM stale snapshot race 수정 | 032a7a5 |
| ✅ D4-fix2 | Codex MEDIUM silent no-op + D4/D5 contract 명시 | 400205b |
| 🔜 D5 | 재시도 정책 + 영구 실패 마킹 | — |

### D4 구현 내용 (309eb39)

- **APScheduler AsyncIOScheduler**, `CronTrigger(hour=16, minute=0, timezone="Asia/Seoul")`, `misfire_grace_time=300`
- **별도 프로세스**: `main_tracker.py` (sector_detector / main.py와 완전 격리)
- **순차 실행**: 종목별 직렬 + 0.1초 sleep, `today` 파라미터 주입으로 테스트 가능
- **KIS 토큰**: `daily_collection_job` 진입 시 `_ensure_real_token()` 선행 실행
- **per-stock try/except**: 1건 실패해도 배치 계속 진행

### D4 Codex 수정 내용 (032a7a5)

- **HIGH 1**: 스케줄러 타겟 쿼리에 `AND ss.tracking_status = 'active'` 필터 추가 (inactive 종목 수집 방지)
- **HIGH 2**: `_ensure_real_token()` 실패 시 `logger.error + raise` → 배치 전체 중단 (기존: warning 후 계속)
- **MEDIUM stale snapshot race (1층)**: `collect_daily` 호출 직전 per-row status 재확인, non-pending이면 pre-check skip
- **MEDIUM stale snapshot race (2층)**: DailyTracker UPSERT에 `WHERE pick_daily_tracking.status = 'pending'` 가드 추가
- **회귀 테스트**: TC6 (inactive 제외), TC7 (auth 실패 배치 중단), TC8 (pre-check skip)

### D4 Codex 수정 내용 (400205b) — D4/D5 concurrency contract 명시

- **CollectResult Enum 도입**: `SUCCESS` / `SKIPPED_NOT_PENDING` / `FAILED`
  - Enum 방식 채택 이유: 코드베이스 컨벤션 (sector_models.py, sector_store.py, constants.py 모두 Enum 사용)
- **cursor.rowcount 체크**: UPSERT 후 `rowcount == 0` → `SKIPPED_NOT_PENDING` 반환 (silent no-op 차단)
  - INSERT (신규 row): rowcount = 1 → SUCCESS
  - UPDATE (pending → success, WHERE true): rowcount = 1 → SUCCESS
  - 충돌 + WHERE false (race 발생): rowcount = 0 → SKIPPED_NOT_PENDING
- **스케줄러 3분류 카운터**: `success_count / skipped_count / failed_count`
  - pre-check skip → skipped_count + `[D4] row skipped (pre-check)` 로그
  - UPSERT race skip → skipped_count + `[D4] row skipped (race)` 로그
- **`run_daily_collection` 반환**: `dict[str, int]` → 테스트에서 카운트 직접 검증 가능
- **회귀 테스트**: TC14 assertion 수정 (failed_permanent row → SKIPPED_NOT_PENDING), TC15 (KIS fetch 후 UPSERT 전 race 시뮬레이션), TC9 (3분류 카운트 검증)

### D4/D5 concurrency contract (확정)

| 역할 | 담당 |
|---|---|
| status='pending' 행 수집 | D4 (collect_daily) |
| 수집 직전 status 재확인 (pre-check) | D4 스케줄러 |
| UPSERT WHERE status='pending' 가드 | D4 DailyTracker |
| race 발생 시 SKIPPED_NOT_PENDING 반환 | D4 DailyTracker |
| failed_temp / failed_permanent 마킹 | D5 전담 |
| retry_count 관리 + 재시도 로직 | D5 전담 |
| D4는 D5 영역(failed_temp, failed_permanent 설정) 절대 불가침 | 불변식 |

- `CollectResult.SKIPPED_NOT_PENDING` = D5 또는 다른 writer가 이미 status를 mutation한 상태
- D4는 `SKIPPED_NOT_PENDING` 발생 시 skipped_count만 증가, 별도 재시도 없음 (D5 책임)

### D4 검증 완료

- **Codex adversarial review 4사이클**: 1차 (HIGH 1+2+MEDIUM 발견) → 2차 (MEDIUM silent no-op 발견) → 3차 (approve, no material findings) → 4차 (approve, no material findings)
- **pytest**: **135 passed, 1 skipped** (TC1~TC15 D3 + TC1~TC9 D4 포함)
- **회귀 없음**: D3 TC1~TC14 전체 통과 유지

### 다음 단계

Phase 2.5 작업 5번: 분봉 raw 수집 (pick_minute_raw)
- D5 (재시도 정책 + 영구 실패 마킹)를 작업 5번 또는 별도 선행 단계로 처리 여부 결정 필요
- 작업 6번 (분봉 집계), 작업 7번 (폭발 마킹) 대기 중

---

## 2026-06-27 노트북 환경 이관 + 수집 파이프라인 조립

### 환경/운영
- **데이터 적립 기기 = 노트북 확정** (PC 상시 가동 불가). `db/trading.db`가 정식 누적 DB (gitignore라 PC와 미동기화, 분봉은 백필 불가).
- 노트북 venv는 **Python 3.14** → `./.venv/Scripts/python.exe`로 실행. 콘솔 출력엔 `PYTHONIOENCODING=utf-8`.
- `.env`는 노트북에서 새로 세팅 (KIS PAPER + REAL 시세 + 텔레그램). `KIS_HTS_ID`는 미사용.
- **표준 작업 규칙을 `CLAUDE.md`에 명문화** (매 세션 자동 로드 + git 동기화): ① 커밋 전 독립 리뷰 ② 기기 동기화 프로토콜(시작 시 pull / 종료 전 push) ③ 마디마다 문서 최신화. PC↔노트북 충돌 방지가 목적.
- AI 메모리는 `.claude-memory/`를 junction으로 git 동기화 (`.claude-memory/SYNC_SETUP.md`).

### 코드 — 수집 파이프라인 조립 (commit dd49909)
- **진단**: Phase 2.5 분봉 모듈(raw/agg/breakout/pullback/sector_strength)이 부품만 있고 스케줄러 미연결. `ensure_tracking_rows`(픽 이벤트 → 추적행 21개)를 **아무도 호출 안 함** = 핵심 누락.
- **`core/pipeline_runner.py` 신규**: 추적행 생성 → 일봉 → 분봉 raw(NXT 장전 08:00 포함, market_code=UN/floor_hour=8) → 3분봉 집계 → 돌파 마킹 → 풀백 dry-run. 각 단계 best-effort.
- **`main_tracker.py`**: 매일 16:00 `full_pipeline_job` 실행으로 변경.
- 마이그레이션 m001~m009 노트북 DB 적용 완료.
- 코드리뷰 반영: `daily_collection_scheduler.py` loguru `%s/%d`→`{}` 포맷 버그 수정.
- 테스트: 통합 3건 추가, 전체 **262 passed, 1 skipped**.

### 알려진 후속 (미결)
- ~~마이그레이션 전 등록된 픽(웹 4개)은 `sector_pick_events` 없어 추적 안 됨 → 재등록 필요.~~ → **2026-06-28 백필 완료** (아래 섹션).
- 추적행 생성을 **픽 등록 시점**으로 올리면 "16:00 이후 등록 시 그날 누락" 갭 해소 (altitude 개선). → **보류 권고**: 분봉 수집 자체가 16:00에만 일어나 행 생성 시점을 앞당겨도 16:00 이후 등록 픽의 당일 분봉은 못 받음. 파이프라인이 매 16:00 `ensure_all_tracking_rows` 를 이미 호출하므로 이득 한계적. 당일 분봉까지 살리려면 "등록 시점 수집 트리거"가 별도로 필요(더 큰 작업). 형 결정 대기.
- 파이프라인 해피패스(실데이터 raw→집계 체인) 통합 테스트 보강 여지 (현재는 모듈별 개별 테스트로 커버).
- 웹앱(`webapp/`)은 일시 중단 — 봉차트/미니 가로캔들/비트코인 달러화까지 완료(commit f7debcb).

---

## 2026-06-28 추적 갭 해소: 이벤트 누락 픽 백필

### 문제
- 운영 DB 상태: `sector_pick_events` 0행, `pick_daily_tracking` 0행인데 active `sector_stocks` 24종목 존재 = **추적 전혀 안 됨**.
- 원인: 웹으로 등록된 픽 4개(pick 1·2·3·4)가 `record_pick_event=True` 도입 *이전*에 등록돼 이벤트 행이 없었음. 이벤트가 없으면 `ensure_tracking_rows` 가 대상으로 못 잡아 일봉/분봉 수집이 영영 누락(분봉은 KIS 당일만 제공 → 매 거래일 영구 손실).

### 해결 — `scripts/backfill_pick_events.py` (신규)
- 활성 종목 보유하나 이벤트 없는 `(pick_id, sector_name)` 그룹을 찾아 정식 경로와 동일한 `SectorStore._record_sector_pick_event` 로 이벤트 생성 → 그 후 활성 이벤트 전체에 `ensure_all_tracking_rows`(파이프라인과 동일 로직)로 추적행 멱등 생성.
- 멱등 + **자가치유**(이벤트만 있고 추적행 없는 고아도 복구) + 실행 전 `VACUUM INTO` 백업 + `pick_date` 오름차순(재픽업 gap 정합).
- 독립 리뷰(서브에이전트) 반영: ① 이벤트 INSERT를 `BEGIN IMMEDIATE` 트랜잭션으로 감쌈 ② 부분 실패 자가치유(2단계 분리) ③ `busy_timeout` ④ `VACUUM INTO` 백업/`now_kst()` 타임스탬프.

### 적용 결과 (운영 DB)
- 이벤트 3개 생성: event 1 반도체(pick 1, 14종목) / event 2 2차전지(pick 3, 5종목) / event 3 전고체(pick 4, 5종목). 전부 `is_sector_repick=0`.
- 추적행 504개 = 24종목 × 21일(D+0~D+20), 전부 `pending`.
- pick 2는 archived(활성 종목 0) → 대상 제외.
- 백업: `db/trading.db.backup_20260628_*`.
- 검증: 멱등 재실행 0건 / DB 복사본에서 고아·누락 양쪽 복구 시뮬 통과 / `pytest 262 passed, 1 skipped`(회귀 없음).

### 효과
- 다음 거래일(2026-06-29 월) 16:00 파이프라인부터 이 3섹터 24종목이 자동 수집 대상에 편입.
- 단, pick_date(2026-06-27 토)의 D+0 분봉은 이미 소실(백필 불가). D+0 일봉은 KIS 과거 일봉으로 수집 가능.

> 주: 이후 이 픽들은 `scripts/cleanup_test_pick.py` 로 정리하고 웹앱에서 재등록함(아래 백테스트 섹션 참조). 현재 active = 23종목 4섹터.

---

## 2026-06-29 토스 과거 분봉 소스 확보 + 프리장 백테스트 엔진

### 데이터 소스 (큰 돌파)
- **과거 NXT 프리장(08:00~08:50) 분봉을 retail로 못 구하던 벽**을 **토스증권 Open API**로 뚫음. KIS(당일만)/트뷰(KRX 거래소만, 유료도 NXT X)/크레온(NXT historical 미제공) 모두 불가였음. 자세한 소스 조사: `.claude-memory/nxt-premarket-historical-data.md`.
- **토스 `GET /api/v1/candles`**: interval `1m`/`1d`, `before`(ISO) 페이징(200/콜), **과거 1분봉을 프리장 실체결까지 + 애프터 포함 통합 시세로 제공**. 최소 1년 보존 확인. 인증 OAuth2 client_credentials(`POST /oauth2/token`).
- **IP 화이트리스트** 주의: 토스 개발자센터에 현재 공인 IP 등록 필요. **모바일/테더링 IP는 수시로 바뀌어** 재등록 필요할 수 있음(`access_denied: IP address not allowed`). 토큰은 디스크 캐시(`db/toss_token.json`, 24h)로 재발급 최소화.

### 추가 파일/설정
- `config/settings.py`: `TOSS_CLIENT_ID/SECRET/BASE_URL` (`.env`). `.env.example` 갱신.
- `scripts/probe_toss_candles.py`: 캔들 프로브(프리장 실체결/깊이 진단).
- `backtest/toss_client.py`: 토스 캔들 클라이언트(토큰 캐시, before 페이징, 전송예외 재시도). 캐시 `db/toss_candles.db`(gitignore).
- `backtest/run_premarket_pullback.py`: 전략 백테스트.

### 전략 백테스트 (형 가설: 프리장 급등→본장 눌림→저점지지→재폭등 당일스캘핑)
- 진입 v2: 프리장 급등 게이트 → 아침고점 대비 눌림 → **눌림 직전 아침고점 재돌파 시 진입**(칼날 회피).
- 청산(형 실매매 룰): **5분할 익절 +5/10/15/20/25%**(둘째≈정적VI 근사), **첫 익절 후 본절 회귀 시 잔량 전량**, **손절 진입가 -4%**.
- 임계치/종목/기간 전부 CLI 파라미터.

### 6월 결과 + 핵심 발견
- 23종목 6월: 누적 **-26.7%**(38건). **기계적 룰로는 6월 엣지 없음.**
- **robust 발견**: **프리장 급등 +12%+ = 탈진, 일관되게 실패**(승률 17%, 평균 -2.8%) → 회피 필터. (표본 키워도 유지)
- **함정 교훈**: +7~12% "스윗스팟"이 8건→13건으로 표본 키우니 +1.08%→+0.08%(본전)로 증발. **작은 표본 행운 = curve-fitting 위험** 실증.

### 다음 작업 (미완)
1. **out-of-sample 검증**: 6월로 만든 필터를 6월에 보면 순환논리. **4~5월(+다개월) 토스 수집**해서 +12% 컷 등이 유지되는지 확인 필수.
2. **진입 필터 추가**(형 제안, 구현 예정): ① 섹터 대장주(그날 섹터 내 프리장 급등 1등) ② 돌파봉 거래량 N배 ③ 박스권 타이트 다지기 ④ 과열(+12%+) 컷. 단 38건에 필터 다중 적용 시 표본 급감 → 반드시 out-of-sample로 검증.
- universe 23종목 4섹터(MLCC/반도체/양자암호/전력설비) 웹 등록, forward 추적 중.

## 2026-07-03 매수타점 v3 — 스승님(굿머닝) 아카이브 근거 재설계

### 무엇을 했나
- **mentor.db(스승님 카페 글 42,947건, 읽기전용)를 마이닝**해서 급등→눌림→지지→재폭등 패턴의 실제 매수 타이밍 방법을 추출, `evaluate_day_v3`로 구현 (`--mode v3`).
- 핵심 전환: v2 "아침고점 종가 재돌파"(어깨 매수) → v3 **"진바닥 확인 후 무릎 진입"**(29102/30600: "진바닥을 확인하고 다시 치고 올라올 때 매수 = 무릎", 30602: "수급 개선 = 거래량이 터져줘야").
- v3 진입 조건(아카이브 근거 → OHLCV 번역): ① 프리장 급등 게이트(동일) ② 눌림 공략(37232/162616 추격금지) ③ **허리 필터**: 눌림이 급등폭 50% 이탈 시 무효(74834) ④ **다지기**: 저저점 리셋(65844 "파동 끝이 직전 저점보다 높아야") + 2봉 이상(85534) + 다지기 거래량 < 하락구간(49434/29606) ⑤ **진입봉**: 다지기 박스고가 종가 돌파 양봉 + 거래량 ≥ 다지기평균×2(30602/114614) + 아침고점 미만(추격금지). 손절에 구조적 플로어(지지 저점 이탈=정리, 89144) 추가.
- 파라미터: `--waist 0.5 --consol-bars 2 --vol-dryup 1.0 --vol-confirm 2.0`. 전체 근거 인용은 v3 docstring에 article_id로 명기.

### 6월 A/B (23종목, 동일 청산)
- v2: 24건, 승률 41.7%, 평균 -0.84%, 누적 -19.7%, MDD -34.2%
- v3: 6건, 승률 33.3%, 평균 +0.04%, 누적 -0.1%, MDD -4.0% (손실의 대부분을 필터로 회피; 구조적 손절이 -4% 고정손절을 -1.3~-2.7%로 축소)
- **허리 필터가 최대 기여**: `--waist 0` 끄면 32건 -14.9%로 악화. vol-confirm/consol-bars 민감도는 낮음.
- 주의: 6건은 극소 표본 — 결론은 out-of-sample(4~5월) 검증 후에.

### 미해결 (아카이브 근거 있으나 데이터/스코프 밖)
- 10호가 잔량 역전 매수(19815/26566): 호가잔량 데이터 없음 → 불가.
- 20일선 돌파 = 무릎(38275), 5선>20선 골든크로스(89288): 일봉 20일 이력 필요 → 캐시 확장 시 가능.
- 선발대 20% 분할 진입(53601): 백테스트 포지션 모델 확장 필요(현재 전량 진입).
- vol-confirm 2.0/consol-bars 2 등 숫자값은 방향만 아카이브 근거(정량값은 임의) → 파라미터 스윕 대상.

### 2026-07-03 추가: 스승님 6월 글 기반 유니버스 확장 (23→50종목 9섹터)
- mentor.db 6월 글 220건에서 "-종목명" 리스트 파싱 + 직전 테마 헤더로 섹터 추론 → 웹앱과 동일 경로(StockMaster.resolve→upsert_sector)로 등록.
- 신규: 기판 5(LG이노텍·이수페타시스·티엘비·대덕·코리아써키트), 광통신 5(대한광통신·광전자·티엠씨·빛과전자·파이버프로), 반도체 +8(SK스퀘어·삼성전자·한미반도체·피에스케이홀딩스·브이엠·이오테크닉스·원익IPS·하나마이크론), 조선엔진 3(한화엔진·STX엔진·HD현대마린엔진), 원자력 3(두산에너빌리티·비에이치아이·우리기술), AI솔루션 3(마음AI·플리토·오브젠).
- 보류(2회 언급 + 섹터 맥락 불명): 현대모비스/현대오토에버/화신/LG전자/로보티즈/SK텔레콤/한화시스템/하이스틸/한켐. 오타 보정: "이스페타시스"→이수페타시스.
- 주의: ① 아카이브는 6/23까지만 수집됨 ② 트래커 폴링 부하 2배 이상(50종목) — main_tracker KIS 레이트리밋 관찰 필요 ③ 스승님 픽은 스윙 관점도 섞여 있어 프리장 게이트가 실질 필터.

### 2026-07-03 추가 2: v2 지지·다지기 강제 (형 문서와 코드 일치화)
- 발견: 구 v2의 "③ 저점 지지 0.5%"는 추적만 하고 아무것도 안 거르는 죽은 로직이었음(진입 = 게이트+눌림+재돌파 3개뿐).
- 수정: v2 에 지지 이탈 시 다지기 리셋 + **다지기 최소 봉수(consol_bars, 기본 3)** 강제. 다지기 미완 상태의 조기 재돌파는 추격하지 않고 구조 폐기 → 다음 눌림 대기. `--consol-bars 0` = 구버전 동작(52건 −18.8% 재현 확인).
- 6월 50종목: 구 v2 52건 −18.8% MDD−39.8% → **새 v2 40건 −9.7% MDD−26.8%** (v3 는 11건 −4.9% MDD−7.6%, consol 2→3 무영향).
- 관찰: 새 v2 는 v3 가 걸러버리는 초강세일 대박(6/12 한미반도체 +9.1% 등)을 유지 — v2(재돌파+다지기) vs v3(무릎+거래량)은 공격/수비 트레이드오프. 판정은 4~5월 OOS 에서.

### 2026-07-03 추가 3: v3 깔때기 진단 + 개선 필터 4종 (전부 opt-in 파라미터)
- **깔때기 실측(6월 게이트 155 종목-일)**: 눌림 등록 154 → **허리 이탈 무효 128(83%)** → 다지기 완성 47 → 진입 11. 병목은 다지기/거래량이 아니라 **고정 눌림 3% × 비례 허리 50%의 수학 충돌**(급등 +6.4% 미만은 진입 불가능, 중앙값 +7.8%에선 버퍼 0.7%p).
- 신규 파라미터(기본 전부 꺼짐, 켜야 작동): `--pullback-frac`(눌림을 급등폭 비례 되돌림으로+허리도 등록시점 상승폭 기준), `--max-surge`(탈진 컷), `--entry-until`(진입 마감 시각), `--leader-only`(섹터 내 프리장 급등 1등만, 과열 종목은 대장 후보 제외).
- 6월 50종목 인크리멘털: 기본 11건 -4.9% → +비례눌림 8건 -2.8% → +과열컷12 7건 **+1.2%** → +11:30컷 3건 +7.4%(승률 67%) → +대장주 1건. 과열컷+11:30만(비례눌림 없이) 5건 +2.6%.
- 해석: **과열컷·11:30 컷이 6월을 플러스로 뒤집는 두 레버**. 비례눌림은 건수 증가 효과 없었음(예상과 달리 8건, 다만 평균/MDD 개선). 대장주 필터는 월 1건으로 과도(보류). 전부 6월 in-sample 튜닝이므로 **채택 판정은 4~5월 OOS 필수** — 3~5건 표본의 승률 67%는 결론 아님.

### 2026-07-03 추가 4: v4 — 아카이브 조합 확장 (선발대·바닥신호·거래량 조기청산)
- `--mode v4` = v3 + ⑥선발대 2단 진입(53601 "20% 미만"/54546/69581; 트리거 = 거래량 마름(29606) + 쌍바닥 재시험(92522) 또는 아래꼬리봉(69581/68486/68828); 선발대 손절은 허리 붕괴에서만) + ⑦오후 거래량 조기청산(49434: 누적≥전일 130% + 음봉 → 잔량 청산, reason 'VOL'). 파라미터 `--scout-frac 0.2 / --wick-min 0.5 / --vol-exit 1.3`.
- 6월 50종목 ablation: v3 11건 -4.9% | v4 선발대OFF(=v3+VOL) 11건 -4.9%(동일) | v4 기본 26건 -7.9% | v4+과열컷+11:30 22건 승률40.9% -1.4% | **최선은 여전히 v3+과열컷+11:30 (5건 +2.6%)**.
- 판정: **선발대는 6월(허리붕괴 83% 적대장)에선 순비용** — 구조 실패일마다 -0.2~-1.2% 수수료. 승자 평단은 개선(SK하이닉스 +7.13→+7.49). VOL 청산은 6월 중립. 둘 다 opt-in으로 두고 4~5월 OOS 에서 판정.
- 개발 중 교훈 2개: ①쌍바닥은 "존 2회 터치"가 아니라 "반등 후 재시험"으로 카운트해야 함(연속봉 오판) ②선발대 손절을 본대 지지선(0.5%)에 걸면 노이즈에 전멸 — 허리 붕괴 기준으로 완화.

---

## 2026-07-03 세션 인수인계 (노트북 → PC 전환용)

### 오늘 한 일 요약 (커밋 2125646 ~ b26ea8e, 전부 push 완료)
1. **v3 신설**: mentor.db(스승님 42,947건) 마이닝 → "진바닥 확인 후 무릎 진입" 매수타점. 규칙별 article_id 근거는 v3 docstring.
2. **유니버스 확장 23→50종목 9섹터**: 스승님 6월 글 220건에서 추출·등록 (기판/광통신/조선엔진/원자력/AI솔루션 신설 + 반도체 8종 추가).
3. **v2 수정**: 죽은 로직이던 저점 지지를 실제 강제 + 다지기 3봉(`--consol-bars`, 0=구버전).
4. **v3 개선 필터 4종**(opt-in): `--pullback-frac`(비례눌림) `--max-surge`(과열컷) `--entry-until`(진입마감) `--leader-only`(섹터대장).
5. **v4 신설**: v3 + 선발대 2단 진입 + 쌍바닥/아래꼬리 바닥신호 + 거래량 130% 조기청산.

### 6월 50종목 스코어보드 (결론)
| 구성 | 건수 | 승률 | 누적 | MDD |
|---|---|---|---|---|
| v2(신) | 40 | 40.0% | -9.7% | -26.8% |
| v3 | 11 | 27.3% | -4.9% | -7.6% |
| **v3+과열컷12+진입~11:30 ← 현재 최선** | 5 | 40.0% | **+2.6%** | -4.2% |
| v4 기본 | 26 | 26.9% | -7.9% | -10.8% |

재현 명령: `./.venv/Scripts/python.exe backtest/run_premarket_pullback.py --mode v3 --start 2026-06-01 --end 2026-06-27 --max-surge 12 --entry-until 11:30`

### ⚠️ PC 에서 시작하기 전에 (git 으로 안 넘어가는 것들)
1. **git pull 먼저** (CLAUDE.md 프로토콜).
2. **유니버스 재등록**: db/trading.db 는 로컬 전용이라 PC 에는 50종목이 없다 →
   `./.venv/Scripts/python.exe scripts/register_mentor_june_picks.py` 1회 실행(멱등). 기존 23종목이 PC DB에 없다면 웹앱으로 먼저 등록 필요.
3. **토스 IP 화이트리스트**: PC 공인 IP 를 토스 개발자센터에 등록해야 함(`https://api.ipify.org` 로 확인). 오늘도 테더링 IP 회전으로 2번 재등록했음. 403 `IP address not allowed` = 이 문제.
4. **토스 분봉 캐시(db/toss_candles.db) 재수집**: PC 에는 캐시가 없어 첫 백테스트 때 50종목 자동 fetch(수 분). `.env` 에 TOSS_CLIENT_ID/SECRET 필요(.env.example 참고).
5. mentor.db 는 PC 의 `C:\projects\ai_moneyingbot_rag_agent\data\mentor.db` 경로 기준 — PC 에 해당 리포/DB 가 있는지 확인.

### 다음 작업 (우선순위)
1. **4~5월 out-of-sample 검증** ← 최우선. 오늘 만든 필터 전부 6월 in-sample 이라 여기서 판정해야 함.
   `--start 2026-04-01 --end 2026-05-30` 으로 v3 / v3+과열컷+11:30 / v4 를 A/B. 토스 1분봉 1년 보존이라 수집 가능.
2. OOS 통과 시: 최선 조합을 기본값으로 승격 + 파라미터 스윕(마름 0.6~1.0, 진입마감 10:30~13:00).
3. 선발대(v4)·VOL청산은 OOS 에서 추세장 성과 확인 후 채택/폐기.
4. 장기: 시뮬레이션 → 실주문 연결은 OOS 에서 일관된 플러스 확인 후에만.

---

## 2026-07-03 PC 세션: acc 모드 통합 (PC·노트북 양쪽 작업 병합)

### 배경
- pull 시점에 PC 로컬에 커밋 안 된 별도 v3(저점 분할매집)가 있어 노트북 v3(무릎 진입)와 충돌.
- 형 결정: **둘 다 살린다** → PC 구현을 `--mode acc` 로 개명해 통합 (v1/v2/v3/v4/acc 5모드 공존).

### acc 모드 (`evaluate_day_acc`) — 지지선 분할매집 지정가
- v2(재돌파 1회 매수)·v3(반등 확인 후 무릎 1회 매수)와 달리, **확인 전 지지선에
  지정가 여러 장**(`--entry-bands 1,0,-1` = 지지+1%/지지/지지-1%)을 깔아 평단을 낮춤.
- 다지기(`--consol-bars`) 확인 후 레벨 세팅 → 재돌파 시 매집 종료 → 분할 익절.
- `--trail 5` 트레일링 스탑(첫 익절 후 고점 -5%, 사유 'TR') — `_split_exit` 에
  `trail_pct` 파라미터로 흡수(기본 0 = v2/v3/v4 동작 불변).
- `--top-n N`: 그날 신호 중 프리장 급등률 상위 N종목만 채택(전 모드 공통, MDD 완화용).

### 독립 리뷰(서브에이전트)에서 잡은 버그 — PC 원본 코드의 백테스트 편향
- **HIGH 룩어헤드**: 매수 레벨을 현재 봉 저가로 계산해 같은 봉에서 체결 처리
  → 수익률 과대평가. 루프를 "지난 봉까지 세팅된 주문만 이번 봉에서 작동" 구조로 수정.
- **MEDIUM 2건**: 같은 봉 내 체결/손절 순서 — 체결 먼저(지정가는 손절선 위) →
  손절은 같은 봉 체결분 포함해 보수적으로 판정하도록 수정.
- 회귀 테스트 추가: 세팅 봉 체결 불가(룩어헤드 금지), 분할 평단 정확값, 트레일링 TR 경로.
- **주의: acc 6월 성과는 아직 안 돌림** — 버그 수정 전 수치는 무의미하므로 4~5월 OOS 와 함께 측정할 것.

### 남은 관찰 (리뷰 LOW, 설계 판단 대기)
- acc 는 프리미엄 돌파로 구조가 무효화돼도 레벨/눌림 구조를 리셋 안 함(v2 는 리셋).
  실제 지정가 운용이면 "안 판 주문은 남는다"로 볼 수도 있어 형 판단 필요.
- `entry_bands` 만 함수 경계에서 % 단위(내부 /100), 나머지 파라미터는 소수 비율 — CLI 경유는 일관.

### 검증
- `pytest`: **269 passed, 1 skipped** (acc 신규 7건 포함, 기존 262 회귀 없음).

### 유니버스 동기화 (같은 세션 후속)
- PC DB 실측: active 21종목 반도체 1섹터(6/25 웹 등록 pick 18) — 노트북 50종목과 불일치.
- `scripts/register_mentor_june_picks.py` PC 실행 완료 → 멘토 27종목(6섹터) 등록됨.
- **`scripts/sync_universe.py` 신설**: `--export` 로 active 유니버스를
  `universe_snapshot.json`(git 동기화)으로 내보내고 `--import` 로 멱등 반영.
  import 는 섹터 내 "다른 활성 픽" 보유 종목도 걸러 픽 간 중복(트래커 이중
  수집)을 방지. PC 에서 export→import 왕복 스모크 통과(전부 스킵 확인).
- **형이 직접 명단 제공 → MLCC/양자암호/전력설비 PC 등록 완료** (pick 25~27):
  전력설비 7(엘에스일렉트릭·대한전선·산일전기·제룡전기·HD현대일렉트릭·대원전선·가온전선)
  / MLCC 4(삼성전기·아모텍·삼화콘덴서공업·LG씨엔에스)
  / 양자암호 5(엑스게이트·아이씨티케이·케이씨에스·우리넷·코위버).
- **PC 최종: 9섹터 58종목 unique** (노트북 50 + PC 의 6/25 웹 반도체 픽 잔여분).
  반도체가 노트북보다 넓음 — 백테스트 결과를 노트북 수치와 직접 비교할 땐 주의.
- **노트북 TODO(선택)**: `--export` 1회 실행 → commit+push → PC 에서 `--import`
  하면 양쪽 유니버스가 정확히 수렴(노트북에만 있는 종목 반영).
- 알려진 잔재: PC pick 21(반도체)에 스모크 테스트로 pick 18 중복 16행이 들어감.
  유니버스(DISTINCT)에는 무영향, PC 는 적립 기기 아님 — 정리 여부는 형 판단.

---

## 2026-07-03 4~5월 Out-of-Sample 검증 결과 (PC, 58종목)

토스 IP/키 정상, 4~5월 + 6월 분봉 58종목 PC 캐시 완료. **유니버스 = PC 58종목**
(노트북 50 + 6/25 웹 반도체 잔여) — 노트북 6월 수치와 직접 비교 불가.

### 스코어보드 (동일 유니버스 58종목, 기본 파라미터)
| 구성 | 4~5월 OOS | 6월 | 판정 |
|---|---|---|---|
| **v2(신)** | 59건 승률47.5% **+64.6%** MDD-17.6% | 44건 47.7% **+7.6%** MDD-21.7% | **양 기간 유일 생존** |
| acc | 181건 51.9% +16.0% **MDD-61.6%** | 135건 50.4% +44.3% MDD-51.0% | 수익률 양수지만 MDD 파멸적 |
| v4+과열컷12+11:30 | 26건 42.3% -1.1% MDD-8.5% | 21건 42.9% -0.1% MDD-7.1% | 본전 근처 |
| v4 기본 | 38건 36.8% -4.3% MDD-13.5% | — | 마이너스 |
| v3+과열컷12+11:30 | 10건 40.0% **-4.8%** MDD-9.5% | 5건 40.0% +2.6% MDD-4.2% | **OOS 탈락** |
| v3 기본 | 23건 30.4% -10.6% MDD-17.3% | 11건 27.3% -3.9% MDD-10.3% | OOS 탈락 |

### 판정
1. **6월 "현재 최선"(v3+과열컷+11:30)은 in-sample 튜닝이었음이 확정** — OOS -4.8%.
   v3 계열(무릎 진입)은 양 기간 모두 손실/본전. 과열컷·11:30 필터 자체는 손실
   축소 방향성만 유효(v3 -10.6→-4.8, v4 -4.3→-1.1).
2. **v2(신: 다지기 강제 재돌파)가 유일하게 양 기간 플러스**. 4~5월 추세장에서
   특히 강함(+64.6%). 단 MDD ~-20%대.
3. **acc(분할매집)는 양 기간 수익이지만 MDD -50~-60%로 운용 불가 수준**.
   월 90건+ 폭발, 청산의 절반 이상이 재돌파 실패 EOD. top-n 선별/사이징 필수.
4. **경고(유니버스 민감도)**: 6월 v2 가 노트북 50종목 -9.7% ↔ PC 58종목 +7.6%.
   반도체 8종목 차이로 부호가 뒤집힘 = 아직 robust 한 엣지 아님.
5. **경고(비용 미반영)**: 백테스트에 수수료/증권거래세/슬리피지 없음.
   왕복 ~0.2%+ 가정 시 acc(평균 +0.13%)는 소멸, v2(+0.93%)는 생존 추정.

### 다음 작업 제안
1. 백테스트에 거래비용 모델 추가(왕복 수수료+세금 파라미터) 후 v2/acc 재검증.
2. v2 를 주 후보로: 유니버스 고정(스냅샷 커밋) + 월별 안정성(4/5/6월 분리) 확인.
3. acc 는 --top-n 1~3 선별 + 비용 반영 후 재평가. v3/v4 는 구조 재검토(보류).

### 후속 검증: v2 "선택과 집중"(과거 승자 공략) walk-forward — 기각
형 제안 "잘 맞는 종목 계속 공략" 검증 (4월 성과→5월 선택, 5월 성과→6월 선택):
- 지난달 상위 3종목만: +2.5% / 상위 5: -6.4% / 상위 10: +5.8% / 승자 전부: -2.1%
- **같은 기간(5~6월) 전체 58종목: +29.4%** → 집중이 전체 대비 크게 밀림.
- 원인: **월간 지속성 없음** — 후성 4월 +19.9%→5월 -1.3%, 심텍 +9.7%→-6.4%,
  한화엔진 +9.7%→-6.5%. 주도주가 매달 로테이션(4월 후성/반도체 → 5월 삼성전기
  → 6월 SK스퀘어). v2 엣지는 특정 종목이 아니라 "그날 조건 충족 종목의 폭"에서 나옴.
- 당일 프리장 강도 top-n(사전정보만): top-3 +13.2% MDD-17.8 vs 전체 +29.4%
  MDD-21.7 — 평균손익 개선 없이(+0.41 vs +0.42) 건수만 줄어 복리 손해.
  실전 자본 한정 시 top-2~3 은 타협안으로는 가능(MDD 소폭 개선).
- 월별 v2: 4월 +37.0%(승률 55%) / 5월 +20.2% / 6월 +7.6% — 석 달 연속 플러스이나
  체감 둔화 추세. 표본 74~103건, 비용 미반영 동일.

### 후속 검증 2: 일 단위 '최근 잘 맞는 놈 따라가기' (4~6월, 사전정보만)
| 선택 규칙 | 건수 | 승률 | 평균 | 누적 | MDD |
|---|---|---|---|---|---|
| [비교] 전체 신호 | 103 | 47.6% | +0.63% | +77.2% | -21.7% |
| A) 누적 성과 1위 | 31 | 51.6% | +0.35% | +9.0% | -24.5% |
| **B) 최근 5일 성과 1위** | 19 | 52.6% | **+1.24%** | +24.4% | **-10.0%** |
| C) 직전 승자 전부 | 34 | 50.0% | +0.52% | +16.7% | -16.8% |
| D) 직전 승자 중 강도 1위 | 18 | 61.1% | +0.70% | +12.3% | -14.3% |

- 총 복리는 폭(전체 103건)이 압승 — 집중은 건수 자체가 줄어 복리에서 짐.
- 단 **하루 1종목 제약이라면 B(최근 5일 1위)가 평균 2배·MDD 절반**으로 최선 후보.
  표본 19건이라 채택 결론 금지 — 기간 늘려 재검 필요.
- 지속성 검정: 직전 승 후 승률 50.0% vs 패 후 38.5% (승률에만 약한 지속성,
  평균수익 차이는 미미 +0.52 vs +0.44).
- **부수 발견: 종목의 '첫 v2 신호'가 최강 (30건, 승률 56.7%, 평균 +1.01%)**
  — 새로 신호 풀에 진입하는 종목(로테이션 초입)이 기존 종목보다 셈.
  "계속 가던 놈"보다 "새로 나타난 놈". 필터 후보로 유망.

### 후속 검증 3: 주도섹터 로테이션 필터 — ★ 현재까지 최고 성과
mentor.db 마이닝(28650 업종→대장 순서 / 149194 3~4일 순환매 / 160817 쉬는
섹터가 다음 주도 / 102729 추격 금지 / 58191 성숙국면 대장 집중)을 OHLCV 로
번역, 사전 정보만으로 walk-forward (신호일 d-1 까지 일봉으로 섹터 선정):

| 규칙 (v2 신호 필터) | 건수 | 승률 | 평균 | 누적 | MDD |
|---|---|---|---|---|---|
| [비교] 전체 신호 | 103 | 47.6% | +0.63% | +77.2% | -21.7% |
| **A) 5일 주도섹터(1위) 종목 전부** | 21 | **66.7%** | **+2.05%** | +50.0% | -12.3% |
| C) 3일 주도섹터 (149194) | 18 | 66.7% | +1.97% | +39.6% | -12.3% |
| E) 각 섹터 5일 대장들만 (28650) | 15 | 53.3% | +1.89% | +30.0% | -7.3% |
| D) 쉬는 주도섹터 (160817) | 13 | 46.2% | +1.19% | +15.1% | -8.5% |
| B) 주도섹터의 대장 1종목만 | 2 | — | — | — | 표본 무의미 |

- **A 월별: 4월 +8.0% / 5월 +32.2%(6건 전승, 삼성전기 연속) / 6월 +5.1% — 3개월 전부 플러스.**
- 주도섹터 로테이션 실측: 광통신(4/8)→양자암호→조선엔진→전력설비→MLCC(5월)
  →AI솔루션→반도체(6/10)→MLCC→… 1~2주 주기, 6월엔 며칠 단위로 가속
  — 149194 "3~4일 순환매" 증언과 일치.
- **대장 1종목(B)은 신호가 안 남(2건)**: 대장은 이미 달리는 중이라 눌림-재돌파
  세팅 자체가 드묾. 실전형은 "주도섹터를 대장이 확인 → 섹터 전체(대장+부대장)
  신호 공략" = A.
- 경고: ① 21건 표본(승률 95% CI ~45-84%) ② **유니버스 자체가 스승님 픽 =
  사후선택 편향**(4~6월에 돈 섹터들만 등록돼 있음) — 섹터 로테이션 필터는 이
  편향을 증폭할 수 있음. 진짜 검증은 forward ③ 비용 미반영(평균 +2%면 견딤).
- 다음: A 를 run_premarket_pullback.py 정식 옵션(--sector-momentum)으로 승격
  + 7월 forward 관찰 검토.

---

## 2026-07-04 strategy_gm_v3 — 멘토 매매원칙 룰 엔진 (신규 격리 모듈)

형 스펙 기반 정량 룰 엔진. **기존 v1~v4/acc·Phase 2.5 코드 무수정**(마이그레이션
러너 m010 등록 2줄만 — 스펙 제약 4항이 허용하는 경로). 실주문 코드 없음.

### 구조
- `strategy/gm_v3/`: config(전 임계값+플래그) / models / rules(R1~R12) /
  paper(체결 시뮬) / data_source(토스 일봉 합성+KIS 보충+합성 패딩) /
  signal_log / synth(더미 유틸)
- `backtest/run_gm_v3.py` 러너, `scripts/migrations/m010_gm_v3_signals.py`
- 테스트 31건 (`test_gm_v3_rules.py` 26 + `test_gm_v3_paper.py` 5)

### 확정 사양 (형 답변 2026-07-04)
① 모듈 위치 strategy/gm_v3/ ② 데이터 토스캐시 1순위 + KIS 보충(--kis-backfill),
부족 시 합성 패딩(--synth-pad, 리포트 명시) — PC 축적 테이블은 0행이라 불가
③ 스윙 고점 = 확정 프랙탈 피벗(k=3, 확정 지연으로 룩어헤드 차단, 소비 후 재발화
금지) ④ 체결 = 다음날 시가 기본(R10 손절만 당일 스탑 체결, --fill close 전환)

### 첫 백테스트 (4/1~6/27, 58종목, 기본 config, R2 off, 비용 미반영)
- 독립 리뷰(HIGH 1·MEDIUM 3·LOW 4) 반영 후 확정치:
  **67건 | 승률 37.3% | 평균실현 +0.57% | 누적(순차복리) +43.5% | MDD -9.4%**
  (리뷰 전 +59.1%/-12.2% 는 R10 갭하락 낙관 체결 등 편향 포함 — 폐기)
- 리뷰 반영: ① R10 갭하락 시 시가 체결(min(open,stop)) ② 평단 조화평균
  (자본비중 매수 회계) ③ synth 시드 crc32(PYTHONHASHSEED 비결정성 제거)
  ④ watch 신고가 갱신+구조 리셋 ⑤ R4 고점 위 추격 금지 ⑥ 동일봉 R1+R4
  이중 진입 방지 ⑦ run_id 필수화 ⑧ 누적/MDD 라벨 정직화(직렬 복리 참고치).
- 패턴: R10 선발대 손절(-0.8%대)이 다수, 승자는 R8→R7 체인 +2~4%.
  선발대 20% 구조 덕에 MDD 가 v2(-21.7%)보다 낮음. 데이터 갭 0종목.
- 시그널 gm_v3_signals 적재(run_id 멱등). 테스트 33건(전체 302 passed).

### 미결/다음
- R9b 는 일봉 근사(분봉 정밀화 TODO), R2 는 60일선 이력 필요(--kis-backfill 60).
- 거래비용 모델 전략 공통 미반영. R6 추가매수는 R1/R4 재신호 시만(물타기 금지).
- v2/주도섹터 필터와의 A/B 는 회계가 달라(투입비중 가중) 직접 비교 주의.

---

## 2026-07-04 1~3월 확장 검증 — 생존 3전략 반기 비교 (acc/v3/v4 폐기 확정)

형 결정: acc(MDD -50~60%)·v3·v4(OOS 마이너스) 폐기. 생존 3전략을 1~3월
신규 구간으로 재검증 (토스 1분봉 58종목 1~3월 수집 완료, 캐시 ~2025-12-28부터).

| 전략 | 1~3월 | 4~6월 |
|---|---|---|
| v2 | 55건 56.4% 평균+0.37% 누적+18.2% MDD-18.9% | 103건 47.6% +0.63% +77.2% -21.7% |
| **v2+주도섹터(5일 1위)** | 22건 **63.6%** **+0.95%** +21.1% **-7.7%** | 21건 66.7% +2.05% +50.0% -12.3% |
| **gm_v3(멘토 룰엔진)** | 75건 33.3% +0.42% +33.5% -10.7% | 67건 37.3% +0.57% +43.5% -9.4% |

### 판정
1. **3전략 모두 양 반기 플러스** — 6개월 일관성 확보.
2. **주도섹터 필터가 양 구간에서 v2 를 개선** (평균 2.5~3배, MDD 절반 이하,
   승률 +7~19%p). 대안 규칙들은 1~3월에서 전멸: B 대장만 -6.3% / C 3일 -3.2%
   / D 쉬는섹터 -16.8% / E 섹터대장들 -13.0% → **5일 주도섹터(A)만 생존 확정,
   나머지 변형 폐기.**
3. **gm_v3 가 가장 안정**: 평균 +0.42/+0.57, MDD -10.7/-9.4 — 구간 간 편차 최소.
4. 1~3월 내부: 1~2월은 본전권, **3월(반도체 랠리)이 수익 대부분** — Q1 도
   결국 추세 구간에서 벎. 횡보장 방어력은 여전히 미검증.
5. 유보 동일: 비용 미반영 / **유니버스 사후선택 편향(6월 픽을 1월로 소급)은
   Q1 에서 더 강함** / gm_v3 1월 초는 워밍업 부족(캐시 시작 12/28).

### 다음
- 거래비용 파라미터(왕복 수수료+세금) 공통 도입 → 3전략 재확정.
- 주도섹터 필터 --sector-momentum 정식 옵션 승격 + 7월 forward.
