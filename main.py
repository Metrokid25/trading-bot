"""3개 에이전트 asyncio 오케스트레이터."""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from agents.analysis_agent import AnalysisAgent
from agents.base_agent import EventBus
from agents.execution_agent import ExecutionAgent
from agents.portfolio_agent import PortfolioAgent
from config import settings
from core.kis_api import KISClient
from core.telegram_bot import TelegramBot
from risk.risk_manager import RiskManager


async def run() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.LOG_LEVEL)
    logger.add(settings.LOG_DIR / "bot_{time:YYYYMMDD}.log",
               level=settings.LOG_LEVEL, rotation="1 day")

    logger.info(f"=== Trading Bot 시작 (ENV={settings.KIS_ENV}) ===")

    bus = EventBus()
    kis = KISClient()
    tg = TelegramBot()
    risk = RiskManager()

    portfolio = PortfolioAgent(bus, tg)
    analysis = AnalysisAgent(bus, kis)
    execution = ExecutionAgent(bus, kis, tg, risk, portfolio)

    await tg.start()
    await tg.notify(f"🤖 Trading Bot 시작 (ENV={settings.KIS_ENV})")

    agents = [portfolio, analysis, execution]
    for a in agents:
        await a.start()

    stop_event = asyncio.Event()

    def _signal_handler(*_):
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _signal_handler)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("종료 신호 수신 — 정리 중...")
        for a in reversed(agents):
            await a.stop()
        await tg.notify("🛑 Trading Bot 종료")
        await tg.stop()
        await kis.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
