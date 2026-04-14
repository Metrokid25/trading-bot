"""매매 시간대 및 전략 상수."""
from __future__ import annotations

from datetime import time
from enum import Enum

KST = "Asia/Seoul"

# --- 장 시간 ---
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# --- 매매 금지 / 특수 구간 ---
NO_TRADE_START = time(9, 0)      # 9:00 ~ 9:10 변동성 구간 매매 금지
NO_TRADE_END = time(9, 10)

FORCE_CLOSE_START = time(15, 10)  # 15:10 ~ 15:20 장마감 비중 강제 조정
FORCE_CLOSE_END = time(15, 20)

# --- 분봉 주기 ---
CANDLE_INTERVAL_SEC = 180  # 3분봉

# --- 지표 파라미터 ---
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30.0

BB_PERIOD = 20
BB_STD = 2.0

MA_SHORT = 5
MA_MID = 20
MA_LONG = 60

# --- 분할 익절 (수익률, 청산비중) ---
TAKE_PROFIT_LEVELS = [
    (0.03, 0.30),
    (0.05, 0.30),
    (0.10, 0.40),
]


class TradeWindow(str, Enum):
    FORBIDDEN = "FORBIDDEN"     # 매매 금지 구간
    NORMAL = "NORMAL"
    FORCE_CLOSE = "FORCE_CLOSE" # 장마감 정리 구간
    CLOSED = "CLOSED"           # 장 외


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    FORCE_CLOSE = "FORCE_CLOSE"
    DAILY_HALT = "DAILY_HALT"
    MANUAL = "MANUAL"
