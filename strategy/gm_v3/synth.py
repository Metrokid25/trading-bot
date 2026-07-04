"""합성 OHLCV 생성 유틸 — 실데이터 부족 구간 대체 + 테스트 픽스처.

TODO(real-data): 백테스트 러너에서 이 모듈을 쓰는 구간은 전부 '데이터 부족
대체'다. 노트북 축적 DB(pick_daily_tracking/pick_minute_raw) 동기화 또는
KIS 일봉 보충이 되면 실데이터로 교체할 것. 리포트에 사용 구간이 명시된다.
"""
from __future__ import annotations

import random
from datetime import date as Date, timedelta

from strategy.gm_v3.models import DailyBar


def next_trading_day(d: Date) -> Date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def make_random_walk(seed: int, n: int, *, start: Date = Date(2026, 1, 5),
                     base_price: float = 10_000.0,
                     base_volume: float = 100_000.0,
                     daily_vol: float = 0.02) -> list[DailyBar]:
    """시드 고정 랜덤워크 일봉 n개 (재현 가능)."""
    rng = random.Random(seed)
    bars: list[DailyBar] = []
    d, close = start, base_price
    for _ in range(n):
        o = close * (1 + rng.uniform(-daily_vol / 2, daily_vol / 2))
        c = o * (1 + rng.uniform(-daily_vol, daily_vol))
        hi = max(o, c) * (1 + rng.uniform(0, daily_vol / 2))
        lo = min(o, c) * (1 - rng.uniform(0, daily_vol / 2))
        v = base_volume * rng.uniform(0.5, 2.0)
        bars.append(DailyBar(d, round(o), round(hi), round(lo), round(c),
                             round(v)))
        close = c
        d = next_trading_day(d)
    return bars


def make_bars(rows: list[tuple], *, start: Date = Date(2026, 1, 5)
              ) -> list[DailyBar]:
    """명시 시나리오 빌더: rows = [(o, h, l, c, v), ...] → 연속 거래일 일봉."""
    bars: list[DailyBar] = []
    d = start
    for o, h, l, c, v in rows:
        bars.append(DailyBar(d, o, h, l, c, v))
        d = next_trading_day(d)
    return bars
