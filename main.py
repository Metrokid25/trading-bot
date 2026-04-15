"""에이전트 오케스트레이터.

python main.py 로 실행하면:
 - 텔레그램 봇 polling 시작
 - CandleStore(SQLite) open
 - Portfolio/Analysis/Execution 에이전트 구동
 - 실시간 WS 틱 수신 → 3분봉 집계 → 시그널 → 자동매매
 - 장 종료 후 당일 분봉 보완 수집
Ctrl+C 로 종료.
"""
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
from data.candle_store import CandleStore
from risk.risk_manager import RiskManager


async def run() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.LOG_LEVEL)
    logger.add(settings.LOG_DIR / "bot_{time:YYYYMMDD}.log",
               level=settings.LOG_LEVEL, rotation="1 day", encoding="utf-8")

    logger.info(f"=== Trading Bot 시작 (ENV={settings.KIS_ENV}) ===")

    # 필수 환경변수 체크
    missing: list[str] = []
    if not settings.app_key:
        missing.append(f"KIS_{settings.KIS_ENV}_APP_KEY")
    if not settings.app_secret:
        missing.append(f"KIS_{settings.KIS_ENV}_APP_SECRET")
    if not settings.account_no:
        missing.append(f"KIS_{settings.KIS_ENV}_ACCOUNT_NO")
    if missing:
        logger.error(f".env 필수값 누락: {', '.join(missing)}")
        return
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 미설정 — 텔레그램 명령/알림 비활성화")

    bus = EventBus()
    kis = KISClient()
    tg = TelegramBot()
    risk = RiskManager()
    store = CandleStore()
    await store.open()

    portfolio = PortfolioAgent(bus, tg)
    analysis = AnalysisAgent(bus, kis, store=store)
    execution = ExecutionAgent(bus, kis, tg, risk, portfolio)

    await tg.start()
    await tg.notify(f"🤖 Trading Bot 시작 (ENV={settings.KIS_ENV})")

    agents = [portfolio, analysis, execution]
    for a in agents:
        await a.start()

    stop_event = asyncio.Event()

    def _signal_handler(*_):
        logger.info("종료 신호 수신")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _signal_handler)

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("정리 중...")
        for a in reversed(agents):
            try:
                await a.stop()
            except Exception as e:
                logger.warning(f"{a.name} stop 실패: {e}")
        try:
            await tg.notify("🛑 Trading Bot 종료")
        except Exception:
            pass
        await tg.stop()
        await store.close()
        await kis.close()
        logger.info("=== 종료 완료 ===")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
