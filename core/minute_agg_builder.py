"""Build 3-minute and 5-minute aggregate bars from stored 1-minute raw bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import aiosqlite
from loguru import logger

from core.time_utils import now_kst, to_db_iso

_ALLOWED_INTERVALS = {3, 5}


class MinuteAggResult(Enum):
    SUCCESS = "success"
    SKIPPED_NO_TARGET = "skipped_no_target"
    SKIPPED_NO_RAW = "skipped_no_raw"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MinuteAggTarget:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int


@dataclass(frozen=True, slots=True)
class MinuteRawRow:
    minute_time: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    value: int | None


@dataclass(frozen=True, slots=True)
class MinuteAggBar:
    interval_minutes: int
    bucket_start: str
    bucket_end: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int
    value: int | None
    raw_count: int
    expected_count: int
    is_complete: int
    source: str = "RAW_1M"


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _normalize_intervals(intervals: tuple[int, ...]) -> tuple[int, ...]:
    if not intervals:
        raise ValueError("intervals must not be empty")
    if len(set(intervals)) != len(intervals):
        raise ValueError("intervals must not contain duplicates")
    invalid = [interval for interval in intervals if interval not in _ALLOWED_INTERVALS]
    if invalid:
        raise ValueError(f"unsupported intervals: {invalid}")
    return intervals


class MinuteAggBuilder:
    def __init__(
        self,
        db_path: str,
        *,
        session_start_hour: int = 9,
        session_start_minute: int = 0,
    ) -> None:
        """N분봉 집계기.

        session_start_hour/minute: 버킷 정렬 기준 세션 시작 시각. 기본 09:00.
            이 시각 이전 분봉은 버려진다. NXT 장전(08:00~09:00)을 3/5분봉으로
            집계하려면 session_start_hour=8. 08:00~09:00은 60분이라 3·5분 모두
            정수 배수 → 정규장(09:00) 버킷 경계도 그대로 정렬된다.
        """
        self.db_path = db_path
        self.session_start_hour = session_start_hour
        self.session_start_minute = session_start_minute

    async def list_d0_targets(
        self, trading_day: str | None = None
    ) -> list[MinuteAggTarget]:
        params: tuple[str, ...] = ()
        trading_day_filter = ""
        if trading_day is not None:
            trading_day_filter = " AND pdt.trading_day = ?"
            params = (trading_day,)

        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                f"""
                SELECT DISTINCT
                    pdt.id,
                    pmr.event_id,
                    pmr.stock_pick_id,
                    pmr.stock_code,
                    pdt.trading_day,
                    pdt.day_offset
                FROM pick_daily_tracking pdt
                JOIN pick_minute_raw pmr ON pmr.daily_tracking_id = pdt.id
                WHERE pdt.day_offset = 0
                  {trading_day_filter}
                ORDER BY pmr.event_id, pmr.stock_pick_id
                """,
                params,
            )
            rows = await cur.fetchall()

        return [
            MinuteAggTarget(
                daily_tracking_id=int(row[0]),
                event_id=int(row[1]),
                stock_pick_id=int(row[2]),
                stock_code=str(row[3]),
                trading_day=str(row[4]),
                day_offset=int(row[5]),
            )
            for row in rows
        ]

    async def _get_target(self, daily_tracking_id: int) -> MinuteAggTarget | None:
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                """
                SELECT
                    pdt.id,
                    pdt.event_id,
                    pdt.stock_pick_id,
                    ss.stock_code,
                    pdt.trading_day,
                    pdt.day_offset
                FROM pick_daily_tracking pdt
                JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
                WHERE pdt.id = ?
                  AND pdt.day_offset = 0
                LIMIT 1
                """,
                (daily_tracking_id,),
            )
            row = await cur.fetchone()

        if row is None:
            return None
        return MinuteAggTarget(
            daily_tracking_id=int(row[0]),
            event_id=int(row[1]),
            stock_pick_id=int(row[2]),
            stock_code=str(row[3]),
            trading_day=str(row[4]),
            day_offset=int(row[5]),
        )

    async def _load_raw_rows(
        self, daily_tracking_id: int, trading_day: str
    ) -> list[MinuteRawRow]:
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                """
                SELECT minute_time, open, high, low, close, volume, value
                FROM pick_minute_raw
                WHERE daily_tracking_id = ?
                  AND trading_day = ?
                ORDER BY minute_time, id
                """,
                (daily_tracking_id, trading_day),
            )
            rows = await cur.fetchall()

        return [
            MinuteRawRow(
                minute_time=str(row[0]),
                open=_optional_float(row[1]),
                high=_optional_float(row[2]),
                low=_optional_float(row[3]),
                close=_optional_float(row[4]),
                volume=_optional_int(row[5]),
                value=_optional_int(row[6]),
            )
            for row in rows
        ]

    def _bucket_bounds(
        self, minute_time: str, interval_minutes: int, trading_day: str | None = None
    ) -> tuple[str, str, datetime] | None:
        try:
            ts = datetime.fromisoformat(minute_time)
        except ValueError:
            return None

        if trading_day is not None and ts.date().isoformat() != trading_day:
            return None

        session_start = ts.replace(
            hour=self.session_start_hour,
            minute=self.session_start_minute,
            second=0,
            microsecond=0,
        )
        if ts < session_start:
            return None

        minutes_since_start = int((ts - session_start).total_seconds() // 60)
        bucket_offset = (minutes_since_start // interval_minutes) * interval_minutes
        bucket_start = session_start + timedelta(minutes=bucket_offset)
        bucket_end = bucket_start + timedelta(minutes=interval_minutes - 1)
        return bucket_start.isoformat(), bucket_end.isoformat(), ts

    def build_agg_bars(
        self,
        raw_rows: list[MinuteRawRow],
        interval_minutes: int,
        trading_day: str | None = None,
    ) -> list[MinuteAggBar]:
        buckets: dict[str, list[tuple[datetime, MinuteRawRow, str]]] = {}

        for row in raw_rows:
            bounds = self._bucket_bounds(
                row.minute_time, interval_minutes, trading_day=trading_day
            )
            if bounds is None:
                logger.debug(
                    "[minute_agg] skipped raw row minute_time={} interval={}",
                    row.minute_time,
                    interval_minutes,
                )
                continue
            bucket_start, bucket_end, ts = bounds
            buckets.setdefault(bucket_start, []).append((ts, row, bucket_end))

        bars: list[MinuteAggBar] = []
        for bucket_start in sorted(buckets):
            items = sorted(buckets[bucket_start], key=lambda item: item[0])
            rows = [item[1] for item in items]
            values = [row.value for row in rows if row.value is not None]
            highs = [row.high for row in rows if row.high is not None]
            lows = [row.low for row in rows if row.low is not None]
            volume = sum(row.volume or 0 for row in rows)

            bars.append(
                MinuteAggBar(
                    interval_minutes=interval_minutes,
                    bucket_start=bucket_start,
                    bucket_end=items[0][2],
                    open=rows[0].open,
                    high=max(highs) if highs else None,
                    low=min(lows) if lows else None,
                    close=rows[-1].close,
                    volume=volume,
                    value=sum(values) if values else None,
                    raw_count=len(rows),
                    expected_count=interval_minutes,
                    is_complete=1 if len(rows) == interval_minutes else 0,
                )
            )
        return bars

    async def _replace_agg_bars(
        self,
        target: MinuteAggTarget,
        bars: list[MinuteAggBar],
        intervals: tuple[int, ...],
    ) -> int:
        now_iso = to_db_iso(now_kst())
        placeholders = ", ".join("?" for _ in intervals)
        params = [
            (
                target.daily_tracking_id,
                target.event_id,
                target.stock_pick_id,
                target.stock_code,
                target.trading_day,
                target.day_offset,
                bar.interval_minutes,
                bar.bucket_start,
                bar.bucket_end,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.value,
                bar.raw_count,
                bar.expected_count,
                bar.is_complete,
                bar.source,
                now_iso,
                now_iso,
            )
            for bar in bars
        ]

        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "DELETE FROM pick_minute_agg"
                    f" WHERE daily_tracking_id = ? AND interval_minutes IN ({placeholders})",
                    (target.daily_tracking_id, *intervals),
                )
                cursor = await db.executemany(
                    """
                    INSERT INTO pick_minute_agg
                        (daily_tracking_id, event_id, stock_pick_id, stock_code,
                         trading_day, day_offset, interval_minutes,
                         bucket_start, bucket_end,
                         open, high, low, close, volume, value,
                         raw_count, expected_count, is_complete, source,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise
        return cursor.rowcount or 0

    async def aggregate_for_tracking_row(
        self,
        daily_tracking_id: int,
        intervals: tuple[int, ...] = (3, 5),
    ) -> MinuteAggResult:
        try:
            intervals = _normalize_intervals(intervals)
            target = await self._get_target(daily_tracking_id)
            if target is None:
                return MinuteAggResult.SKIPPED_NO_TARGET

            raw_rows = await self._load_raw_rows(
                daily_tracking_id, target.trading_day
            )
            if not raw_rows:
                return MinuteAggResult.SKIPPED_NO_RAW

            bars: list[MinuteAggBar] = []
            for interval in intervals:
                bars.extend(
                    self.build_agg_bars(
                        raw_rows, interval, trading_day=target.trading_day
                    )
                )

            if not bars:
                return MinuteAggResult.SKIPPED_NO_RAW

            await self._replace_agg_bars(target, bars, intervals)
            return MinuteAggResult.SUCCESS
        except Exception as exc:
            logger.warning(
                "[minute_agg] aggregate failed daily_tracking_id={} error={}",
                daily_tracking_id,
                exc,
            )
            return MinuteAggResult.FAILED

    async def aggregate_all_d0(
        self,
        trading_day: str | None = None,
        intervals: tuple[int, ...] = (3, 5),
    ) -> dict[str, int]:
        counts = {result.value: 0 for result in MinuteAggResult}
        try:
            intervals = _normalize_intervals(intervals)
        except ValueError as exc:
            logger.warning("[minute_agg] invalid intervals error={}", exc)
            counts[MinuteAggResult.FAILED.value] += 1
            return counts

        targets = await self.list_d0_targets(trading_day=trading_day)
        for target in targets:
            result = await self.aggregate_for_tracking_row(
                target.daily_tracking_id, intervals=intervals
            )
            counts[result.value] += 1
        return counts
