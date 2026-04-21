"""매매 시간대 및 전략 상수."""
from __future__ import annotations

from datetime import time
from enum import Enum

KST = "Asia/Seoul"

# --- 장 시간 ---
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# --- 매매 금지 / 특수 구간 ---
NO_TRADE_START = time(9, 0)       # 9:00 ~ 9:30 변동성 구간 매매 금지
NO_TRADE_END = time(9, 30)

NO_NEW_BUY_AFTER = time(14, 30)   # 14:30 이후 신규 매수 금지

FORCE_CLOSE_START = time(15, 10)  # 15:10 ~ 15:20 장마감 강제 청산
FORCE_CLOSE_END = time(15, 20)

# --- 분봉 주기 ---
CANDLE_INTERVAL_SEC = 180  # 3분봉
HTF_MULTIPLIER = 5         # 3분봉 5개 = 15분봉

# --- 지표 파라미터 (신규 전략) ---
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
ATR_TP_MULTS = (1.5, 2.5, 4.0)
ATR_TP_RATIOS = (0.40, 0.40, 1.00)    # 3차는 잔여 전량
ATR_TRAILING_TRIGGER = 1.0            # +1 ATR 도달 시 본절 이동
TP_STOP_BUFFER_ATR = 0.5              # 익절 후 손절선 = 직전 익절가 - 0.5 ATR

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

EMA_SHORT = 9
EMA_MID = 20
EMA_LONG = 60

# --- v3 전략: 일봉 필터 + 수급 필터 ---
DAILY_MA_SHORT = 20
DAILY_MA_LONG = 60
FLOW_LOOKBACK_DAYS = 5
FLOW_THRESHOLD_MWON = 500              # 5일 누적 순매수 ≥ 500백만원(=5억) 이면 PASS

# VWAP 터치 허용 오차 (close가 VWAP 아래로 얼마까지 찍었다가 반등했는가)
VWAP_TOUCH_TOLERANCE = 0.002           # 0.2%

# --- v5: BREAKOUT 시그널 채널 ---
BREAKOUT_VOLUME_MULT = 3.0             # 5일 평균(per-bar) 대비 현재 봉 거래량 배수
BREAKOUT_VOL_LOOKBACK_BARS = 650       # 약 5거래일 (코스닥 3분봉 ~130/일 × 5)
BREAKOUT_HIGH_LOOKBACK = 60            # 60봉(3시간) 신고가 돌파

VOLUME_SURGE_MULT = 1.5
VOLUME_LOOKBACK = 20

# --- Phase 2: 섹터 쏠림 감지 (Stage 1 = 조건 A + B) ---
# 조건 A: 개별 종목 필터 (거래량/상승률/양봉)
SECTOR_A_VOL_MULT_DEFAULT = 3.0        # 정규 구간: 최근 VOLUME_LOOKBACK분 평균 × 3배
SECTOR_A_VOL_MULT_EARLY = 4.0          # 09:00~09:30 장 초반 강화
SECTOR_A_VOL_MULT_LATE = 3.5           # 14:30 이후 강화
SECTOR_A_RETURN_DEFAULT = 0.02         # 당일 시가 대비 +2%
SECTOR_A_RETURN_EARLY = 0.03           # 장 초반 +3%

# 조건 B: 같은 섹터 내 A 통과 종목 수 임계
SECTOR_B_MIN_PASSED = 3

# 시간대 경계 (EARLY < SECTOR_EARLY_END, LATE ≥ SECTOR_LATE_START)
SECTOR_EARLY_END = time(9, 30)
SECTOR_LATE_START = time(14, 30)
# 동시호가 구간 - 신호 발생 차단
SECTOR_BLOCK_START = time(15, 20)
SECTOR_BLOCK_END = time(15, 30)

# 동일 (pick, sector) 중복 알림 쿨다운 (분)
SECTOR_ALERT_COOLDOWN_MIN = 5

# --- 레거시 지표 (테스트/백테스트 호환) ---
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30.0
BB_PERIOD = 20
BB_STD = 2.0
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60

# --- 리스크 ---
MAX_POSITION_PCT = 0.30         # 종목당 투입 상한 (시드 대비)
RISK_PER_TRADE_PCT = 0.01       # 1회 매매 리스크 (시드 대비)
DAILY_SOFT_HALT_PCT = -3.0      # 신규매수 중단
DAILY_HARD_HALT_PCT = -5.0      # 전 포지션 청산 + 당일 매매 중단


class TradeWindow(str, Enum):
    CLOSED = "CLOSED"
    FORBIDDEN = "FORBIDDEN"       # 9:00~9:15
    NORMAL = "NORMAL"
    NO_NEW_BUY = "NO_NEW_BUY"     # 14:30~15:10
    FORCE_CLOSE = "FORCE_CLOSE"   # 15:10~15:20


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAIL_STOP = "TRAIL_STOP"
    VWAP_BREAK = "VWAP_BREAK"
    MACD_FLIP = "MACD_FLIP"
    FORCE_CLOSE = "FORCE_CLOSE"
    DAILY_HALT = "DAILY_HALT"
    MANUAL = "MANUAL"
