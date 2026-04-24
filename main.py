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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from agents.analysis_agent import AnalysisAgent
from agents.base_agent import EventBus
from agents.execution_agent import ExecutionAgent
from agents.portfolio_agent import PortfolioAgent
from agents.sector_detector import SectorDetector
from config import constants as C
from config import settings
from core.kis_api import KISClient
from core.pick_handlers import register_pick_handlers
from core.telegram_bot import TelegramBot
from data.candle_store import CandleStore
from data.sector_store import SectorStore
from data.stock_master import StockMaster
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
    # PAPER 모드에서도 시세는 실전 서버 사용 → 실전 키 필수
    if settings.KIS_ENV == "PAPER":
        if not settings.KIS_REAL_APP_KEY:
            missing.append("KIS_REAL_APP_KEY")
        if not settings.KIS_REAL_APP_SECRET:
            missing.append("KIS_REAL_APP_SECRET")
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
    sector_store = SectorStore()
    await sector_store.open()
    stock_master = StockMaster()

    portfolio = PortfolioAgent(bus, tg)
    analysis = AnalysisAgent(bus, kis, store=store)
    execution = ExecutionAgent(bus, kis, tg, risk, portfolio)

    register_pick_handlers(tg, sector_store, stock_master)
    await tg.start()
    await tg.notify(f"🤖 Trading Bot 시작 (ENV={settings.KIS_ENV})")

    # 섹터 쏠림 감지기 (Phase 2 Stage 1) — 매 분 10초에 발화.
    # 10초 오프셋 이유: KIS 분봉은 해당 분 마감 후 반영 → 정각 폴링 시
    # "방금 닫힌 분봉"이 미확정 상태로 돌아올 가능성. 10초 여유로 회피.
    detector = SectorDetector(kis, sector_store, tg)
    scheduler = AsyncIOScheduler(timezone=C.KST)
    scheduler.add_job(
        detector.scan_once,
        CronTrigger(
            second=10, minute="*", hour="9-15",
            day_of_week="mon-fri", timezone=C.KST,
        ),
        id="sector_scan",
        max_instances=1,  # 이전 scan_once 진행 중이면 새 트리거 스킵
        coalesce=True,    # 밀린 트리거는 병합 1회만
    )
    scheduler.start()
    logger.info("[sector] APScheduler 시작 — 매 분 10초 scan_once (평일 9~15시)")

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
        # 스케줄러 먼저 정지 — 진행 중인 scan_once() 완료 대기 후 새 트리거 차단
        try:
            scheduler.shutdown(wait=True)
            logger.info("[sector] 스케줄러 종료 완료")
        except Exception as e:
            logger.warning(f"scheduler shutdown 실패: {e}")
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
        await sector_store.close()
        await kis.close()
        logger.info("=== 종료 완료 ===")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
