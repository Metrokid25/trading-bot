"""Phase 2.5 일일 수집 스케줄러 진입점.

python main_tracker.py 로 실행하면:
 - 매일 16:00 KST에 pick_daily_tracking의 pending 행을 KIS에서 수집
 - sector_detector 알림 봇(main.py)과 독립 프로세스로 동작
Ctrl+C 로 종료.
"""
from __future__ import annotations

import asyncio
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import settings
from core.daily_collection_scheduler import daily_collection_job
from core.daily_tracker import DailyTracker
from core.kis_api import KISClient


async def run() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.LOG_LEVEL)
    logger.add(
        settings.LOG_DIR / "tracker_{time:YYYYMMDD}.log",
        level=settings.LOG_LEVEL,
        rotation="1 day",
        encoding="utf-8",
    )

    logger.info("=== Phase 2.5 Tracker 시작 (DB=%s) ===", settings.DB_PATH)

    kis = KISClient()
    tracker = DailyTracker(str(settings.DB_PATH), kis)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_collection_job,
        CronTrigger(hour=16, minute=0, timezone="Asia/Seoul"),
        args=[tracker, kis],
        id="daily_collection",
        name="일일 수집",
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("스케줄러 시작 — 매일 16:00 KST 실행")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("스케줄러 종료 중...")
        scheduler.shutdown(wait=True)
        logger.info("종료 완료")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
