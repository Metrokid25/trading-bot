"""텔레그램 봇 단독 테스트.

PortfolioAgent + TelegramBot 만 기동해 /add, /search, /list, /status, /remove,
/weight, /seed 명령이 실제로 동작하는지 확인한다. KIS API나 다른 에이전트는 기동하지 않는다.
실행 후 텔레그램에서 직접 명령을 쳐보고, Ctrl+C 로 종료.
"""
from __future__ import annotations

import asyncio
import signal

from loguru import logger

from agents.base_agent import EventBus
from agents.portfolio_agent import PortfolioAgent
from core.telegram_bot import TelegramBot


async def main() -> None:
    bus = EventBus()
    tg = TelegramBot()
    agent = PortfolioAgent(bus, tg)

    await tg.start()
    await agent.start()

    await tg.notify(
        "🧪 봇 명령 테스트 모드 시작\n"
        "다음 명령을 시도해보세요:\n"
        "• /add 삼성전자\n"
        "• /add 카카오 0.3\n"
        "• /search 카카오\n"
        "• /list\n"
        "• /status\n"
        "• /del 삼성전자\n"
        "종료는 서버에서 Ctrl+C"
    )
    logger.info("테스트 봇 대기 중… (Ctrl+C 로 종료)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:
        pass  # Windows

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("종료 중…")
        await agent.stop()
        await tg.stop()
        await agent.master.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
