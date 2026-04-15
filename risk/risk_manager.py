"""리스크 게이트: 시간대 / 일손실(-3% soft, -5% hard) / 동시보유 / 포지션 사이징."""
from __future__ import annotations

from datetime import date, datetime, time

from config import settings
from config.constants import (
    ATR_STOP_MULT,
    DAILY_HARD_HALT_PCT,
    DAILY_SOFT_HALT_PCT,
    FORCE_CLOSE_END,
    FORCE_CLOSE_START,
    MARKET_CLOSE,
    MARKET_OPEN,
    MAX_POSITION_PCT,
    NO_NEW_BUY_AFTER,
    NO_TRADE_END,
    NO_TRADE_START,
    RISK_PER_TRADE_PCT,
    TradeWindow,
)


class RiskManager:
    def __init__(self) -> None:
        self.hard_halt: bool = False
        self.soft_halt: bool = False
        self.halt_reason: str = ""
        self.day: date = date.today()
        self.start_equity: float = float(settings.TOTAL_SEED)
        self.current_equity: float = float(settings.TOTAL_SEED)

    # 기존 호출부 호환용 alias
    @property
    def trading_halted(self) -> bool:
        return self.hard_halt

    # --- 시간대 ---
    def classify_window(self, now: datetime | None = None) -> TradeWindow:
        t: time = (now or datetime.now()).time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return TradeWindow.CLOSED
        if NO_TRADE_START <= t < NO_TRADE_END:
            return TradeWindow.FORBIDDEN
        if FORCE_CLOSE_START <= t < FORCE_CLOSE_END:
            return TradeWindow.FORCE_CLOSE
        if NO_NEW_BUY_AFTER <= t < FORCE_CLOSE_START:
            return TradeWindow.NO_NEW_BUY
        return TradeWindow.NORMAL

    # --- 일손실 ---
    def daily_pnl_ratio(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return (self.current_equity - self.start_equity) / self.start_equity * 100.0

    def update_equity(self, equity: float) -> None:
        self.current_equity = equity
        pnl = self.daily_pnl_ratio()
        if pnl <= DAILY_HARD_HALT_PCT and not self.hard_halt:
            self.hard_halt = True
            self.soft_halt = True
            self.halt_reason = f"일손실 HARD halt ({pnl:.2f}%)"
        elif pnl <= DAILY_SOFT_HALT_PCT and not self.soft_halt:
            self.soft_halt = True
            self.halt_reason = f"일손실 SOFT halt ({pnl:.2f}%) — 신규매수 중단"

    def halt(self, reason: str) -> None:
        self.hard_halt = True
        self.soft_halt = True
        self.halt_reason = reason

    def reset_daily(self, equity: float) -> None:
        self.day = date.today()
        self.start_equity = equity
        self.current_equity = equity
        self.hard_halt = False
        self.soft_halt = False
        self.halt_reason = ""

    # --- 진입 가능 여부 ---
    def can_open_new(self, open_positions: int, now: datetime | None = None) -> tuple[bool, str]:
        if self.hard_halt:
            return False, f"HARD halt: {self.halt_reason}"
        if self.soft_halt:
            return False, f"SOFT halt: {self.halt_reason}"
        if open_positions >= settings.MAX_CONCURRENT_POSITIONS:
            return False, f"동시보유 한도({settings.MAX_CONCURRENT_POSITIONS}) 초과"
        w = self.classify_window(now)
        if w != TradeWindow.NORMAL:
            return False, f"시간대 제한: {w.value}"
        return True, "OK"

    # --- 포지션 사이징 ---
    def position_size(self, entry_price: float, atr_val: float) -> int:
        """ATR 손절폭 기준 리스크 1% / 종목당 30% 상한에 맞춰 수량 산정."""
        if entry_price <= 0 or atr_val <= 0 or self.start_equity <= 0:
            return 0
        seed = float(settings.TOTAL_SEED)
        risk_krw = seed * RISK_PER_TRADE_PCT
        stop_dist = atr_val * ATR_STOP_MULT
        qty_risk = int(risk_krw // stop_dist) if stop_dist > 0 else 0
        qty_cap = int((seed * MAX_POSITION_PCT) // entry_price)
        return max(0, min(qty_risk, qty_cap))
