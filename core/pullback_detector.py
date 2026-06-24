"""Detect 09:20~09:40 눌림목 (pullback-hold) entries on Phase 2.5 pick pipeline.

전략 2~4단계의 라이브 구현. 입력은 `pick_minute_agg`(3분봉)와
`pick_breakout_marks`(1단계 강세 마크). 구조적 정의를 따른다:

  - 강세 게이트: 해당 종목이 당일 강세 마크(EARLY/CONFIRMED breakout)를 가진다.
  - 저점 유지: 09:20~09:40 3분봉의 저점이 계속 깨지지 않는다(허용오차 내).
  - 거래량 유지: 윈도우 내 최저 거래대금이 바닥 임계 이상으로 유지된다.
  - 반등 조짐: 윈도우 마지막 봉이 양봉(close > open)으로 마감.

백테스트의 VWAP/MACD PULLBACK(strategy/signal.py)과는 별개의, 픽 파이프라인
전용 구조적 판정이다. 임계는 PullbackRuleConfig 로 전부 튜닝 가능하다.

이 모듈은 DB에 쓰지 않는다(Phase 2.5 데이터 축적 모드). 시그널을 in-memory로
반환하고, 알림은 dry-run(로그)이 기본이다. 실제 텔레그램 발송은 승인 게이트.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import aiosqlite
from loguru import logger

from core.breakout_marker import CONFIRMED_BREAKOUT, EARLY_BREAKOUT

PULLBACK_HOLD = "PULLBACK_HOLD"


class PullbackResult(Enum):
    SUCCESS = "success"
    SKIPPED_NO_TARGET = "skipped_no_target"
    SKIPPED_NO_STRENGTH = "skipped_no_strength"
    SKIPPED_NO_WINDOW_BARS = "skipped_no_window_bars"
    SKIPPED_NO_SIGNAL = "skipped_no_signal"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PullbackRuleConfig:
    rule_version: str = "phase25_pullback_v1"
    interval_minutes: int = 3
    window_start_hhmm: str = "09:20"
    window_end_hhmm: str = "09:40"  # exclusive 상한
    require_prior_breakout: bool = True
    # 저점 유지: 윈도우 최초 저점(지지선)을 (1 - tol%) 미만으로 깨면 탈락.
    low_break_tolerance_pct: float = 0.5
    # 거래량(거래대금) 유지: 윈도우 내 최저 거래대금 바닥 임계(원).
    min_window_value: int = 100_000_000
    # 반등 조짐: 마지막 봉 양봉 마감 요구.
    require_green_close: bool = True


@dataclass(frozen=True, slots=True)
class PullbackTarget:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int


@dataclass(frozen=True, slots=True)
class PullbackAggBar:
    interval_minutes: int
    bucket_start: str
    bucket_end: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    value: int | None


@dataclass(frozen=True, slots=True)
class PullbackSignal:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    trading_day: str
    day_offset: int
    interval_minutes: int
    signal_type: str
    window_start: str
    window_end: str
    first_bar_start: str
    last_bar_start: str
    window_low: float | None
    last_close: float | None
    last_open: float | None
    min_window_value: int | None
    rule_version: str
    threshold_low_break_tolerance_pct: float
    threshold_min_window_value: int


def _validate_rule_config(config: PullbackRuleConfig) -> None:
    if not config.rule_version:
        raise ValueError("rule_version must not be empty")
    if config.interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    if config.window_start_hhmm >= config.window_end_hhmm:
        raise ValueError("window_start_hhmm must be before window_end_hhmm")
    if config.low_break_tolerance_pct < 0:
        raise ValueError("low_break_tolerance_pct must be non-negative")
    if config.min_window_value < 0:
        raise ValueError("min_window_value must be non-negative")


class PullbackDetector:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @staticmethod
    def _config(rule_config: PullbackRuleConfig | None) -> PullbackRuleConfig:
        return rule_config or PullbackRuleConfig()

    async def list_d0_targets(
        self,
        trading_day: str | None = None,
        rule_config: PullbackRuleConfig | None = None,
    ) -> list[PullbackTarget]:
        config = self._config(rule_config)
        params: list[object] = [config.interval_minutes]
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
                  AND pma.interval_minutes = ?
                  {trading_day_filter}
                ORDER BY pdt.event_id, pdt.stock_pick_id
                """,
                tuple(params),
            )
            rows = await cur.fetchall()

        return [
            PullbackTarget(
                daily_tracking_id=int(row[0]),
                event_id=int(row[1]),
                stock_pick_id=int(row[2]),
                stock_code=str(row[3]),
                trading_day=str(row[4]),
                day_offset=int(row[5]),
            )
            for row in rows
        ]

    async def _get_target(self, daily_tracking_id: int) -> PullbackTarget | None:
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
        return PullbackTarget(
            daily_tracking_id=int(row[0]),
            event_id=int(row[1]),
            stock_pick_id=int(row[2]),
            stock_code=str(row[3]),
            trading_day=str(row[4]),
            day_offset=int(row[5]),
        )

    async def _has_prior_breakout(self, target: PullbackTarget) -> bool:
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                """
                SELECT 1
                FROM pick_breakout_marks
                WHERE daily_tracking_id = ?
                  AND trading_day = ?
                  AND breakout_type IN (?, ?)
                LIMIT 1
                """,
                (
                    target.daily_tracking_id,
                    target.trading_day,
                    EARLY_BREAKOUT,
                    CONFIRMED_BREAKOUT,
                ),
            )
            return await cur.fetchone() is not None

    async def _load_window_bars(
        self, target: PullbackTarget, config: PullbackRuleConfig
    ) -> list[PullbackAggBar]:
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                """
                SELECT
                    interval_minutes, bucket_start, bucket_end,
                    open, high, low, close, volume, value
                FROM pick_minute_agg
                WHERE daily_tracking_id = ?
                  AND trading_day = ?
                  AND event_id = ?
                  AND stock_pick_id = ?
                  AND interval_minutes = ?
                  AND substr(bucket_start, 12, 5) >= ?
                  AND substr(bucket_start, 12, 5) < ?
                ORDER BY bucket_start, id
                """,
                (
                    target.daily_tracking_id,
                    target.trading_day,
                    target.event_id,
                    target.stock_pick_id,
                    config.interval_minutes,
                    config.window_start_hhmm,
                    config.window_end_hhmm,
                ),
            )
            rows = await cur.fetchall()

        return [
            PullbackAggBar(
                interval_minutes=int(row[0]),
                bucket_start=str(row[1]),
                bucket_end=str(row[2]),
                open=None if row[3] is None else float(row[3]),
                high=None if row[4] is None else float(row[4]),
                low=None if row[5] is None else float(row[5]),
                close=None if row[6] is None else float(row[6]),
                volume=None if row[7] is None else int(row[7]),
                value=None if row[8] is None else int(row[8]),
            )
            for row in rows
        ]

    def evaluate(
        self,
        target: PullbackTarget,
        bars: list[PullbackAggBar],
        config: PullbackRuleConfig,
    ) -> PullbackSignal | None:
        if not bars:
            return None

        lows = [bar.low for bar in bars if bar.low is not None]
        values = [bar.value for bar in bars if bar.value is not None]
        if len(lows) != len(bars) or len(values) != len(bars):
            # 결측 봉이 섞이면 보수적으로 판정하지 않는다.
            return None

        # 저점 유지: 윈도우 최초 저점(지지선) 기준으로 어떤 봉도 (1 - tol%) 미만으로
        # 깨면 탈락. running 기준이 아니라 first_low 절대 기준 — 연속 소폭 하락으로
        # 저점이 점진 잠식되는 경우도 탈락시키기 위함(눌림목 지지선 유지 의도).
        tol = config.low_break_tolerance_pct / 100.0
        support = lows[0] * (1.0 - tol)
        if any(low < support for low in lows[1:]):
            return None

        # 거래량(거래대금) 유지: 윈도우 최저 거래대금이 바닥 임계 이상.
        min_value = min(values)
        if min_value < config.min_window_value:
            return None

        # 반등 조짐: 마지막 봉 양봉 마감.
        last = bars[-1]
        if config.require_green_close:
            if last.close is None or last.open is None or last.close <= last.open:
                return None

        return PullbackSignal(
            daily_tracking_id=target.daily_tracking_id,
            event_id=target.event_id,
            stock_pick_id=target.stock_pick_id,
            stock_code=target.stock_code,
            trading_day=target.trading_day,
            day_offset=target.day_offset,
            interval_minutes=config.interval_minutes,
            signal_type=PULLBACK_HOLD,
            window_start=config.window_start_hhmm,
            window_end=config.window_end_hhmm,
            first_bar_start=bars[0].bucket_start,
            last_bar_start=last.bucket_start,
            window_low=min(lows),
            last_close=last.close,
            last_open=last.open,
            min_window_value=min_value,
            rule_version=config.rule_version,
            threshold_low_break_tolerance_pct=config.low_break_tolerance_pct,
            threshold_min_window_value=config.min_window_value,
        )

    async def detect_for_tracking_row(
        self,
        daily_tracking_id: int,
        rule_config: PullbackRuleConfig | None = None,
    ) -> tuple[PullbackResult, PullbackSignal | None]:
        config = self._config(rule_config)
        try:
            _validate_rule_config(config)
            target = await self._get_target(daily_tracking_id)
            if target is None:
                return PullbackResult.SKIPPED_NO_TARGET, None

            if config.require_prior_breakout and not await self._has_prior_breakout(target):
                return PullbackResult.SKIPPED_NO_STRENGTH, None

            bars = await self._load_window_bars(target, config)
            if not bars:
                return PullbackResult.SKIPPED_NO_WINDOW_BARS, None

            signal = self.evaluate(target, bars, config)
            if signal is None:
                return PullbackResult.SKIPPED_NO_SIGNAL, None
            return PullbackResult.SUCCESS, signal
        except Exception as exc:
            logger.warning(
                "[pullback] detect failed daily_tracking_id={} error={}",
                daily_tracking_id,
                exc,
            )
            return PullbackResult.FAILED, None

    async def detect_all_d0(
        self,
        trading_day: str | None = None,
        rule_config: PullbackRuleConfig | None = None,
    ) -> tuple[dict[str, int], list[PullbackSignal]]:
        config = self._config(rule_config)
        counts = {result.value: 0 for result in PullbackResult}
        signals: list[PullbackSignal] = []
        # 빠른 실패: 잘못된 config면 타겟 조회 전에 즉시 FAILED 반환. 개별
        # detect_for_tracking_row도 동일 검증을 하지만(frozen config라 무해),
        # 여기서 먼저 막아 N건의 무의미한 조회를 피한다.
        try:
            _validate_rule_config(config)
        except ValueError as exc:
            logger.warning("[pullback] invalid rule_config error={}", exc)
            counts[PullbackResult.FAILED.value] += 1
            return counts, signals

        targets = await self.list_d0_targets(trading_day=trading_day, rule_config=config)
        for target in targets:
            result, signal = await self.detect_for_tracking_row(
                target.daily_tracking_id, rule_config=config
            )
            counts[result.value] += 1
            if signal is not None:
                signals.append(signal)
        return counts, signals
