# 트레이딩봇 핸드오프 문서

> 다음 AI 세션이 이 프로젝트를 즉시 이해하고 이어받기 위한 기술 문서.
> 마지막 업데이트: 2026-04-16

---

## 1. 프로젝트 정체성

- **목적**: 한국 주식(KOSPI/KOSDAQ) 자동매매 봇. 3분봉 기준 단기 매매.
- **연동**:
  - 시세/주문: 한국투자증권 KIS Open API (REST + WebSocket)
  - 알림/제어: Telegram Bot (`@zzapmoneying_bot`)
  - 백테스트 데이터: tvDatafeed (TradingView), yfinance, KIS
- **상태**: 백테스트 인프라 완성, 전략 v4 실전 권장, 실시간 운영 코드는 작성됨/실전 검증 안 됨
- **운영자**: 재승 (행신파이낸셜) — 자세한 사용자 프로필은 `메모리 user_profile.md`

---

## 2. 디렉토리 구조

```
C:/trading-bot/
├── main.py                     # 실시간 봇 엔트리 (검증 부족)
├── requirements.txt
├── README.md
├── test_connection.py          # KIS 연결 테스트
├── test_telegram.py            # Telegram 토큰 확인
├── test_telegram_commands.py
│
├── config/
│   ├── settings.py             # pydantic-settings, .env 로드
│   └── constants.py            # 모든 전략/시간/게이트 상수
│
├── core/
│   ├── kis_api.py              # KIS REST 래퍼 (토큰 파일캐시, 백오프 재시도)
│   └── telegram_bot.py
│
├── data/
│   ├── models.py               # Candle, Signal, Position, Trade
│   ├── candle_store.py         # CandleBuffer + SQLite (aiosqlite)
│   ├── flow_data.py            # KIS 외국인/기관 수급 (백만원 단위)
│   ├── daily_data.py           # 일봉 MA20/MA60 정배열 체크
│   └── stock_master.py         # KRX 종목명↔코드 매핑 (캐시)
│
├── strategy/
│   ├── indicators.py           # vwap, ema, macd, atr_wilder, rsi 등
│   └── signal.py               # ★ 듀얼 채널: PULLBACK + BREAKOUT 디스패처
│
├── risk/
│   └── risk_manager.py         # 일일손실 한도, 동시 보유 제한
│
├── agents/
│   ├── analysis_agent.py       # (확장 영역)
│   └── execution_agent.py
│
├── backtest/
│   ├── engine.py               # ★ BacktestEngine (게이트, 듀얼시그널 토글)
│   ├── report.py               # CSV/요약 출력
│   ├── collect_tv.py           # ★ tvDatafeed 3분봉 수집 (무로그인 5000봉 max)
│   ├── collect_yf.py           # yfinance 수집 (1분봉→3분봉 리샘플)
│   ├── collect_data.py         # KIS 분봉 수집 (당일만, 페이지네이션)
│   ├── run_full.py             # KIS 수집 + 백테스트 + 리포트 (구버전)
│   ├── run_backtest.py         # 단순 백테스트 러너
│   ├── run_v3.py               # v3 러너 (gate_check 함수 위치)
│   ├── run_v4.py               # ★ v4 러너 (게이트 + v1 시그널)
│   ├── run_v5.py               # ★ v5 러너 (게이트 + 듀얼 시그널)
│   ├── run_batch.py            # 배치 (v1 vs v4)
│   ├── run_batch_compare.py    # 배치 (v4 임계값 2개 비교)
│   └── run_batch_v5.py         # ★ 배치 (v1/v4/v5 3종 비교)
│
├── db/
│   ├── trading.db              # 캔들 SQLite
│   ├── stock_master.json       # KRX 마스터 캐시
│   └── kis_token.json          # KIS 액세스 토큰 캐시 (1분 쿨다운 회피)
│
└── tests/                      # pytest
```

---

## 3. 데이터 모델 (`data/models.py`)

```python
@dataclass
class Candle:
    code: str; ts: datetime
    open: float; high: float; low: float; close: float; volume: int

@dataclass
class Signal:
    code: str; type: SignalType  # BUY/SELL/HOLD
    price: float; ts: datetime
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    # meta 에는 atr, vwap3, ema9, hist, score, kind('PULLBACK'|'BREAKOUT') 등

@dataclass
class Position:
    code: str; entry_price: float; qty: int; opened_at: datetime
    realized_pnl: float = 0.0
    tp_hit: set[int] = field(default_factory=set)
    atr: float = 0.0
    stop_price: float = 0.0
    tp_prices: list[float] = field(default_factory=list)
    trailing_activated: bool = False

@dataclass
class Trade:
    code: str; side: str  # BUY/SELL
    price: float; qty: int; ts: datetime
    reason: str = ""        # v5: "[PULLBACK] ..." 또는 "[BREAKOUT] ..." 프리픽스
    pnl: float = 0.0
    exit_reason: ExitReason | None = None
    atr: float = 0.0; stop_price: float = 0.0; tp_prices: tuple = ()
```

`ExitReason` enum: `TAKE_PROFIT, STOP_LOSS, TRAIL_STOP, VWAP_BREAK, MACD_FLIP, FORCE_CLOSE, DAILY_HALT, MANUAL`

---

## 4. 핵심 상수 (`config/constants.py`) — 전략 튜닝 다 여기

### 시간대
- `MARKET_OPEN/CLOSE` = 9:00 / 15:30
- `NO_TRADE_START/END` = 9:00~9:30 (변동성 매매 금지)
- `NO_NEW_BUY_AFTER` = 14:30 (이후 신규매수 금지)
- `FORCE_CLOSE_START/END` = 15:10~15:20 (장마감 강제청산)

### 분봉
- `CANDLE_INTERVAL_SEC` = 180 (3분봉)
- `HTF_MULTIPLIER` = 5 (3분×5 = 15분봉)

### ATR 손익절 (PULLBACK/BREAKOUT 공유)
- `ATR_PERIOD` = 14
- `ATR_STOP_MULT` = 2.0
- `ATR_TP_MULTS` = (1.5, 2.5, 4.0)
- `ATR_TP_RATIOS` = (0.40, 0.40, 1.00)
- `ATR_TRAILING_TRIGGER` = 1.0 (1ATR 도달 시 본절)
- `TP_STOP_BUFFER_ATR` = 0.5

### MACD/EMA
- `MACD_FAST/SLOW/SIGNAL` = 12/26/9
- `EMA_SHORT/MID/LONG` = 9/20/60
- `VOLUME_SURGE_MULT` = 1.5, `VOLUME_LOOKBACK` = 20 (PULLBACK 거래량)

### 게이트 (v4)
- `DAILY_MA_SHORT/LONG` = 20/60 (일봉 MA20>MA60 정배열)
- `FLOW_LOOKBACK_DAYS` = 5
- `FLOW_THRESHOLD_MWON` = 500 (5억원, 백만원 단위)

### 추가
- `VWAP_TOUCH_TOLERANCE` = 0.002 (0.2%)
- BREAKOUT (v5):
  - `BREAKOUT_VOLUME_MULT` = 3.0
  - `BREAKOUT_VOL_LOOKBACK_BARS` = 650 (~5거래일)
  - `BREAKOUT_HIGH_LOOKBACK` = 60 (3시간)

### 리스크
- `MAX_POSITION_PCT` = 0.30 (종목당 시드의 30%)
- `RISK_PER_TRADE_PCT` = 0.01 (1회 매매 리스크 = 시드의 1%)
- `DAILY_SOFT_HALT_PCT` = -3.0 / `DAILY_HARD_HALT_PCT` = -5.0

---

## 5. 시그널 (`strategy/signal.py`) — 현재 상태: 듀얼 채널

```python
def evaluate_buy(code, buf, ts, allow_breakout: bool = False) -> Signal | None:
    sig = _evaluate_pullback(code, buf, ts)
    if sig:
        sig.meta["kind"] = "PULLBACK"
        return sig
    if allow_breakout:
        sig = _evaluate_breakout(code, buf, ts)
        if sig:
            sig.meta["kind"] = "BREAKOUT"
            return sig
    return None
```

### `_evaluate_pullback` (v1, 모든 버전 공통)
1. 시간대 체크 (NO_TRADE 영역 제외)
2. 데이터 부족 체크
3. **3분봉 3조건 모두 충족**:
   - VWAP 지지반등: `lows[-1] ≤ vwap3*1.002 AND closes[-1] ≥ vwap3 AND closes[-1] > opens[-1]`
   - 거래량 급증: `vols[-1] ≥ avg(vols[-21:-1]) × 1.5`
   - MACD 양전환: 최근 5봉 내 `hist 음→양` 발생 + 현재 `hist > 0`
4. **15분 TF 필터**: `close > VWAP15` AND `MACD15 hist > 0`
5. 가점: `close > EMA9` 면 `score += 1`

### `_evaluate_breakout` (v5 추가)
1. 시간대 체크
2. 데이터 부족 체크 (60봉+ 필요)
3. **3조건 모두 충족**:
   - 거래량 폭발: `vols[-1] ≥ mean(vols[-651:-1]) × 3.0`  (per-bar 평균 기준)
   - 신고가 돌파: `highs[-1] > max(highs[-61:-1])`
   - 양봉 마감: `closes[-1] > opens[-1]`
4. 15분 필터 **없음** (속도 우선)

⚠️ **현실 검증**: BREAKOUT 채널은 코스닥 3분봉에선 너무 자주 발화 (21종목 247건). 손익비 악화. **현재 v4 권장, v5는 보류.**

---

## 6. 백테스트 엔진 (`backtest/engine.py`)

```python
@dataclass
class BacktestConfig:
    seed: int = 10_000_000              # 시드 1천만원
    max_concurrent: int = 3              # 동시 보유 최대 3
    fee_rate: float = 0.00015            # 수수료
    tax_rate: float = 0.0018             # 매도세
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.30
    eligible_codes: set[str] | None = None  # 게이트 통과 종목 집합 (None=무게이트)
    allow_breakout: bool = False             # BREAKOUT 채널 토글
```

### 동작 흐름 (`BacktestEngine.run`)
1. 모든 종목 캔들을 시간순 단일 이벤트 큐로 정렬
2. 각 봉에 대해:
   - 포지션 있으면 손절/분할익절/트레일링 본절 처리
   - VWAP_BREAK / MACD_FLIP 청산 체크 (`_check_exit_signal`)
   - 포지션 없고 `eligible_codes` 통과 시 `evaluate_buy(allow_breakout=cfg.allow_breakout)`
   - 진입 시 ATR 기반 사이즈 계산 (`risk_per_trade ÷ atr×stop_mult`)
   - Trade.reason 에 `[PULLBACK]` / `[BREAKOUT]` 프리픽스
3. equity_curve 기록, 최종 수익률/MDD/승률 산출

### 청산 시그널 (`_check_exit_signal`)
- VWAP_BREAK: 최근 2봉 close < VWAP3 + 거래량 급증
- MACD_FLIP: 직전 hist > 0 ≥ 현재 hist (PULLBACK·BREAKOUT 공통)

⚠️ MACD_FLIP 청산은 PULLBACK 진입과 짝맞음. BREAKOUT 진입에선 부자연스러울 수 있음 — 다음 세션에서 검토 가능.

---

## 7. 데이터 수집 옵션

### A. tvDatafeed (`backtest/collect_tv.py`) — 추천
```bash
python -m backtest.collect_tv 100790 100790 5000
# code, symbol, n_bars
```
- **무로그인 한도: 약 5,000봉 (~40 거래일)**
- 로그인 시 더 많은 데이터 가능 (`username`/`password` 인자 추가)
- TradingView 심볼: `KRX:028300` 형식 (코스피·코스닥 모두 `KRX`)

### B. KIS (`backtest/collect_data.py`) — 당일만
- KIS는 과거 분봉 공식 미지원 → "오늘" 분봉을 시각 페이지네이션
- HHMMSS 역방향 호출, 한 번에 30봉 → 하루치 ~130봉 (3분봉)

### C. yfinance (`backtest/collect_yf.py`) — 백업
- 1분봉 7일 한도 → 3분봉 리샘플
- 티커: `028300.KQ` (코스닥) / `005930.KS` (코스피)

모든 수집기는 `CandleStore` 통해 동일한 SQLite 테이블에 저장:
```sql
candles(code TEXT, ts TEXT, open, high, low, close, volume, PRIMARY KEY(code, ts))
```

---

## 8. 게이트 (v4 핵심)

### `data/daily_data.py::daily_ma_passed`
- `inquire-daily-itemchartprice` (TR `FHKST03010100`) 로 70일치 일봉
- `MA20 > MA60` 이면 PASS

### `data/flow_data.py::flow_passed`
- `inquire-investor` (TR `FHKST01010900`) 로 30영업일 외국인/기관 매매동향
- 거래대금 단위 = **백만원**
- 5일 누적 `max(외국인, 기관) >= 500` (기본 5억) 이면 PASS
- 임계값 변경 가능: `flow_passed(kis, code, threshold_mwon=...)`

### `backtest/run_v3.py::gate_check` — 통합 게이트
```python
async def gate_check(kis, code, flow_threshold_mwon=None) -> tuple[bool, dict]:
    ma_ok, ma_s, ma_l = await daily_ma_passed(kis, code)
    fl_ok, f_sum, i_sum = await flow_passed(kis, code, threshold_mwon=...)
    return (ma_ok AND fl_ok), info
```

→ run_v4, run_v5, run_batch 모두 이 함수 사용.

---

## 9. KIS API (`core/kis_api.py`) — 주의사항

### 인증
- 토큰 발급은 **1분 쿨다운** (PAPER 환경 더 엄격)
- 파일 캐시 자동: `db/kis_token.json` (KIS_ENV 별로 분리)
- 캐시된 토큰은 만료 60초 전까지 재사용

### 안정성
- `get_investor_trend`, `get_daily_candles` 둘 다 PAPER 에서 간헐 500
- 내부적으로 500ms × 4회 백오프 재시도 자동 적용

### 환경
- `.env` 의 `KIS_ENV=PAPER` 또는 `REAL`
- PAPER URL: `openapivts.koreainvestment.com:29443`
- 주문 TR: BUY=`VTTC0802U`(PAPER)/`TTTC0802U`(REAL), SELL=`VTTC0801U`/`TTTC0801U`
- 잔고 TR: `VTTC8434R`/`TTTC8434R`

---

## 10. 러너 스크립트 — 어느 걸 써야 하나

| 스크립트 | 용도 | 권장 사용처 |
|---|---|---|
| `run_v4.py` | 단일/소수 종목 v4 백테스트 + ASCII 곡선 | 빠른 실험 |
| `run_v5.py` | 동상 v5 (듀얼시그널) | BREAKOUT 검증용 |
| `run_batch.py` | N종목 v1 vs v4 표 + CSV | 게이트 효과 검증 |
| `run_batch_compare.py` | N종목 × v1, v4@th_a, v4@th_b | 임계값 sweep |
| `run_batch_v5.py` | **N종목 v1 vs v4 vs v5 표** | **메인 비교** |

CSV 결과는 모두 `backtest/results/*.csv` 에 저장.

### 사용 예
```bash
# 게이트만 보고 싶다 (1종목, 빠름)
python -m backtest.run_v4 100790

# 21종목 정식 비교
python -m backtest.run_batch_v5 010170,062970,...

# 임계값 튜닝
python -m backtest.run_batch_compare 100790,440110 500,300
```

---

## 11. 전략 진화 히스토리 — 무엇을 시도했고 결과는

| 버전 | 정의 | 결과 (21종목 평균) | 채택? |
|---|---|---|---|
| **v1** | PULLBACK only (VWAP반등+거래량+MACD+15m필터) | 수익률 -0.22%, MDD -1.20% | 베이스라인 |
| v2 | v1 + 추세모드 (15m MACD↑ + EMA정배열 시 ATR 확장) | 100790에서 v1 대비 악화 | ❌ 폐기 |
| v3 | MACD 제거, VWAP터치+EMA9+거래량 + 게이트 | 3종목 모두 악화 (-3.59%) | ❌ 폐기 |
| **v4** | **v1 시그널 + 일봉MA20>60 + 5일 수급≥5억 게이트** | **수익률 +0.21%, MDD -0.41%** | ✅ **현 권장** |
| v5 | v4 + BREAKOUT 채널 (60봉 신고가+거래량×3+양봉) | 수익률 -0.54%, MDD -1.64% | ❌ 보류 |

### v4 가 작동한 이유
- 9개 게이트 FAIL 종목에서 v1이 합계 -9.16% 손실, v4 가 모두 회피
- false reject는 1종목 (062970 한국첨단소재 +0.76% 놓침)

### v5 가 망한 이유
- BREAKOUT 247건 (PULLBACK 66건의 4배) — 너무 자주 발화
- 코스닥 소형주는 신고가 직후 단기 반락 잦음 → ATR×2 손절로 못 버팀
- 한선엔지니어링 3/13 09:33 BREAKOUT → 6분 만에 -109,913원 손절 (대표 사례)

### 시도하지 않은 것 / 다음 후보
- BREAKOUT 강화: 개장 30분 제외, 15분 EMA 추세 확인, 신고가 윈도우 확대
- BREAKOUT 사이즈 축소 (`risk_per_trade × 0.5`)
- 게이트 임계값 sweep (300/500은 차이 없음 — 21종목 풀이 양극화)
- 더 큰 종목 풀(50~100개)에서 게이트 통계적 유의성 검증
- MA 조건 강화 (5>20>60 또는 종가>MA20)
- 수급 연속성 조건 (5일 중 3일 이상 순매수)
- 거래대금 필터 (일평균 100억 이상)

---

## 12. 검증 데이터 한계

- **백테스트 기간**: 2026-02-19 ~ 2026-04-16 (약 40거래일)
- 표본 작음 → 통계적 유의성 약함
- 단일 시장 국면 (관측 기간 동안 특정 추세) → 다양한 국면 미검증
- 추가 검증 권장: 더 긴 기간(tvDatafeed 로그인 필요) + 더 많은 종목

---

## 13. 실시간 운영 (검증 부족 영역)

`main.py`, `agents/execution_agent.py`, `core/telegram_bot.py` 등은 코드만 있고 **실전 라이브 운영 미검증**. 백테스트 결과(v4)를 실시간으로 옮기려면:

1. KIS WebSocket 연결로 실시간 분봉 수신
2. 매봉 마감 시 `evaluate_buy(allow_breakout=False)` 호출
3. 매일 장 시작 전 watchlist 갱신 (`gate_check` 일괄 호출)
4. Telegram 알림/명령 (현재 매매 상태, 강제 청산 등)
5. 일일 손실 한도(`risk_manager.py`) 적용

---

## 14. 환경

- OS: Windows 11, Python 3.14
- 주요 의존성: `httpx, websockets, aiosqlite, pandas, numpy, pydantic-settings, loguru, python-telegram-bot, pytz, tvDatafeed, yfinance`
- KIS 계정: PAPER (모의투자)
- Git 브랜치: main, 원격 `Metrokid25/trading-bot`

---

## 15. "다시 시작" 시 추천 진입점

1. 이 문서를 읽고 전체 그림 잡기
2. `backtest/run_batch_v5.py` 21종목 한 번 돌려서 현 상태 재현
3. 결정해야 할 것:
   - v4를 실전으로 옮길지 (실시간 운영 인프라 검증)
   - 더 다양한 종목/기간으로 v4 검증을 확대할지
   - BREAKOUT 채널 개선(v5.1)을 시도할지
   - 다른 시그널 채널(돌파말고 추세추종 등)을 추가할지

---

## 16. 알려진 이슈 / 주의사항

- ⚠️ tvDatafeed 무로그인 5000봉 한도 — 로그인 계정 추가 시 늘어남
- ⚠️ KIS PAPER 토큰 1분 쿨다운 (파일 캐시로 우회됨)
- ⚠️ KIS PAPER 일부 시세 TR 간헐 500 (백오프 재시도 적용됨)
- ⚠️ Windows 콘솔 한글 깨짐 → `PYTHONIOENCODING=utf-8` 권장
- ⚠️ BREAKOUT 채널의 MACD_FLIP 청산이 부자연스러움 (TODO)
- ⚠️ `Position.realized_pnl` 분할익절 합산 vs 청산시 PNL 계산 — 더블카운트 가능성 점검 필요
- ⚠️ 일일 손실 한도 (`risk_manager.py`) 백테스트 엔진엔 미통합
