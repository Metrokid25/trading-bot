from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.breakout_marker import (
    CONFIRMED_BREAKOUT,
    EARLY_BREAKOUT,
    BreakoutMarker,
    BreakoutMarkResult,
    BreakoutRuleConfig,
)
from scripts.migrations import m007_phase25_minute_raw_rebuild as m007
from scripts.migrations import m008_phase25_minute_agg as m008
from scripts.migrations import m009_phase25_breakout_marks as m009


BASE_DDL = [
    """
    CREATE TABLE sector_picks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_date  TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        expires_at TEXT    NOT NULL,
        status     TEXT    NOT NULL
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
        created_at    TEXT    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending',
        retry_count   INTEGER NOT NULL DEFAULT 0,
        event_id      INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
        UNIQUE(event_id, stock_pick_id, trading_day)
    )
    """,
]

LEGACY_MINUTE_DDL = """
CREATE TABLE pick_minute_raw (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_pick_id      INTEGER NOT NULL REFERENCES sector_stocks(id),
    trading_day        TEXT    NOT NULL,
    bar_time           TEXT    NOT NULL,
    minute_idx         INTEGER NOT NULL,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    volume             INTEGER,
    transaction_amount INTEGER,
    created_at         TEXT    NOT NULL,
    UNIQUE(stock_pick_id, trading_day, minute_idx)
)
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "breakout_marker.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    m008.up(conn)
    m009.up(conn)
    conn.commit()
    conn.close()
    return path


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_pick(conn: sqlite3.Connection, stock_code: str = "005930") -> int:
    cur = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES ('2026-05-06', '2026-05-06T09:00:00',"
        " '2026-05-30T09:00:00', 'active')"
    )
    pick_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO sector_stocks"
        " (pick_id, sector_name, stock_code, stock_name, added_order, tracking_status)"
        " VALUES (?, 'semi', ?, 'Samsung', 1, 'active')",
        (pick_id, stock_code),
    )
    return int(cur.lastrowid)


def _seed_event(conn: sqlite3.Connection, pick_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, 'semi', '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id,),
    )
    return int(cur.lastrowid)


def _seed_daily(
    conn: sqlite3.Connection,
    stock_pick_id: int,
    event_id: int,
    *,
    trading_day: str = "2026-05-06",
) -> int:
    cur = conn.execute(
        "INSERT INTO pick_daily_tracking"
        " (stock_pick_id, trading_day, day_offset, created_at, status, event_id)"
        " VALUES (?, ?, 0, '2026-05-06T09:00:00', 'pending', ?)",
        (stock_pick_id, trading_day, event_id),
    )
    return int(cur.lastrowid)


def _seed_tracking(
    conn: sqlite3.Connection,
    *,
    stock_code: str = "005930",
    trading_day: str = "2026-05-06",
) -> tuple[int, int, int]:
    stock_pick_id = _seed_pick(conn, stock_code)
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    daily_id = _seed_daily(conn, stock_pick_id, event_id, trading_day=trading_day)
    return daily_id, event_id, stock_pick_id


def _insert_agg(
    conn: sqlite3.Connection,
    daily_id: int,
    event_id: int,
    stock_pick_id: int,
    stock_code: str,
    trading_day: str,
    interval: int,
    hhmm: str,
    *,
    open_: float | None = 100,
    close: float | None = 100,
    value: int | None = 100_000_000,
) -> int:
    start = f"{trading_day}T{hhmm}:00"
    end_minute = int(hhmm[-2:]) + interval - 1
    end = f"{trading_day}T{hhmm[:3]}{end_minute:02d}:00"
    cur = conn.execute(
        "INSERT INTO pick_minute_agg"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, bucket_start, bucket_end,"
        "  open, high, low, close, volume, value, raw_count, expected_count,"
        "  is_complete, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1000, ?,"
        "  ?, ?, 1, 'RAW_1M', '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_id,
            event_id,
            stock_pick_id,
            stock_code,
            trading_day,
            interval,
            start,
            end,
            open_,
            close,
            close,
            close,
            value,
            interval,
            interval,
        ),
    )
    return int(cur.lastrowid)


def _insert_existing_mark(
    conn: sqlite3.Connection,
    daily_id: int,
    event_id: int,
    stock_pick_id: int,
    agg_id: int,
    *,
    rule_version: str = "phase25_breakout_v1",
    bucket_start: str = "2026-05-06T09:03:00",
) -> None:
    conn.execute(
        "INSERT INTO pick_breakout_marks"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, agg_id, bucket_start, bucket_end,"
        "  breakout_type, prev_close, current_close, day_open,"
        "  prev_close_change_rate, day_open_change_rate, value, prev_value,"
        "  value_ratio, threshold_prev_change_rate,"
        "  threshold_day_open_change_rate, threshold_value,"
        "  threshold_value_ratio, rule_version, created_at, updated_at)"
        " VALUES (?, ?, ?, '005930', '2026-05-06', 0, 3, ?, ?,"
        "  '2026-05-06T09:05:00', 'EARLY_BREAKOUT', 100, 105, 100,"
        "  5, 5, 600000000, 100000000, 6, 1.5, 3.0, 500000000,"
        "  3.0, ?, '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (daily_id, event_id, stock_pick_id, agg_id, bucket_start, rule_version),
    )


def test_m009_creates_pick_breakout_marks_schema(tmp_path: Path):
    path = str(tmp_path / "migration.db")
    conn = _connect(path)
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    m008.up(conn)
    m009.up(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(pick_breakout_marks)")}
    assert {
        "daily_tracking_id",
        "event_id",
        "stock_pick_id",
        "agg_id",
        "breakout_type",
        "prev_close_change_rate",
        "threshold_value_ratio",
        "rule_version",
        "updated_at",
    }.issubset(cols)

    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(pick_breakout_marks)")
    }
    assert "idx_breakout_marks_event_day" in indexes
    assert "idx_breakout_marks_stock_day" in indexes
    assert "idx_breakout_marks_tracking_rule" in indexes
    assert "idx_breakout_marks_type_day" in indexes

    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03"
    )
    _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.close()


@pytest.mark.asyncio
async def test_early_breakout_mark_created(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=600_000_000)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(daily_id)
    assert result == BreakoutMarkResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT breakout_type, prev_close, current_close, day_open,"
        " ROUND(prev_close_change_rate, 4), day_open_change_rate, value,"
        " prev_value, value_ratio, threshold_prev_change_rate,"
        " threshold_day_open_change_rate, threshold_value, threshold_value_ratio,"
        " rule_version"
        " FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (
        EARLY_BREAKOUT,
        102.0,
        105.0,
        100.0,
        2.9412,
        5.0,
        600_000_000,
        100_000_000,
        6.0,
        1.5,
        3.0,
        500_000_000,
        3.0,
        "phase25_breakout_v1",
    )


@pytest.mark.asyncio
async def test_confirmed_breakout_mark_created(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 5, "09:00", open_=100, close=102, value=400_000_000)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 5, "09:05", open_=102, close=105, value=1_100_000_000)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(daily_id)
    assert result == BreakoutMarkResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT breakout_type, interval_minutes, value, prev_value, value_ratio,"
        " threshold_prev_change_rate, threshold_value FROM pick_breakout_marks"
        " WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (CONFIRMED_BREAKOUT, 5, 1_100_000_000, 400_000_000, 2.75, 2.0, 1_000_000_000)


@pytest.mark.asyncio
async def test_conditions_must_all_pass_and_no_breakout_clears_existing(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    first_agg = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=100_000_000)
    _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, first_agg)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(daily_id)
    assert result == BreakoutMarkResult.SKIPPED_NO_BREAKOUT

    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_no_agg_preserves_existing_mark_for_other_rule_version(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-07", 3, "09:00")
    _insert_existing_mark(
        conn, daily_id, event_id, stock_pick_id, agg_id, rule_version="existing_rule"
    )
    conn.commit()
    conn.close()

    no_agg_config = BreakoutRuleConfig(rule_version="no_agg_rule")
    result = await BreakoutMarker(db_path).mark_for_tracking_row(
        daily_id, rule_config=no_agg_config
    )
    assert result == BreakoutMarkResult.SKIPPED_NO_AGG

    conn = _connect(db_path)
    rows = conn.execute("SELECT rule_version FROM pick_breakout_marks").fetchall()
    conn.close()
    assert rows == [("existing_rule",)]


@pytest.mark.asyncio
async def test_mismatched_agg_rows_are_not_used(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    other_stock_pick_id = _seed_pick(conn, "000660")
    other_pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (other_stock_pick_id,)
    ).fetchone()[0]
    other_event_id = _seed_event(conn, other_pick_id)

    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-07", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_agg(conn, daily_id, other_event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=110, value=1_000_000_000)
    _insert_agg(conn, daily_id, event_id, other_stock_pick_id, "005930", "2026-05-06", 3, "09:06", open_=110, close=120, value=2_000_000_000)
    valid_first_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:09", open_=100, close=102, value=100_000_000)
    valid_second_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:12", open_=102, close=105, value=600_000_000)
    assert valid_first_id != valid_second_id
    conn.commit()
    conn.close()

    assert await BreakoutMarker(db_path).mark_for_tracking_row(daily_id) == BreakoutMarkResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT agg_id, bucket_start FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [(valid_second_id, "2026-05-06T09:12:00")]


@pytest.mark.asyncio
async def test_only_mismatched_agg_rows_return_no_agg_and_preserve_existing_mark(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    other_stock_pick_id = _seed_pick(conn, "000660")
    other_pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (other_stock_pick_id,)
    ).fetchone()[0]
    other_event_id = _seed_event(conn, other_pick_id)

    agg_id = _insert_agg(conn, daily_id, other_event_id, other_stock_pick_id, "005930", "2026-05-07", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(daily_id)
    assert result == BreakoutMarkResult.SKIPPED_NO_AGG

    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_exception_preserves_existing_mark(db_path: str, monkeypatch: pytest.MonkeyPatch):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=600_000_000)
    _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    marker = BreakoutMarker(db_path)

    async def fail_replace(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(marker, "_replace_marks", fail_replace)
    assert await marker.mark_for_tracking_row(daily_id) == BreakoutMarkResult.FAILED

    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rule_config",
    [
        BreakoutRuleConfig(rule_version=""),
        BreakoutRuleConfig(early_interval_minutes=5, confirmed_interval_minutes=5),
        BreakoutRuleConfig(early_interval_minutes=1),
        BreakoutRuleConfig(confirmed_interval_minutes=10),
        BreakoutRuleConfig(early_prev_close_change_rate=0),
        BreakoutRuleConfig(early_day_open_change_rate=-1),
        BreakoutRuleConfig(early_value=0),
        BreakoutRuleConfig(early_value_ratio=-1),
        BreakoutRuleConfig(confirmed_prev_close_change_rate=0),
        BreakoutRuleConfig(confirmed_day_open_change_rate=-1),
        BreakoutRuleConfig(confirmed_value=0),
        BreakoutRuleConfig(confirmed_value_ratio=-1),
    ],
)
async def test_invalid_rule_config_fails_and_preserves_existing_mark(
    db_path: str, rule_config: BreakoutRuleConfig
):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
    _insert_existing_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(
        daily_id, rule_config=rule_config
    )
    assert result == BreakoutMarkResult.FAILED

    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_mark_all_invalid_rule_config_returns_failed_count(db_path: str):
    counts = await BreakoutMarker(db_path).mark_all_d0(
        rule_config=BreakoutRuleConfig(early_value=0)
    )

    assert counts[BreakoutMarkResult.FAILED.value] == 1
    assert counts[BreakoutMarkResult.SUCCESS.value] == 0
    assert counts[BreakoutMarkResult.SKIPPED_NO_TARGET.value] == 0
    assert counts[BreakoutMarkResult.SKIPPED_NO_AGG.value] == 0
    assert counts[BreakoutMarkResult.SKIPPED_NO_BREAKOUT.value] == 0


@pytest.mark.asyncio
async def test_delete_and_replace_relabeling(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
    second_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=600_000_000)
    conn.commit()
    conn.close()

    marker = BreakoutMarker(db_path)
    assert await marker.mark_for_tracking_row(daily_id) == BreakoutMarkResult.SUCCESS

    conn = _connect(db_path)
    conn.execute("UPDATE pick_minute_agg SET close = 102, value = 100000000 WHERE id = ?", (second_id,))
    conn.commit()
    conn.close()

    assert await marker.mark_for_tracking_row(daily_id) == BreakoutMarkResult.SKIPPED_NO_BREAKOUT
    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_multi_event_same_stock_isolated(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute("SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)).fetchone()[0]
    event_a = _seed_event(conn, pick_id)
    event_b = _seed_event(conn, pick_id)
    daily_a = _seed_daily(conn, stock_pick_id, event_a)
    daily_b = _seed_daily(conn, stock_pick_id, event_b)
    for daily_id, event_id in ((daily_a, event_a), (daily_b, event_b)):
        _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=100, close=102, value=100_000_000)
        _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=600_000_000)
    conn.commit()
    conn.close()

    marker = BreakoutMarker(db_path)
    assert await marker.mark_for_tracking_row(daily_a) == BreakoutMarkResult.SUCCESS
    assert await marker.mark_for_tracking_row(daily_b) == BreakoutMarkResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, event_id FROM pick_breakout_marks ORDER BY daily_tracking_id"
    ).fetchall()
    conn.close()
    assert rows == [(daily_a, event_a), (daily_b, event_b)]


@pytest.mark.asyncio
async def test_trading_day_filter_limits_mark_all(db_path: str):
    conn = _connect(db_path)
    day_1, event_1, pick_1 = _seed_tracking(conn, trading_day="2026-05-06")
    day_2, event_2, pick_2 = _seed_tracking(conn, trading_day="2026-05-07")
    for daily_id, event_id, pick_id, trading_day in (
        (day_1, event_1, pick_1, "2026-05-06"),
        (day_2, event_2, pick_2, "2026-05-07"),
    ):
        _insert_agg(conn, daily_id, event_id, pick_id, "005930", trading_day, 3, "09:00", open_=100, close=102, value=100_000_000)
        _insert_agg(conn, daily_id, event_id, pick_id, "005930", trading_day, 3, "09:03", open_=102, close=105, value=600_000_000)
    conn.commit()
    conn.close()

    counts = await BreakoutMarker(db_path).mark_all_d0(trading_day="2026-05-07")
    assert counts[BreakoutMarkResult.SUCCESS.value] == 1

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, trading_day FROM pick_breakout_marks"
    ).fetchall()
    conn.close()
    assert rows == [(day_2, "2026-05-07")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prev_close", "day_open", "prev_value"),
    [(None, 100, 100_000_000), (0, 100, 100_000_000), (102, None, 100_000_000), (102, 0, 100_000_000), (102, 100, None), (102, 100, 0)],
)
async def test_null_or_zero_inputs_do_not_break_and_do_not_mark(
    db_path: str,
    prev_close: float | None,
    day_open: float | None,
    prev_value: int | None,
):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:00", open_=day_open, close=prev_close, value=prev_value)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", 3, "09:03", open_=102, close=105, value=600_000_000)
    conn.commit()
    conn.close()

    result = await BreakoutMarker(db_path).mark_for_tracking_row(daily_id)
    assert result == BreakoutMarkResult.SKIPPED_NO_BREAKOUT

    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pick_breakout_marks WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 0
