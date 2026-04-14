"""도메인 dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config.constants import ExitReason, SignalType


@dataclass
class WatchItem:
    code: str
    name: str = ""
    weight: float = 0.0  # 시드 대비 비중 (0~1)
    enabled: bool = True


@dataclass
class Candle:
    code: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Signal:
    code: str
    type: SignalType
    price: float
    ts: datetime
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    code: str
    entry_price: float
    qty: int
    opened_at: datetime
    realized_pnl: float = 0.0
    tp_hit: set[int] = field(default_factory=set)  # 청산한 TP 레벨 인덱스

    def pnl_ratio(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price

    def market_value(self, price: float) -> float:
        return price * self.qty


@dataclass
class Trade:
    code: str
    side: str  # BUY / SELL
    price: float
    qty: int
    ts: datetime
    reason: str = ""
    pnl: float = 0.0
    exit_reason: ExitReason | None = None
