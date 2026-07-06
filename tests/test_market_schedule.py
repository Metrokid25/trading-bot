"""core/market_schedule.py — 타임테이블 경계(이상~미만)·중단 대기 검증."""
from datetime import datetime

import pytest

from core.market_schedule import next_action


def _at(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 7, 6, h, m, s)  # 월요일 (tz 불필요 — 시각만 판정)


@pytest.mark.parametrize("hh,mm,interval,label", [
    (6, 0, 1800, "dawn"),        # 06:00 이상 — dawn 시작
    (6, 59, 1800, "dawn"),
    (7, 0, 600, "pre-open"),     # 07:00 경계 — pre-open (이상~미만)
    (7, 59, 600, "pre-open"),
    (8, 0, 300, "market"),       # 08:00 경계 — market
    (12, 30, 300, "market"),
    (15, 59, 300, "market"),
    (16, 0, 600, "post-close"),  # 16:00 경계 — post-close
    (17, 59, 600, "post-close"),
    (18, 0, 1800, "evening"),    # 18:00 경계 — evening
    (22, 59, 1800, "evening"),
])
def test_active_windows(hh, mm, interval, label):
    active, wait_s, got = next_action(_at(hh, mm))
    assert active is True
    assert wait_s == float(interval)
    assert got == label


def test_halt_at_2300_waits_until_next_0600():
    active, wait_s, label = next_action(_at(23, 0))
    assert active is False
    assert label == "halt"
    assert wait_s == 7 * 3600          # 23:00 → 익일 06:00 = 7시간


def test_halt_after_midnight_waits_until_same_day_0600():
    active, wait_s, label = next_action(_at(0, 30))
    assert active is False
    assert wait_s == 5 * 3600 + 30 * 60  # 00:30 → 06:00 = 5.5시간


def test_halt_just_before_0600():
    active, wait_s, label = next_action(_at(5, 59, 59))
    assert active is False
    assert wait_s == 1.0                # 06:00 까지 1초


def test_boundary_2259_is_still_evening():
    active, _w, label = next_action(_at(22, 59, 59))
    assert active is True and label == "evening"
