"""D+0 minute raw collection for Phase 2.5.

The tracker reuses KISClient.get_minute_candles_at() through duck typing so
tests can inject a mock client. It is not wired into main.py/main_tracker.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

import aiosqlite
from loguru import logger

from core.time_utils import now_kst, to_db_iso


class MinuteCollectResult(Enum):
    SUCCESS = "success"
    SKIPPED_NO_TARGET = "skipped_no_target"
    SKIPPED_ALREADY_EXISTS = "skipped_already_exists"
    SKIPPED_NO_BARS = "skipped_no_bars"
    FAILED = "failed"


class MinuteCandleClient(Protocol):
    async def get_minute_candles_at(
        self, code: str, hhmmss: str, past_data: bool = True
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class MinuteTarget:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int


@dataclass(frozen=True, slots=True)
class MinuteRawBar:
    stock_code: str
    trading_day: str
    minute_time: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    value: int | None
    source: str = "KIS"


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class MinuteRawTracker:
    def __init__(self, db_path: str, kis_client: MinuteCandleClient) -> None:
        self.db_path = db_path
        self.kis_client = kis_client

    async def list_d0_targets(
        self, trading_day: str | None = None
    ) -> list[MinuteTarget]:
        params: tuple[str, ...] = ()
        trading_day_filter = ""
        if trading_day is not None:
            trading_day_filter = " AND pdt.trading_day = ?"
            params = (trading_day,)

        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                f"""
                SELECT
                    pdt.id AS daily_tracking_id,
                    pdt.event_id,
                    pdt.stock_pick_id,
                    ss.stock_code,
                    pdt.trading_day,
                    pdt.day_offset
                FROM pick_daily_tracking pdt
                JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
                WHERE pdt.day_offset = 0
                  AND ss.tracking_status = 'active'
                  {trading_day_filter}
                ORDER BY pdt.event_id, pdt.stock_pick_id
                """,
                params,
            )
            rows = await cur.fetchall()

        return [
            MinuteTarget(
                daily_tracking_id=int(row[0]),
                event_id=int(row[1]),
                stock_pick_id=int(row[2]),
                stock_code=str(row[3]),
                trading_day=str(row[4]),
                day_offset=int(row[5]),
            )
            for row in rows
        ]

    async def _get_target(self, daily_tracking_id: int) -> MinuteTarget | None:
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
                  AND ss.tracking_status = 'active'
                LIMIT 1
                """,
                (daily_tracking_id,),
            )
            row = await cur.fetchone()

        if row is None:
            return None
        return MinuteTarget(
            daily_tracking_id=int(row[0]),
            event_id=int(row[1]),
            stock_pick_id=int(row[2]),
            stock_code=str(row[3]),
            trading_day=str(row[4]),
            day_offset=int(row[5]),
        )

    @staticmethod
    def parse_kis_minute_row(
        stock_code: str, trading_day: str, row: dict[str, Any]
    ) -> MinuteRawBar | None:
        try:
            date_raw = str(row.get("stck_bsop_date") or "")
            hour_raw = str(row.get("stck_cntg_hour") or "").zfill(6)
            if len(date_raw) != 8 or len(hour_raw) != 6:
                return None

            row_day = datetime.strptime(date_raw, "%Y%m%d").strftime("%Y-%m-%d")
            if row_day != trading_day:
                return None

            ts = datetime.strptime(date_raw + hour_raw, "%Y%m%d%H%M%S")
            minute_time = ts.isoformat()

            value = _parse_optional_int(
                row.get("acml_tr_pbmn")
                or row.get("tr_pbmn")
                or row.get("stck_tr_pbmn")
            )

            return MinuteRawBar(
                stock_code=stock_code,
                trading_day=trading_day,
                minute_time=minute_time,
                open=float(row.get("stck_oprc") or 0),
                high=float(row.get("stck_hgpr") or 0),
                low=float(row.get("stck_lwpr") or 0),
                close=float(row.get("stck_prpr") or 0),
                volume=int(row.get("cntg_vol") or 0),
                value=value,
            )
        except (TypeError, ValueError):
            return None

    async def fetch_minute_raw_for_day(
        self,
        stock_code: str,
        trading_day: str,
        *,
        end_hhmmss: str = "153000",
        max_pages: int = 20,
        sleep_sec: float = 0.0,
    ) -> list[MinuteRawBar]:
        collected: dict[str, MinuteRawBar] = {}
        hhmmss = end_hhmmss

        for _ in range(max_pages):
            rows = await self.kis_client.get_minute_candles_at(
                stock_code, hhmmss, past_data=True
            )
            if not rows:
                break

            oldest_dt: datetime | None = None
            new_count = 0

            for row in rows:
                bar = self.parse_kis_minute_row(stock_code, trading_day, row)
                if bar is None:
                    continue
                if bar.minute_time in collected:
                    continue
                collected[bar.minute_time] = bar
                new_count += 1

                try:
                    bar_dt = datetime.fromisoformat(bar.minute_time)
                except ValueError:
                    continue
                if oldest_dt is None or bar_dt < oldest_dt:
                    oldest_dt = bar_dt

            if new_count == 0 or oldest_dt is None:
                logger.warning(
                    "[minute_raw] no usable bars in page stock_code={} trading_day={} hhmmss={} rows={}",
                    stock_code,
                    trading_day,
                    hhmmss,
                    len(rows),
                )
                break

            next_dt = oldest_dt - timedelta(seconds=1)
            if next_dt.hour < 9:
                break
            hhmmss = next_dt.strftime("%H%M%S")
            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)

        return sorted(collected.values(), key=lambda b: b.minute_time)

    async def _replace_bars(self, target: MinuteTarget, bars: list[MinuteRawBar]) -> int:
        now_iso = to_db_iso(now_kst())
        params = [
            (
                target.daily_tracking_id,
                target.event_id,
                target.stock_pick_id,
                target.stock_code,
                target.trading_day,
                target.day_offset,
                bar.minute_time,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.value,
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
                    "DELETE FROM pick_minute_raw WHERE daily_tracking_id = ?",
                    (target.daily_tracking_id,),
                )
                cursor = await db.executemany(
                    """
                    INSERT INTO pick_minute_raw
                        (daily_tracking_id, event_id, stock_pick_id, stock_code,
                         trading_day, day_offset, minute_time,
                         open, high, low, close, volume, value, source,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise
        return cursor.rowcount or 0

    async def collect_d0_for_tracking_row(
        self, daily_tracking_id: int
    ) -> MinuteCollectResult:
        try:
            target = await self._get_target(daily_tracking_id)
            if target is None:
                return MinuteCollectResult.SKIPPED_NO_TARGET

            bars = await self.fetch_minute_raw_for_day(
                target.stock_code, target.trading_day
            )
            if not bars:
                return MinuteCollectResult.SKIPPED_NO_BARS

            await self._replace_bars(target, bars)
            return MinuteCollectResult.SUCCESS
        except Exception as exc:
            logger.warning(
                "[minute_raw] collect failed daily_tracking_id={} error={}",
                daily_tracking_id,
                exc,
            )
            return MinuteCollectResult.FAILED

    async def collect_d0_all(
        self, trading_day: str | None = None
    ) -> dict[str, int]:
        targets = await self.list_d0_targets(trading_day=trading_day)
        counts = {result.value: 0 for result in MinuteCollectResult}
        for target in targets:
            result = await self.collect_d0_for_tracking_row(target.daily_tracking_id)
            counts[result.value] += 1
        return counts
