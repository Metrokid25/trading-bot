"""AnalysisAgent — 3분봉 기반 RSI/BB/MA 로 BUY 시그널 생성."""
from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from agents.base_agent import BaseAgent, E, Event, EventBus
from core.kis_api import KISClient
from core.websocket_client import KISWebSocket
from data.candle_store import CandleBuffer
from data.models import WatchItem
from strategy.signal import evaluate_buy


class AnalysisAgent(BaseAgent):
    def __init__(self, bus: EventBus, kis: KISClient) -> None:
        super().__init__("AnalysisAgent", bus)
        self.kis = kis
        self.buffers: dict[str, CandleBuffer] = {}
        self.watchlist: dict[str, WatchItem] = {}
        self._ws = KISWebSocket(on_tick=self._on_tick)
        self._ws_task: asyncio.Task | None = None

        bus.subscribe(E.WATCHLIST_UPDATED, self._on_watchlist)

    async def _on_watchlist(self, evt: Event) -> None:
        new: dict[str, WatchItem] = evt.payload
        added = set(new) - set(self.watchlist)
        removed = set(self.watchlist) - set(new)
        self.watchlist = dict(new)
        for code in added:
            self.buffers.setdefault(code, CandleBuffer(code))
            await self._ws.subscribe(code)
        for code in removed:
            self.buffers.pop(code, None)
            await self._ws.unsubscribe(code)

    async def _on_tick(self, code: str, price: int, ts_str: str) -> None:
        buf = self.buffers.get(code)
        if not buf:
            return
        # ts_str: HHMMSS
        try:
            now = datetime.now().replace(
                hour=int(ts_str[:2]), minute=int(ts_str[2:4]), second=int(ts_str[4:6]), microsecond=0
            )
        except Exception:
            now = datetime.now()
        await self.bus.publish(Event(E.TICK, {"code": code, "price": price, "ts": now}))

        closed = buf.on_tick(float(price), now)
        if closed:
            await self.bus.publish(Event(E.CANDLE_CLOSED, {"code": code, "candle": closed, "buf": buf}))
            sig = evaluate_buy(code, buf, now)
            if sig:
                logger.info(f"[BUY SIGNAL] {code} @ {sig.price} — {sig.reason}")
                await self.bus.publish(Event(E.BUY_SIGNAL, sig))

    async def run(self) -> None:
        codes = list(self.watchlist.keys())
        self._ws_task = asyncio.create_task(self._ws.run(codes))
        await self._stop.wait()
        self._ws.stop()
        if self._ws_task:
            self._ws_task.cancel()
        logger.info("[AnalysisAgent] stopped")
