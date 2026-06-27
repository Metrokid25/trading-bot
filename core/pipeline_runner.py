"""Phase 2.5 통합 수집 파이프라인.

장마감 후 1회 실행하며, 흩어져 있던 수집 모듈들을 순서대로 연결한다:

  1. 활성 sector_pick_events → ensure_tracking_rows (추적행 생성, 멱등)
  2. 일봉 수집        (run_daily_collection)
  3. 분봉 raw 수집    (NXT 장전 08:00 포함 옵션)
  4. 3분봉 집계
  5. 돌파 마킹
  6. 풀백 감지        (dry-run, 로그만)

각 단계는 best-effort — 한 단계가 실패해도 다음 단계는 계속 진행한다.
시세 API는 항상 REAL 서버를 쓰므로 KIS_ENV 와 무관하다(불변식 유지).
"""
from __future__ import annotations

from datetime import date

import aiosqlite
from loguru import logger

from core.breakout_marker import BreakoutMarker
from core.daily_collection_scheduler import run_daily_collection
from core.daily_tracker import DailyTracker
from core.minute_agg_builder import MinuteAggBuilder
from core.minute_raw_tracker import MinuteRawTracker
from core.pullback_detector import PullbackDetector
from core.time_utils import now_kst


async def _active_event_ids(db_path: str) -> list[int]:
    """활성 종목을 가진 sector_pick_events 의 event_id 목록."""
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        cur = await db.execute(
            """
            SELECT DISTINCT spe.event_id
            FROM sector_pick_events spe
            JOIN sector_stocks ss
                ON ss.pick_id = spe.pick_id AND ss.sector_name = spe.sector_name
            WHERE ss.tracking_status = 'active'
            ORDER BY spe.event_id
            """
        )
        return [int(r[0]) for r in await cur.fetchall()]


async def ensure_all_tracking_rows(tracker: DailyTracker) -> int:
    """활성 이벤트 전체에 대해 추적행을 생성(멱등)하고 생성된 행 수를 반환.

    이게 빠져 있던 핵심 연결고리다 — 이전엔 ensure_tracking_rows 를 아무도
    호출하지 않아 pick_daily_tracking 이 영영 비어 있었다.
    """
    created = 0
    for event_id in await _active_event_ids(tracker.db_path):
        try:
            created += await tracker.ensure_tracking_rows(event_id)
        except Exception as exc:
            logger.warning(
                "[pipeline] ensure_tracking_rows 실패 event_id={} error={}",
                event_id, exc,
            )
    return created


async def run_full_pipeline(
    db_path: str,
    kis,
    *,
    today: date | None = None,
    include_nxt: bool = True,
) -> dict[str, object]:
    """Phase 2.5 통합 수집 파이프라인 1회 실행. 각 단계 best-effort.

    Args:
        db_path: 누적 DB 경로.
        kis: KISClient (시세=REAL 서버).
        today: 기준일. None이면 KST 오늘.
        include_nxt: True면 NXT 장전(08:00~09:00) 분봉까지 수집/집계.
    """
    if today is None:
        today = now_kst().date()
    today_str = today.isoformat()
    market_code = "UN" if include_nxt else "J"
    floor_hour = 8 if include_nxt else 9
    session_start_hour = 8 if include_nxt else 9

    summary: dict[str, object] = {"trading_day": today_str, "include_nxt": include_nxt}
    tracker = DailyTracker(db_path, kis)

    async def _stage(key: str, factory):
        # factory를 try 안에서 호출 → 객체 생성·인자 바인딩 예외까지 best-effort로 흡수.
        try:
            summary[key] = await factory()
        except Exception as exc:
            logger.error("[pipeline] {} 단계 실패: {}", key, exc)
            summary[key] = None

    # 1. 추적행 생성 (핵심 연결고리)
    await _stage("tracking_rows_created", lambda: ensure_all_tracking_rows(tracker))
    # 2. 일봉 수집
    await _stage("daily", lambda: run_daily_collection(tracker, today=today))
    # 3. 분봉 raw 수집 (NXT 장전 포함 옵션)
    await _stage(
        "minute_raw",
        lambda: MinuteRawTracker(
            db_path, kis, market_code=market_code, floor_hour=floor_hour
        ).collect_d0_all(trading_day=today_str),
    )
    # 4. 3분봉 집계
    await _stage(
        "minute_agg",
        lambda: MinuteAggBuilder(
            db_path, session_start_hour=session_start_hour
        ).aggregate_all_d0(trading_day=today_str),
    )
    # 5. 돌파 마킹
    await _stage(
        "breakout",
        lambda: BreakoutMarker(db_path).mark_all_d0(trading_day=today_str),
    )

    # 6. 풀백 감지 (dry-run, 로그만)
    try:
        counts, signals = await PullbackDetector(db_path).detect_all_d0(
            trading_day=today_str
        )
        summary["pullback"] = counts
        summary["pullback_signals"] = len(signals)
        for s in signals:
            logger.info(
                "[pipeline][PULLBACK dry-run] {} {} {}~{} low={} last_close={}",
                s.stock_code, s.trading_day, s.window_start, s.window_end,
                s.window_low, s.last_close,
            )
    except Exception as exc:
        logger.error("[pipeline] pullback 단계 실패: {}", exc)
        summary["pullback"] = None

    logger.info("[pipeline] 완료: {}", summary)
    return summary


async def full_pipeline_job(db_path: str, kis, *, include_nxt: bool = True) -> None:
    """APScheduler job: KIS 토큰 확보 후 통합 파이프라인 실행."""
    try:
        await kis._ensure_real_token()
    except Exception as exc:
        logger.error("[pipeline] KIS 토큰 갱신 실패 — 수집 중단: {}", exc)
        raise
    try:
        await run_full_pipeline(db_path, kis, include_nxt=include_nxt)
    except Exception as exc:
        logger.error("[pipeline] full_pipeline_job 예외: {}", exc)
