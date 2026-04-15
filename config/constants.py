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

VOLUME_SURGE_MULT = 1.5
VOLUME_LOOKBACK = 20

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
