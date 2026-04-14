"""리스크 게이트: 시간대, 일손실 한도, 동시보유 제한."""
from __future__ import annotations

from datetime import date, datetime, time

from config import settings
from config.constants import (
    FORCE_CLOSE_END,
    FORCE_CLOSE_START,
    MARKET_CLOSE,
    MARKET_OPEN,
    NO_TRADE_END,
    NO_TRADE_START,
    TradeWindow,
)


class RiskManager:
    def __init__(self) -> None:
        self.trading_halted: bool = False
        self.halt_reason: str = ""
        self.day: date = date.today()
        self.start_equity: float = float(settings.TOTAL_SEED)
        self.current_equity: float = float(settings.TOTAL_SEED)

    # --- 시간대 ---
    def classify_window(self, now: datetime | None = None) -> TradeWindow:
        t: time = (now or datetime.now()).time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return TradeWindow.CLOSED
        if NO_TRADE_START <= t < NO_TRADE_END:
            return TradeWindow.FORBIDDEN
        if FORCE_CLOSE_START <= t < FORCE_CLOSE_END:
            return TradeWindow.FORCE_CLOSE
        return TradeWindow.NORMAL

    # --- 일손실 ---
    def daily_pnl_ratio(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return (self.current_equity - self.start_equity) / self.start_equity * 100.0

    def update_equity(self, equity: float) -> None:
        self.current_equity = equity
        if self.daily_pnl_ratio() <= settings.DAILY_LOSS_LIMIT_PCT and not self.trading_halted:
            self.halt(f"일손실 한도 도달 ({self.daily_pnl_ratio():.2f}%)")

    def halt(self, reason: str) -> None:
        self.trading_halted = True
        self.halt_reason = reason

    def reset_daily(self, equity: float) -> None:
        self.day = date.today()
        self.start_equity = equity
        self.current_equity = equity
        self.trading_halted = False
        self.halt_reason = ""

    # --- 진입 가능 여부 ---
    def can_open_new(self, open_positions: int, now: datetime | None = None) -> tuple[bool, str]:
        if self.trading_halted:
            return False, f"거래중단: {self.halt_reason}"
        if open_positions >= settings.MAX_CONCURRENT_POSITIONS:
            return False, f"동시보유 한도({settings.MAX_CONCURRENT_POSITIONS}) 초과"
        w = self.classify_window(now)
        if w != TradeWindow.NORMAL:
            return False, f"시간대 제한: {w.value}"
        return True, "OK"
