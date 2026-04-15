"""ExecutionAgent — ATR 기반 손절/분할익절/트레일링 + VWAP/MACD 청산시그널."""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime
from pathlib import Path

from loguru import logger

from agents.base_agent import BaseAgent, E, Event, EventBus
from agents.portfolio_agent import PortfolioAgent
from config import settings
from config.constants import (
    ATR_STOP_MULT,
    ATR_TP_MULTS,
    ATR_TP_RATIOS,
    ATR_TRAILING_TRIGGER,
    TP_STOP_BUFFER_ATR,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    VOLUME_LOOKBACK,
    VOLUME_SURGE_MULT,
    ExitReason,
    TradeWindow,
)
from core.kis_api import KISClient
from core.telegram_bot import TelegramBot
from data.candle_store import CandleBuffer
from data.models import Position, Signal, Trade
from risk.risk_manager import RiskManager
from strategy.indicators import macd_hist_series, vwap


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
        bus.subscribe(E.CANDLE_CLOSED, self._on_candle_closed)

    # ----- 시그널 처리 -----
    async def _on_signal(self, evt: Event) -> None:
        sig: Signal = evt.payload
        ok, reason = self.risk.can_open_new(len(self.positions))
        if not ok:
            logger.info(f"[SKIP BUY] {sig.code}: {reason}")
            return
        if sig.code in self.positions:
            return

        atr_val = float(sig.meta.get("atr", 0.0) or 0.0)
        if atr_val <= 0 or sig.price <= 0:
            logger.warning(f"[SKIP BUY] {sig.code}: invalid ATR/price ATR={atr_val} price={sig.price}")
            return

        qty = self.risk.position_size(sig.price, atr_val)
        if qty <= 0:
            logger.info(f"[SKIP BUY] {sig.code}: qty=0 (risk sizing)")
            return

        try:
            await self.kis.buy_market(sig.code, qty)
        except Exception as e:
            await self.tg.alert(f"매수 실패 {sig.code}: {e}")
            return

        stop_price = sig.price - atr_val * ATR_STOP_MULT
        tp_prices = [sig.price + atr_val * m for m in ATR_TP_MULTS]

        pos = Position(
            code=sig.code,
            entry_price=sig.price,
            qty=qty,
            opened_at=datetime.now(),
            atr=atr_val,
            stop_price=stop_price,
            tp_prices=tp_prices,
        )
        self.positions[sig.code] = pos

        await self._log_trade(
            Trade(
                sig.code, "BUY", sig.price, qty, datetime.now(),
                reason=sig.reason, atr=atr_val, stop_price=stop_price,
                tp_prices=tuple(tp_prices),
            )
        )
        await self.tg.notify(
            f"🟢 매수 {sig.code} x{qty} @ {sig.price:,.0f}\n"
            f"ATR={atr_val:.1f} SL={stop_price:,.0f} "
            f"TP={'/'.join(f'{p:,.0f}' for p in tp_prices)}\n{sig.reason}"
        )

    # ----- 가격 업데이트 처리 -----
    async def _on_tick(self, evt: Event) -> None:
        data = evt.payload
        code, price = data["code"], float(data["price"])
        self._last_prices[code] = price

        # HARD halt 시 전청산
        if self.risk.hard_halt and code in self.positions:
            await self._close(code, price, ExitReason.DAILY_HALT)
            return

        # 장마감 강제 청산
        if self.risk.classify_window() == TradeWindow.FORCE_CLOSE:
            if code in self.positions:
                await self._close(code, price, ExitReason.FORCE_CLOSE)
            return

        pos = self.positions.get(code)
        if not pos:
            self._update_equity()
            return

        # 손절 (동적 stop_price)
        if price <= pos.stop_price:
            reason = ExitReason.TRAIL_STOP if pos.trailing_activated else ExitReason.STOP_LOSS
            await self._close(code, price, reason)
            return

        # 트레일링: +1 ATR 도달 시 본절 이동 (1회성)
        if not pos.trailing_activated and pos.atr > 0:
            if price >= pos.entry_price + pos.atr * ATR_TRAILING_TRIGGER:
                pos.trailing_activated = True
                if pos.entry_price > pos.stop_price:
                    pos.stop_price = pos.entry_price
                logger.info(f"[TRAIL] {code} 본절 이동 → stop={pos.stop_price:,.0f}")

        # 분할 익절 (ATR 기반 절대가)
        for idx, (target_price, ratio) in enumerate(zip(pos.tp_prices, ATR_TP_RATIOS)):
            if idx in pos.tp_hit:
                continue
            if price >= target_price:
                await self._partial_close(code, price, ratio, idx, target_price)

        self._update_equity()

    # ----- 캔들 마감 시 청산시그널 -----
    async def _on_candle_closed(self, evt: Event) -> None:
        data = evt.payload
        if not isinstance(data, dict):
            return
        code = data.get("code")
        buf: CandleBuffer | None = data.get("buf")
        if not code or code not in self.positions or buf is None:
            return

        candles = buf.candles()
        if len(candles) < max(MACD_SLOW + MACD_SIGNAL, VOLUME_LOOKBACK + 2):
            return

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        vols = [c.volume for c in candles]

        today = candles[-1].ts.date()
        sess = next((i for i, c in enumerate(candles) if c.ts.date() == today), len(candles) - 1)
        vwap3 = vwap(highs[sess:], lows[sess:], closes[sess:], vols[sess:])
        hist = macd_hist_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        price = closes[-1]

        # VWAP 아래 종가 2봉 연속 이탈 + 거래량 급증 → 전량 청산
        vol_window = vols[-(VOLUME_LOOKBACK + 1):-1]
        avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
        two_bar_break = len(closes) >= 2 and closes[-1] < vwap3 and closes[-2] < vwap3
        if two_bar_break and avg_vol > 0 and vols[-1] >= avg_vol * VOLUME_SURGE_MULT:
            await self._close(code, price, ExitReason.VWAP_BREAK)
            return

        # MACD 히스토그램 양→음 전환 → 잔여 전량 청산
        if len(hist) >= 2 and hist[-2] > 0 >= hist[-1]:
            await self._close(code, price, ExitReason.MACD_FLIP)

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
        pnl = (price - pos.entry_price) * pos.qty + pos.realized_pnl
        await self._log_trade(
            Trade(
                code, "SELL", price, pos.qty, datetime.now(),
                reason=reason.value, pnl=pnl, exit_reason=reason,
                atr=pos.atr, stop_price=pos.stop_price, tp_prices=tuple(pos.tp_prices),
            )
        )
        emoji = "🔴" if reason in (ExitReason.STOP_LOSS, ExitReason.DAILY_HALT) else "🟡"
        await self.tg.notify(
            f"{emoji} {reason.value} {code} x{pos.qty} @ {price:,.0f} (PNL {pnl:+,.0f})"
        )

    async def _partial_close(
        self, code: str, price: float, ratio: float, tp_idx: int, tp_price: float
    ) -> None:
        pos = self.positions.get(code)
        if not pos:
            return
        # ratio >= 1.0 또는 잔량 청산
        if ratio >= 1.0 or int(pos.qty * ratio) >= pos.qty:
            await self._close(code, price, ExitReason.TAKE_PROFIT)
            return
        qty = max(1, int(pos.qty * ratio))
        try:
            await self.kis.sell_market(code, qty)
        except Exception as e:
            await self.tg.alert(f"부분매도 실패 {code}: {e}")
            return
        pos.qty -= qty
        pos.tp_hit.add(tp_idx)
        pnl = (price - pos.entry_price) * qty
        pos.realized_pnl += pnl
        # 손절선을 "직전 익절가 - 0.5 ATR" 로 상향 (약간의 눌림 허용)
        new_stop = tp_price - pos.atr * TP_STOP_BUFFER_ATR
        if new_stop > pos.stop_price:
            pos.stop_price = new_stop
        await self._log_trade(
            Trade(
                code, "SELL", price, qty, datetime.now(),
                reason=f"TP{tp_idx+1}", pnl=pnl, exit_reason=ExitReason.TAKE_PROFIT,
                atr=pos.atr, stop_price=pos.stop_price, tp_prices=tuple(pos.tp_prices),
            )
        )
        await self.tg.notify(
            f"💰 분할익절 TP{tp_idx+1} {code} x{qty} @ {price:,.0f} (+{pnl:,.0f})\n"
            f"잔여 {pos.qty} · SL↑ {pos.stop_price:,.0f}"
        )

    async def _log_trade(self, t: Trade) -> None:
        self._trade_log.parent.mkdir(parents=True, exist_ok=True)
        new = not self._trade_log.exists()
        with self._trade_log.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow([
                    "ts", "code", "side", "price", "qty", "reason",
                    "pnl", "exit_reason", "atr", "stop_price", "tp_prices",
                ])
            w.writerow([
                t.ts.isoformat(), t.code, t.side, t.price, t.qty,
                t.reason, t.pnl, t.exit_reason.value if t.exit_reason else "",
                t.atr, t.stop_price,
                "/".join(f"{p:.0f}" for p in t.tp_prices) if t.tp_prices else "",
            ])

    def _update_equity(self) -> None:
        equity = settings.TOTAL_SEED
        for code, pos in self.positions.items():
            price = self._last_prices.get(code, pos.entry_price)
            equity += (price - pos.entry_price) * pos.qty + pos.realized_pnl
        self.risk.update_equity(equity)
        if self.risk.hard_halt:
            asyncio.create_task(self._halt_liquidate())

    async def _halt_liquidate(self) -> None:
        await self.tg.alert(f"일손실 HARD halt — 전종목 청산 & 매매중단\n{self.risk.halt_reason}")
        for code in list(self.positions.keys()):
            price = self._last_prices.get(code, self.positions[code].entry_price)
            await self._close(code, price, ExitReason.DAILY_HALT)

    async def run(self) -> None:
        await self._stop.wait()
        logger.info("[ExecutionAgent] stopped")
