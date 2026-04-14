"""ExecutionAgent — 시그널을 즉시 체결하고 TP/SL/강제청산을 관리."""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime
from pathlib import Path

from loguru import logger

from agents.base_agent import BaseAgent, E, Event, EventBus
from agents.portfolio_agent import PortfolioAgent
from config import settings
from config.constants import TAKE_PROFIT_LEVELS, ExitReason, TradeWindow
from core.kis_api import KISClient
from core.telegram_bot import TelegramBot
from data.models import Position, Signal, Trade
from risk.risk_manager import RiskManager


class ExecutionAgent(BaseAgent):
    def __init__(
        self,
        bus: EventBus,
        kis: KISClient,
        tg: TelegramBot,
        risk: RiskManager,
        portfolio: PortfolioAgent,
    ) -> None:
        super().__init__("ExecutionAgent", bus)
        self.kis = kis
        self.tg = tg
        self.risk = risk
        self.portfolio = portfolio
        self.positions: dict[str, Position] = {}
        self._trade_log = Path(settings.LOG_DIR) / f"trades_{datetime.now():%Y%m%d}.csv"
        self._last_prices: dict[str, float] = {}

        bus.subscribe(E.BUY_SIGNAL, self._on_signal)
        bus.subscribe(E.TICK, self._on_tick)

    # ----- 시그널 처리 -----
    async def _on_signal(self, evt: Event) -> None:
        sig: Signal = evt.payload
        ok, reason = self.risk.can_open_new(len(self.positions))
        if not ok:
            logger.info(f"[SKIP BUY] {sig.code}: {reason}")
            return
        if sig.code in self.positions:
            return
        budget = self.portfolio.allocate_budget(sig.code)
        if budget <= 0 or sig.price <= 0:
            return
        qty = int(budget // sig.price)
        if qty <= 0:
            return
        try:
            await self.kis.buy_market(sig.code, qty)
        except Exception as e:
            await self.tg.alert(f"매수 실패 {sig.code}: {e}")
            return

        pos = Position(code=sig.code, entry_price=sig.price, qty=qty, opened_at=datetime.now())
        self.positions[sig.code] = pos
        await self._log_trade(Trade(sig.code, "BUY", sig.price, qty, datetime.now(), reason=sig.reason))
        await self.tg.notify(f"🟢 매수 {sig.code} x{qty} @ {sig.price:,.0f}\n{sig.reason}")

    # ----- 가격 업데이트 처리 -----
    async def _on_tick(self, evt: Event) -> None:
        data = evt.payload
        code, price = data["code"], float(data["price"])
        self._last_prices[code] = price

        # 시간대 기반 강제 청산
        if self.risk.classify_window() == TradeWindow.FORCE_CLOSE:
            if code in self.positions:
                await self._close(code, price, ExitReason.FORCE_CLOSE)
            return

        pos = self.positions.get(code)
        if not pos:
            return
        pnl = pos.pnl_ratio(price)

        # 손절
        if pnl <= settings.STOP_LOSS_PCT / 100.0:
            await self._close(code, price, ExitReason.STOP_LOSS)
            return

        # 분할 익절
        for idx, (target, ratio) in enumerate(TAKE_PROFIT_LEVELS):
            if idx in pos.tp_hit:
                continue
            if pnl >= target:
                await self._partial_close(code, price, ratio, idx)

        # 일손실 체크
        self._update_equity()

    # ----- 체결/로그 -----
    async def _close(self, code: str, price: float, reason: ExitReason) -> None:
        pos = self.positions.pop(code, None)
        if not pos:
            return
        try:
            await self.kis.sell_market(code, pos.qty)
        except Exception as e:
            await self.tg.alert(f"매도 실패 {code}: {e}")
            return
        pnl = (price - pos.entry_price) * pos.qty
        await self._log_trade(Trade(code, "SELL", price, pos.qty, datetime.now(),
                                    reason=reason.value, pnl=pnl, exit_reason=reason))
        emoji = "🔴" if reason == ExitReason.STOP_LOSS else "🟡"
        await self.tg.notify(f"{emoji} {reason.value} {code} x{pos.qty} @ {price:,.0f} (PNL {pnl:+,.0f})")

    async def _partial_close(self, code: str, price: float, ratio: float, tp_idx: int) -> None:
        pos = self.positions.get(code)
        if not pos:
            return
        qty = max(1, int(pos.qty * ratio))
        if qty >= pos.qty:
            await self._close(code, price, ExitReason.TAKE_PROFIT)
            return
        try:
            await self.kis.sell_market(code, qty)
        except Exception as e:
            await self.tg.alert(f"부분매도 실패 {code}: {e}")
            return
        pos.qty -= qty
        pos.tp_hit.add(tp_idx)
        pnl = (price - pos.entry_price) * qty
        pos.realized_pnl += pnl
        await self._log_trade(Trade(code, "SELL", price, qty, datetime.now(),
                                    reason=f"TP{tp_idx+1}", pnl=pnl, exit_reason=ExitReason.TAKE_PROFIT))
        await self.tg.notify(f"💰 분할익절 TP{tp_idx+1} {code} x{qty} @ {price:,.0f} (+{pnl:,.0f})")

    async def _log_trade(self, t: Trade) -> None:
        self._trade_log.parent.mkdir(parents=True, exist_ok=True)
        new = not self._trade_log.exists()
        with self._trade_log.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "code", "side", "price", "qty", "reason", "pnl", "exit_reason"])
            w.writerow([t.ts.isoformat(), t.code, t.side, t.price, t.qty,
                        t.reason, t.pnl, t.exit_reason.value if t.exit_reason else ""])

    def _update_equity(self) -> None:
        equity = settings.TOTAL_SEED
        for code, pos in self.positions.items():
            price = self._last_prices.get(code, pos.entry_price)
            equity += (price - pos.entry_price) * pos.qty + pos.realized_pnl
        self.risk.update_equity(equity)
        if self.risk.trading_halted:
            asyncio.create_task(self._halt_liquidate())

    async def _halt_liquidate(self) -> None:
        await self.tg.alert(f"일손실 한도 도달 — 전종목 청산 & 매매중단\n{self.risk.halt_reason}")
        for code in list(self.positions.keys()):
            price = self._last_prices.get(code, self.positions[code].entry_price)
            await self._close(code, price, ExitReason.DAILY_HALT)

    async def run(self) -> None:
        await self._stop.wait()
        logger.info("[ExecutionAgent] stopped")
