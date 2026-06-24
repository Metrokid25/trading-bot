"""Label breakout candidates from stored 3-minute and 5-minute aggregate bars."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiosqlite
from loguru import logger

from core.time_utils import now_kst, to_db_iso

EARLY_BREAKOUT = "EARLY_BREAKOUT"
CONFIRMED_BREAKOUT = "CONFIRMED_BREAKOUT"


class BreakoutMarkResult(Enum):
    SUCCESS = "success"
    SKIPPED_NO_TARGET = "skipped_no_target"
    SKIPPED_NO_AGG = "skipped_no_agg"
    SKIPPED_NO_BREAKOUT = "skipped_no_breakout"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BreakoutRuleConfig:
    rule_version: str = "phase25_breakout_v1"
    early_interval_minutes: int = 3
    early_prev_close_change_rate: float = 1.5
    early_day_open_change_rate: float = 3.0
    early_value: int = 500_000_000
    early_value_ratio: float = 3.0
    confirmed_interval_minutes: int = 5
    confirmed_prev_close_change_rate: float = 2.0
    confirmed_day_open_change_rate: float = 3.0
    confirmed_value: int = 1_000_000_000
    confirmed_value_ratio: float = 2.5


@dataclass(frozen=True, slots=True)
class BreakoutTarget:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int


@dataclass(frozen=True, slots=True)
class BreakoutAggBar:
    id: int
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int
    interval_minutes: int
    bucket_start: str
    bucket_end: str
    open: float | None
    close: float | None
    value: int | None


@dataclass(frozen=True, slots=True)
class BreakoutMark:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int
    interval_minutes: int
    agg_id: int
    bucket_start: str
    bucket_end: str
    breakout_type: str
    prev_close: float | None
    current_close: float | None
    day_open: float | None
    prev_close_change_rate: float | None
    day_open_change_rate: float | None
    value: int | None
    prev_value: int | None
    value_ratio: float | None
    threshold_prev_change_rate: float
    threshold_day_open_change_rate: float
    threshold_value: int
    threshold_value_ratio: float
    rule_version: str


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _change_rate(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base == 0:
        return None
    return ((current - base) / base) * 100


def _value_ratio(value: int | None, prev_value: int | None) -> float | None:
    if value is None or prev_value is None or prev_value == 0:
        return None
    return value / prev_value


def _validate_rule_config(config: BreakoutRuleConfig) -> None:
    if not config.rule_version:
        raise ValueError("rule_version must not be empty")
    if config.early_interval_minutes != 3:
        raise ValueError("early_interval_minutes must be 3")
    if config.confirmed_interval_minutes != 5:
        raise ValueError("confirmed_interval_minutes must be 5")
    if config.early_interval_minutes == config.confirmed_interval_minutes:
        raise ValueError("early and confirmed intervals must differ")

    thresholds = (
        config.early_prev_close_change_rate,
        config.early_day_open_change_rate,
        config.early_value,
        config.early_value_ratio,
        config.confirmed_prev_close_change_rate,
        config.confirmed_day_open_change_rate,
        config.confirmed_value,
        config.confirmed_value_ratio,
    )
    if any(threshold <= 0 for threshold in thresholds):
        raise ValueError("all breakout thresholds must be positive")


class BreakoutMarker:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @staticmethod
    def _config(rule_config: BreakoutRuleConfig | None) -> BreakoutRuleConfig:
        return rule_config or BreakoutRuleConfig()

    async def list_d0_targets(
        self,
        trading_day: str | None = None,
        rule_config: BreakoutRuleConfig | None = None,
    ) -> list[BreakoutTarget]:
        config = self._config(rule_config)
        params: list[Any] = [
            config.early_interval_minutes,
            config.confirmed_interval_minutes,
        ]
        trading_day_filter = ""
        if trading_day is not None:
            trading_day_filter = " AND pdt.trading_day = ?"
            params.append(trading_day)

        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                f"""
                SELECT DISTINCT
                    pdt.id,
                    pdt.event_id,
                    pdt.stock_pick_id,
                    ss.stock_code,
                    pdt.trading_day,
                    pdt.day_offset
                FROM pick_daily_tracking pdt
                JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
                JOIN pick_minute_agg pma ON pma.daily_tracking_id = pdt.id
                WHERE pdt.day_offset = 0
                  AND pma.interval_minutes IN (?, ?)
                  {trading_day_filter}
                ORDER BY pdt.event_id, pdt.stock_pick_id
                """,
                tuple(params),
            )
            rows = await cur.fetchall()

        return [
            BreakoutTarget(
                daily_tracking_id=int(row[0]),
                event_id=int(row[1]),
                stock_pick_id=int(row[2]),
                stock_code=str(row[3]),
                trading_day=str(row[4]),
                day_offset=int(row[5]),
            )
            for row in rows
        ]

    async def _get_target(self, daily_tracking_id: int) -> BreakoutTarget | None:
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
        return BreakoutTarget(
            daily_tracking_id=int(row[0]),
            event_id=int(row[1]),
            stock_pick_id=int(row[2]),
            stock_code=str(row[3]),
            trading_day=str(row[4]),
            day_offset=int(row[5]),
        )

    async def _load_agg_bars(
        self, target: BreakoutTarget, config: BreakoutRuleConfig
    ) -> list[BreakoutAggBar]:
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                """
                SELECT
                    id, daily_tracking_id, event_id, stock_pick_id, stock_code,
                    trading_day, day_offset, interval_minutes, bucket_start,
                    bucket_end, open, close, value
                FROM pick_minute_agg
                WHERE daily_tracking_id = ?
                  AND trading_day = ?
                  AND event_id = ?
                  AND stock_pick_id = ?
                  AND interval_minutes IN (?, ?)
                ORDER BY interval_minutes, bucket_start, id
                """,
                (
                    target.daily_tracking_id,
                    target.trading_day,
                    target.event_id,
                    target.stock_pick_id,
                    config.early_interval_minutes,
                    config.confirmed_interval_minutes,
                ),
            )
            rows = await cur.fetchall()

        return [
            BreakoutAggBar(
                id=int(row[0]),
                daily_tracking_id=int(row[1]),
                event_id=int(row[2]),
                stock_pick_id=int(row[3]),
                stock_code=str(row[4]),
                trading_day=str(row[5]),
                day_offset=int(row[6]),
                interval_minutes=int(row[7]),
                bucket_start=str(row[8]),
                bucket_end=str(row[9]),
                open=_optional_float(row[10]),
                close=_optional_float(row[11]),
                value=_optional_int(row[12]),
            )
            for row in rows
        ]

    @staticmethod
    def _day_open(bars: list[BreakoutAggBar]) -> float | None:
        ordered = sorted(bars, key=lambda bar: (bar.bucket_start, bar.interval_minutes, bar.id))
        if not ordered:
            return None
        return ordered[0].open

    def build_marks(
        self, bars: list[BreakoutAggBar], config: BreakoutRuleConfig
    ) -> list[BreakoutMark]:
        day_open = self._day_open(bars)
        marks: list[BreakoutMark] = []

        for interval in (config.early_interval_minutes, config.confirmed_interval_minutes):
            interval_bars = sorted(
                [bar for bar in bars if bar.interval_minutes == interval],
                key=lambda bar: (bar.bucket_start, bar.id),
            )
            prev: BreakoutAggBar | None = None
            for bar in interval_bars:
                if prev is None:
                    prev = bar
                    continue

                prev_change = _change_rate(bar.close, prev.close)
                day_open_change = _change_rate(bar.close, day_open)
                ratio = _value_ratio(bar.value, prev.value)

                if interval == config.early_interval_minutes:
                    breakout_type = EARLY_BREAKOUT
                    threshold_prev = config.early_prev_close_change_rate
                    threshold_day = config.early_day_open_change_rate
                    threshold_value = config.early_value
                    threshold_ratio = config.early_value_ratio
                else:
                    breakout_type = CONFIRMED_BREAKOUT
                    threshold_prev = config.confirmed_prev_close_change_rate
                    threshold_day = config.confirmed_day_open_change_rate
                    threshold_value = config.confirmed_value
                    threshold_ratio = config.confirmed_value_ratio

                passes = (
                    prev_change is not None
                    and day_open_change is not None
                    and bar.value is not None
                    and ratio is not None
                    and prev_change >= threshold_prev
                    and day_open_change >= threshold_day
                    and bar.value >= threshold_value
                    and ratio >= threshold_ratio
                )
                if passes:
                    marks.append(
                        BreakoutMark(
                            daily_tracking_id=bar.daily_tracking_id,
                            event_id=bar.event_id,
                            stock_pick_id=bar.stock_pick_id,
                            stock_code=bar.stock_code,
                            trading_day=bar.trading_day,
                            day_offset=bar.day_offset,
                            interval_minutes=bar.interval_minutes,
                            agg_id=bar.id,
                            bucket_start=bar.bucket_start,
                            bucket_end=bar.bucket_end,
                            breakout_type=breakout_type,
                            prev_close=prev.close,
                            current_close=bar.close,
                            day_open=day_open,
                            prev_close_change_rate=prev_change,
                            day_open_change_rate=day_open_change,
                            value=bar.value,
                            prev_value=prev.value,
                            value_ratio=ratio,
                            threshold_prev_change_rate=threshold_prev,
                            threshold_day_open_change_rate=threshold_day,
                            threshold_value=threshold_value,
                            threshold_value_ratio=threshold_ratio,
                            rule_version=config.rule_version,
                        )
                    )
                prev = bar
        return marks

    async def _replace_marks(
        self,
        daily_tracking_id: int,
        rule_version: str,
        marks: list[BreakoutMark],
    ) -> int:
        now_iso = to_db_iso(now_kst())
        params = [
            (
                mark.daily_tracking_id,
                mark.event_id,
                mark.stock_pick_id,
                mark.stock_code,
                mark.trading_day,
                mark.day_offset,
                mark.interval_minutes,
                mark.agg_id,
                mark.bucket_start,
                mark.bucket_end,
                mark.breakout_type,
                mark.prev_close,
                mark.current_close,
                mark.day_open,
                mark.prev_close_change_rate,
                mark.day_open_change_rate,
                mark.value,
                mark.prev_value,
                mark.value_ratio,
                mark.threshold_prev_change_rate,
                mark.threshold_day_open_change_rate,
                mark.threshold_value,
                mark.threshold_value_ratio,
                mark.rule_version,
                now_iso,
                now_iso,
            )
            for mark in marks
        ]

        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "DELETE FROM pick_breakout_marks"
                    " WHERE daily_tracking_id = ? AND rule_version = ?",
                    (daily_tracking_id, rule_version),
                )
                cursor = await db.executemany(
                    """
                    INSERT INTO pick_breakout_marks
                        (daily_tracking_id, event_id, stock_pick_id, stock_code,
                         trading_day, day_offset, interval_minutes, agg_id,
                         bucket_start, bucket_end, breakout_type,
                         prev_close, current_close, day_open,
                         prev_close_change_rate, day_open_change_rate,
                         value, prev_value, value_ratio,
                         threshold_prev_change_rate,
                         threshold_day_open_change_rate, threshold_value,
                         threshold_value_ratio, rule_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise
        return cursor.rowcount if marks else 0

    async def mark_for_tracking_row(
        self,
        daily_tracking_id: int,
        rule_config: BreakoutRuleConfig | None = None,
    ) -> BreakoutMarkResult:
        config = self._config(rule_config)
        try:
            _validate_rule_config(config)
            target = await self._get_target(daily_tracking_id)
            if target is None:
                return BreakoutMarkResult.SKIPPED_NO_TARGET

            bars = await self._load_agg_bars(target, config)
            if not bars:
                return BreakoutMarkResult.SKIPPED_NO_AGG

            marks = self.build_marks(bars, config)
            await self._replace_marks(daily_tracking_id, config.rule_version, marks)
            if not marks:
                return BreakoutMarkResult.SKIPPED_NO_BREAKOUT
            return BreakoutMarkResult.SUCCESS
        except Exception as exc:
            logger.warning(
                "[breakout_marker] mark failed daily_tracking_id={} error={}",
                daily_tracking_id,
                exc,
            )
            return BreakoutMarkResult.FAILED

    async def mark_all_d0(
        self,
        trading_day: str | None = None,
        rule_config: BreakoutRuleConfig | None = None,
    ) -> dict[str, int]:
        config = self._config(rule_config)
        counts = {result.value: 0 for result in BreakoutMarkResult}
        try:
            _validate_rule_config(config)
        except ValueError as exc:
            logger.warning("[breakout_marker] invalid rule_config error={}", exc)
            counts[BreakoutMarkResult.FAILED.value] += 1
            return counts

        targets = await self.list_d0_targets(
            trading_day=trading_day, rule_config=config
        )
        for target in targets:
            result = await self.mark_for_tracking_row(
                target.daily_tracking_id, rule_config=config
            )
            counts[result.value] += 1
        return counts
