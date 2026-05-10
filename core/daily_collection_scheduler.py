"""Phase 2.5 일일 수집 스케줄러 — 오케스트레이션 로직."""
from __future__ import annotations

import asyncio
import time
from datetime import date

import aiosqlite
from loguru import logger

from core.daily_tracker import DailyTracker
from core.time_utils import now_kst


async def run_daily_collection(
    tracker: DailyTracker,
    today: date | None = None,
) -> None:
    """pick_daily_tracking의 당일 이하 pending 행에 대해 collect_daily 순차 실행.

    Args:
        tracker: DailyTracker 인스턴스.
        today: 기준 날짜. None이면 KST 오늘 날짜 사용 (테스트에서 주입 가능).
    """
    if today is None:
        today = now_kst().date()
    today_str = today.isoformat()
    job_start = time.monotonic()

    async with aiosqlite.connect(tracker.db_path, isolation_level=None) as db:
        cur = await db.execute(
            """
            SELECT pdt.event_id, pdt.stock_pick_id, pdt.trading_day, ss.stock_code
            FROM pick_daily_tracking pdt
            JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
            WHERE pdt.trading_day <= ? AND pdt.status = 'pending'
            ORDER BY pdt.trading_day, pdt.event_id, pdt.stock_pick_id
            """,
            (today_str,),
        )
        targets = await cur.fetchall()

    logger.info("[D4] daily collection started, targets=%d", len(targets))

    success_count = 0
    failed_count = 0
    failed_list: list[str] = []

    for event_id, stock_pick_id, trading_day, ticker in targets:
        row_start = time.monotonic()
        try:
            result = await tracker.collect_daily(
                event_id, ticker, date.fromisoformat(trading_day)
            )
            elapsed = time.monotonic() - row_start
            result_str = "success" if result else "failed"
            logger.info(
                "[D4] event_id=%d ticker=%s trading_day=%s result=%s elapsed=%.1fs",
                event_id, ticker, trading_day, result_str, elapsed,
            )
            if result:
                success_count += 1
            else:
                failed_count += 1
                failed_list.append(f"{ticker}@{trading_day}")
        except Exception as exc:
            elapsed = time.monotonic() - row_start
            logger.warning(
                "[D4] event_id=%d ticker=%s trading_day=%s result=failed elapsed=%.1fs error=%s",
                event_id, ticker, trading_day, elapsed, exc,
            )
            failed_count += 1
            failed_list.append(f"{ticker}@{trading_day}")

        await asyncio.sleep(0.1)

    total_elapsed = time.monotonic() - job_start
    failed_note = f", failed_list={failed_list}" if failed_list else ""
    logger.info(
        "[D4] daily collection finished, success=%d, failed=%d, elapsed=%.1fs%s",
        success_count, failed_count, total_elapsed, failed_note,
    )


async def daily_collection_job(tracker: DailyTracker, kis_client) -> None:
    """APScheduler job 함수: 토큰 갱신 후 run_daily_collection 실행."""
    try:
        await kis_client._ensure_real_token()
    except Exception as exc:
        logger.warning("[D4] KIS 토큰 갱신 실패: %s — 수집 계속 시도", exc)

    try:
        await run_daily_collection(tracker)
    except Exception as exc:
        logger.error("[D4] daily_collection_job 예외: %s", exc)
