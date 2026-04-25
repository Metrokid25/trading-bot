# 트레이딩봇 핸드오프 문서

> 다음 AI 세션이 이 프로젝트를 즉시 이해하고 이어받기 위한 기술 문서.
> 마지막 업데이트: 2026-04-25
> 기준 커밋: `b5eafe0 fix(alerts): persist alert before telegram delivery`

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
