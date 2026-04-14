"""에이전트 베이스와 간단 asyncio EventBus."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

Handler = Callable[["Event"], Awaitable[None]]


@dataclass
class Event:
    type: str
    payload: Any = None


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        for h in list(self._subs.get(event.type, [])):
            try:
                await h(event)
            except Exception as e:
                logger.exception(f"handler error on {event.type}: {e}")


class BaseAgent(ABC):
    def __init__(self, name: str, bus: EventBus) -> None:
        self.name = name
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._safe_run(), name=self.name)
        logger.info(f"[{self.name}] started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=5)

    async def _safe_run(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"[{self.name}] crashed: {e}")

    @abstractmethod
    async def run(self) -> None: ...


# 공용 이벤트 타입
class E:
    WATCHLIST_UPDATED = "watchlist.updated"
    BUY_SIGNAL = "signal.buy"
    TRADE_FILLED = "trade.filled"
    PNL_UPDATE = "pnl.update"
    HALT = "risk.halt"
    FORCE_CLOSE = "risk.force_close"
    TICK = "market.tick"
    CANDLE_CLOSED = "market.candle_closed"
