"""daily_collection_scheduler (D4) 단위 테스트.

tmp_path 기반 파일 SQLite DB. DailyTracker.collect_daily는 AsyncMock.
APScheduler 실제 동작은 테스트하지 않음.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.daily_collection_scheduler import run_daily_collection
from core.daily_tracker import DailyTracker


# ---------------------------------------------------------------------------
# 스키마 DDL (최소 필요 테이블만)
# ---------------------------------------------------------------------------

_DDL_STATEMENTS = [
    """
    CREATE TABLE sector_picks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_date  TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE sector_stocks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id         INTEGER NOT NULL REFERENCES sector_picks(id),
        sector_name     TEXT    NOT NULL,
        stock_code      TEXT    NOT NULL,
        stock_name      TEXT    NOT NULL,
        added_order     INTEGER NOT NULL,
        tracking_status TEXT    NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE sector_pick_events (
        event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id           INTEGER NOT NULL REFERENCES sector_picks(id),
        sector_name       TEXT    NOT NULL,
        registered_at_kst TEXT    NOT NULL,
        pick_date         TEXT
    )
    """,
    """
    CREATE TABLE pick_daily_tracking (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_pick_id INTEGER NOT NULL REFERENCES sector_stocks(id),
        trading_day   TEXT    NOT NULL,
        day_offset    INTEGER NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending',
        retry_count   INTEGER NOT NULL DEFAULT 0,
        event_id      INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
        created_at    TEXT    NOT NULL,
        UNIQUE(event_id, stock_pick_id, trading_day)
    )
    """,
]

_TODAY = date(2026, 5, 10)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "sched_test.db")
    conn = sqlite3.connect(path)
    for stmt in _DDL_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return path


def _make_tracker(db_path: str) -> DailyTracker:
    client = MagicMock()
    client.get_daily_candles = AsyncMock(return_value=[])
    tracker = DailyTracker(db_path, client)
    tracker.collect_daily = AsyncMock(return_value=True)
    return tracker


def _seed(db_path: str, rows: list[tuple]) -> tuple[int, int]:
    """sector_picks + sector_stocks + sector_pick_events + pick_daily_tracking 삽입.

    rows: [(stock_code, trading_day, status), ...]
    반환: (stock_pick_id, event_id) (단일 픽/이벤트 공유)
    """
    conn = sqlite3.connect(db_path)
    sp = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES ('2026-05-06', '2026-05-01T09:00:00', '2026-05-27T09:00:00', 'active')"
    )
    pick_id = sp.lastrowid

    sst = conn.execute(
        "INSERT INTO sector_stocks"
        " (pick_id, sector_name, stock_code, stock_name, added_order)"
        " VALUES (?, '반도체', '005930', '삼성전자', 1)",
        (pick_id,),
    )
    stock_pick_id = sst.lastrowid

    ev = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, '반도체', '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id,),
    )
    event_id = ev.lastrowid

    for _, trading_day, status in rows:
        conn.execute(
            "INSERT INTO pick_daily_tracking"
            " (stock_pick_id, trading_day, day_offset, status, event_id, created_at)"
            " VALUES (?, ?, 0, ?, ?, '2026-05-06T09:00:00')",
            (stock_pick_id, trading_day, status, event_id),
        )

    conn.commit()
    conn.close()
    return stock_pick_id, event_id


def _seed_multi_stocks(db_path: str, codes_and_rows: list[tuple]) -> list[tuple[str, int]]:
    """다수 종목으로 seed. codes_and_rows: [(stock_code, trading_day, status), ...]
    반환: [(ticker, event_id), ...] 의 고유 목록"""
    conn = sqlite3.connect(db_path)
    sp = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES ('2026-05-06', '2026-05-01T09:00:00', '2026-05-27T09:00:00', 'active')"
    )
    pick_id = sp.lastrowid
    ev = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, '반도체', '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id,),
    )
    event_id = ev.lastrowid

    seen: dict[str, int] = {}
    for stock_code, trading_day, status in codes_and_rows:
        if stock_code not in seen:
            sst = conn.execute(
                "INSERT INTO sector_stocks"
                " (pick_id, sector_name, stock_code, stock_name, added_order)"
                " VALUES (?, '반도체', ?, ?, ?)",
                (pick_id, stock_code, stock_code, len(seen) + 1),
            )
            seen[stock_code] = sst.lastrowid
        conn.execute(
            "INSERT INTO pick_daily_tracking"
            " (stock_pick_id, trading_day, day_offset, status, event_id, created_at)"
            " VALUES (?, ?, 0, ?, ?, '2026-05-06T09:00:00')",
            (seen[stock_code], trading_day, status, event_id),
        )
    conn.commit()
    conn.close()
    return [(code, event_id) for code in seen]


# ---------------------------------------------------------------------------
# TC1: 수집 대상 쿼리 — trading_day <= today AND status='pending'만 반환
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_filters_by_date_and_status(db_path):
    """TC1: 오늘 이하의 pending 행만 collect_daily 호출 대상이 됨."""
    _seed(db_path, [
        ("005930", "2026-05-08", "pending"),   # 과거, pending → 대상
        ("005930", "2026-05-10", "pending"),   # 오늘, pending → 대상
        ("005930", "2026-05-11", "pending"),   # 미래 → 제외
    ])

    tracker = _make_tracker(db_path)
    await run_daily_collection(tracker, today=_TODAY)

    calls = tracker.collect_daily.call_args_list
    called_dates = {str(call.args[2]) for call in calls}
    assert called_dates == {"2026-05-08", "2026-05-10"}
    assert "2026-05-11" not in called_dates


# ---------------------------------------------------------------------------
# TC2: job 함수가 대상 행에 대해 collect_daily를 직렬 호출
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_called_for_each_target(db_path):
    """TC2: 수집 대상 2행 모두 collect_daily 호출됨 (event_id, ticker, date 일치)."""
    _seed(db_path, [
        ("005930", "2026-05-08", "pending"),
        ("005930", "2026-05-09", "pending"),
    ])

    tracker = _make_tracker(db_path)
    await run_daily_collection(tracker, today=_TODAY)

    assert tracker.collect_daily.call_count == 2
    called_days = {str(call.args[2]) for call in tracker.collect_daily.call_args_list}
    assert called_days == {"2026-05-08", "2026-05-09"}


# ---------------------------------------------------------------------------
# TC3: collect_daily 한 건 실패해도 다음 종목으로 계속 진행
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_failure_does_not_stop_others(db_path):
    """TC3: 두 번째 종목 collect_daily에서 예외가 나도 세 번째는 정상 호출됨."""
    _seed_multi_stocks(db_path, [
        ("005930", "2026-05-08", "pending"),
        ("000660", "2026-05-08", "pending"),
        ("042700", "2026-05-08", "pending"),
    ])

    tracker = _make_tracker(db_path)
    call_count = 0

    async def side_effect(event_id, ticker, target_date):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("KIS 오류 시뮬레이션")
        return True

    tracker.collect_daily = AsyncMock(side_effect=side_effect)

    await run_daily_collection(tracker, today=_TODAY)

    assert tracker.collect_daily.call_count == 3


# ---------------------------------------------------------------------------
# TC4: 미래 trading_day 행은 절대 호출 안 됨
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_future_trading_day_never_collected(db_path):
    """TC4: today 이후 날짜 행은 status=pending이어도 호출 대상에서 제외됨."""
    _seed(db_path, [
        ("005930", "2026-05-11", "pending"),  # 미래
        ("005930", "2026-05-12", "pending"),  # 미래
        ("005930", "2026-05-20", "pending"),  # 미래
    ])

    tracker = _make_tracker(db_path)
    await run_daily_collection(tracker, today=_TODAY)

    assert tracker.collect_daily.call_count == 0


# ---------------------------------------------------------------------------
# TC5: status != 'pending' 행은 호출 대상에서 제외
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_pending_statuses_excluded(db_path):
    """TC5: success / failed_temp / failed_permanent 행은 수집 대상에서 제외."""
    _seed_multi_stocks(db_path, [
        ("005930", "2026-05-08", "success"),
        ("000660", "2026-05-08", "failed_temp"),
        ("042700", "2026-05-08", "failed_permanent"),
        ("058470", "2026-05-08", "pending"),   # 이것만 대상
    ])

    tracker = _make_tracker(db_path)
    await run_daily_collection(tracker, today=_TODAY)

    assert tracker.collect_daily.call_count == 1
    called_ticker = tracker.collect_daily.call_args.args[1]
    assert called_ticker == "058470"
