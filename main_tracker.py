"""Phase 2.5 데이터 수집 스케줄러 진입점.

python main_tracker.py 로 실행하면:
 - 매일 16:00 KST에 통합 파이프라인 1회 실행:
   추적행 생성 → 일봉 수집 → 분봉 raw(NXT 장전 포함) → 3분봉 집계
   → 돌파 마킹 → 풀백 감지(dry-run)
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
from core.kis_api import KISClient
from core.pipeline_runner import full_pipeline_job
from strategy.paper_runner import paper_job


async def run() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.LOG_LEVEL)
    logger.add(
        settings.LOG_DIR / "tracker_{time:YYYYMMDD}.log",
        level=settings.LOG_LEVEL,
        rotation="1 day",
        encoding="utf-8",
    )

    logger.info("=== Phase 2.5 Tracker 시작 (DB={}) ===", settings.DB_PATH)

    kis = KISClient()

    async def daily_16h_job() -> None:
        """수집 파이프라인 → 모의투자 기록 (각각 best-effort, 순차)."""
        try:
            await full_pipeline_job(str(settings.DB_PATH), kis)
        except Exception as exc:
            logger.error("[16h] 파이프라인 실패 — 페이퍼 기록은 계속: {}", exc)
        await paper_job()   # 내부에서 예외 흡수

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_16h_job,
        CronTrigger(hour=16, minute=0, timezone="Asia/Seoul"),
        id="full_pipeline",
        name="통합 수집 파이프라인 + 모의투자 기록",
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("스케줄러 시작 — 매일 16:00 KST 파이프라인 + 페이퍼 기록")

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
