"""PortfolioAgent — 텔레그램 명령으로 종목/시드/비중 관리."""
from __future__ import annotations

import asyncio

from loguru import logger

from agents.base_agent import BaseAgent, E, Event, EventBus
from config import settings
from core.telegram_bot import TelegramBot
from data.models import WatchItem


class PortfolioAgent(BaseAgent):
    def __init__(self, bus: EventBus, tg: TelegramBot) -> None:
        super().__init__("PortfolioAgent", bus)
        self.tg = tg
        self.watchlist: dict[str, WatchItem] = {}
        self.total_seed: int = settings.TOTAL_SEED
        self._lock = asyncio.Lock()

        tg.register("add", self._cmd_add)
        tg.register("remove", self._cmd_remove)
        tg.register("seed", self._cmd_seed)
        tg.register("weight", self._cmd_weight)
        tg.register("list", self._cmd_list)
        tg.register("status", self._cmd_status)

    # ----- 명령 핸들러 -----
    async def _cmd_add(self, args: list[str]) -> str:
        if len(args) < 1:
            return "사용법: /add <종목코드> [비중(0~1)] [이름]"
        code = args[0]
        weight = float(args[1]) if len(args) > 1 else 0.0
        name = " ".join(args[2:]) if len(args) > 2 else ""
        async with self._lock:
            self.watchlist[code] = WatchItem(code=code, name=name, weight=weight)
        await self._publish()
        return f"✅ 추가됨: {code} (비중 {weight:.0%})"

    async def _cmd_remove(self, args: list[str]) -> str:
        if not args:
            return "사용법: /remove <종목코드>"
        code = args[0]
        async with self._lock:
            self.watchlist.pop(code, None)
        await self._publish()
        return f"🗑 제거됨: {code}"

    async def _cmd_seed(self, args: list[str]) -> str:
        if not args:
            return f"현재 시드: {self.total_seed:,}"
        self.total_seed = int(args[0])
        return f"💰 시드 설정: {self.total_seed:,}"

    async def _cmd_weight(self, args: list[str]) -> str:
        if len(args) < 2:
            return "사용법: /weight <종목코드> <비중(0~1)>"
        code, w = args[0], float(args[1])
        if code not in self.watchlist:
            return f"❌ 미등록 종목: {code}"
        self.watchlist[code].weight = w
        return f"⚖ {code} 비중 → {w:.0%}"

    async def _cmd_list(self, args: list[str]) -> str:
        if not self.watchlist:
            return "📭 관심종목 없음"
        lines = [f"{w.code} {w.name} w={w.weight:.0%} 배분={self.allocate_budget(w.code):,}"
                 for w in self.watchlist.values()]
        return "📋 관심종목\n" + "\n".join(lines)

    async def _cmd_status(self, args: list[str]) -> str:
        total_w = sum(w.weight for w in self.watchlist.values())
        return (f"시드 {self.total_seed:,}원 | 종목 {len(self.watchlist)}개 | "
                f"총비중 {total_w:.0%}")

    # ----- 비즈니스 로직 -----
    def allocate_budget(self, code: str) -> int:
        item = self.watchlist.get(code)
        if not item:
            return 0
        return int(self.total_seed * item.weight)

    async def _publish(self) -> None:
        await self.bus.publish(Event(
            E.WATCHLIST_UPDATED,
            {c: i for c, i in self.watchlist.items() if i.enabled},
        ))

    async def run(self) -> None:
        # 주기적으로 상태 publish (초기화 시점 공유용)
        while not self._stop.is_set():
            await self._publish()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
        logger.info("[PortfolioAgent] stopped")
