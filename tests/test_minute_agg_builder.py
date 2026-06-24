from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.minute_agg_builder import MinuteAggBuilder, MinuteAggResult, MinuteRawRow
from scripts.migrations import m007_phase25_minute_raw_rebuild as m007
from scripts.migrations import m008_phase25_minute_agg as m008


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
    path = str(tmp_path / "minute_agg.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    m008.up(conn)
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
    daily_tracking_id = _seed_daily(
        conn, stock_pick_id, event_id, trading_day=trading_day
    )
    return daily_tracking_id, event_id, stock_pick_id


def _insert_raw(
    conn: sqlite3.Connection,
    daily_tracking_id: int,
    event_id: int,
    stock_pick_id: int,
    stock_code: str,
    trading_day: str,
    hhmm: str,
    *,
    open_: float = 100,
    high: float = 110,
    low: float = 95,
    close: float = 105,
    volume: int = 10,
    value: int | None = 1000,
    minute_time: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pick_minute_raw"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, minute_time, open, high, low, close, volume, value,"
        "  source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'KIS',"
        " '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_tracking_id,
            event_id,
            stock_pick_id,
            stock_code,
            trading_day,
            minute_time or f"{trading_day}T{hhmm}:00",
            open_,
            high,
            low,
            close,
            volume,
            value,
        ),
    )


def test_m008_creates_pick_minute_agg_schema(tmp_path: Path):
    path = str(tmp_path / "migration.db")
    conn = _connect(path)
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    m008.up(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(pick_minute_agg)")}
    assert {
        "daily_tracking_id",
        "event_id",
        "stock_pick_id",
        "interval_minutes",
        "bucket_start",
        "bucket_end",
        "raw_count",
        "expected_count",
        "is_complete",
        "updated_at",
    }.issubset(cols)

    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(pick_minute_agg)")
    }
    assert "idx_pick_minute_agg_event_day" in indexes
    assert "idx_pick_minute_agg_stock_day_interval" in indexes
    assert "idx_pick_minute_agg_tracking_interval" in indexes

    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    params = (
        daily_id,
        event_id,
        stock_pick_id,
        "005930",
        "2026-05-06",
        0,
        3,
        "2026-05-06T09:00:00",
        "2026-05-06T09:02:00",
        1,
        3,
        0,
        "RAW_1M",
        "2026-05-06T16:00:00",
        "2026-05-06T16:00:00",
    )
    conn.execute(
        "INSERT INTO pick_minute_agg"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, bucket_start, bucket_end,"
        "  raw_count, expected_count, is_complete, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pick_minute_agg"
            " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
            "  day_offset, interval_minutes, bucket_start, bucket_end,"
            "  raw_count, expected_count, is_complete, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
    conn.close()


@pytest.mark.asyncio
async def test_three_minute_aggregation(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:00", open_=100, high=110, low=95, close=105, volume=10, value=1000,
    )
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:01", open_=105, high=120, low=100, close=115, volume=20, value=2000,
    )
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:02", open_=115, high=118, low=108, close=110, volume=30, value=3000,
    )
    conn.commit()
    conn.close()

    result = await MinuteAggBuilder(db_path).aggregate_for_tracking_row(
        daily_id, intervals=(3,)
    )
    assert result == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT bucket_start, bucket_end, open, high, low, close, volume,"
        " value, raw_count, expected_count, is_complete"
        " FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (
        "2026-05-06T09:00:00",
        "2026-05-06T09:02:00",
        100.0,
        120.0,
        95.0,
        110.0,
        60,
        6000,
        3,
        3,
        1,
    )


@pytest.mark.asyncio
async def test_five_minute_incomplete_aggregation(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    for hhmm in ("09:00", "09:01", "09:04"):
        _insert_raw(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", hhmm)
    conn.commit()
    conn.close()

    result = await MinuteAggBuilder(db_path).aggregate_for_tracking_row(
        daily_id, intervals=(5,)
    )
    assert result == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT bucket_start, bucket_end, raw_count, expected_count, is_complete"
        " FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == ("2026-05-06T09:00:00", "2026-05-06T09:04:00", 3, 5, 0)


def test_bucket_boundaries():
    builder = MinuteAggBuilder(":memory:")
    rows = [
        MinuteRawRow("2026-05-06T09:02:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T09:03:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T09:04:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T09:05:00", 1, 1, 1, 1, 1, None),
    ]

    three = builder.build_agg_bars(rows[:2], 3)
    five = builder.build_agg_bars(rows[2:], 5)

    assert [bar.bucket_start for bar in three] == [
        "2026-05-06T09:00:00",
        "2026-05-06T09:03:00",
    ]
    assert [bar.bucket_end for bar in three] == [
        "2026-05-06T09:02:00",
        "2026-05-06T09:05:00",
    ]
    assert [bar.bucket_start for bar in five] == [
        "2026-05-06T09:00:00",
        "2026-05-06T09:05:00",
    ]
    assert [bar.bucket_end for bar in five] == [
        "2026-05-06T09:04:00",
        "2026-05-06T09:09:00",
    ]


def test_nxt_premarket_anchor_buckets_08_and_keeps_0900_aligned():
    """session_start_hour=8: 08:00 장전 3분봉 생성 + 09:00 경계 정렬 유지."""
    builder = MinuteAggBuilder(":memory:", session_start_hour=8)
    rows = [
        MinuteRawRow("2026-05-06T08:00:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T08:01:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T08:02:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T09:00:00", 1, 1, 1, 1, 1, None),
    ]

    bars = builder.build_agg_bars(rows, 3)

    starts = [bar.bucket_start for bar in bars]
    # 장전 08:00 버킷이 생성되고, 09:00은 60분=3의 배수라 깨끗한 버킷 경계로 정렬.
    assert "2026-05-06T08:00:00" in starts
    assert "2026-05-06T09:00:00" in starts


def test_default_anchor_still_skips_premarket():
    """기본(09:00 앵커): 08:xx 장전 분봉은 버려지고 09:00만 집계된다."""
    builder = MinuteAggBuilder(":memory:")
    rows = [
        MinuteRawRow("2026-05-06T08:30:00", 1, 1, 1, 1, 1, None),
        MinuteRawRow("2026-05-06T09:00:00", 1, 1, 1, 1, 1, None),
    ]

    bars = builder.build_agg_bars(rows, 3)

    assert [bar.bucket_start for bar in bars] == ["2026-05-06T09:00:00"]


def test_value_null_handling():
    builder = MinuteAggBuilder(":memory:")
    all_null = builder.build_agg_bars(
        [
            MinuteRawRow("2026-05-06T09:00:00", 1, 1, 1, 1, 1, None),
            MinuteRawRow("2026-05-06T09:01:00", 1, 1, 1, 1, 1, None),
        ],
        3,
    )
    partial = builder.build_agg_bars(
        [
            MinuteRawRow("2026-05-06T09:00:00", 1, 1, 1, 1, 1, None),
            MinuteRawRow("2026-05-06T09:01:00", 1, 1, 1, 1, 1, 2000),
            MinuteRawRow("2026-05-06T09:02:00", 1, 1, 1, 1, 1, 3000),
        ],
        3,
    )

    assert all_null[0].value is None
    assert partial[0].value == 5000


@pytest.mark.asyncio
async def test_delete_and_replace_reaggregation(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_raw(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", "09:00", close=105)
    conn.commit()
    conn.close()

    builder = MinuteAggBuilder(db_path)
    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    conn.execute("DELETE FROM pick_minute_raw WHERE daily_tracking_id = ?", (daily_id,))
    _insert_raw(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", "09:01", close=150)
    conn.commit()
    conn.close()

    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT bucket_start, close FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [("2026-05-06T09:00:00", 150.0)]


@pytest.mark.asyncio
async def test_no_raw_preserves_existing_agg(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_raw(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", "09:00")
    conn.commit()
    conn.close()

    builder = MinuteAggBuilder(db_path)
    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    conn.execute("DELETE FROM pick_minute_raw WHERE daily_tracking_id = ?", (daily_id,))
    conn.commit()
    conn.close()

    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SKIPPED_NO_RAW

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT bucket_start FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [("2026-05-06T09:00:00",)]


@pytest.mark.asyncio
async def test_multi_event_same_stock_isolated(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_a = _seed_event(conn, pick_id)
    event_b = _seed_event(conn, pick_id)
    daily_a = _seed_daily(conn, stock_pick_id, event_a)
    daily_b = _seed_daily(conn, stock_pick_id, event_b)
    _insert_raw(conn, daily_a, event_a, stock_pick_id, "005930", "2026-05-06", "09:00", close=101)
    _insert_raw(conn, daily_b, event_b, stock_pick_id, "005930", "2026-05-06", "09:00", close=202)
    conn.commit()
    conn.close()

    builder = MinuteAggBuilder(db_path)
    assert await builder.aggregate_for_tracking_row(daily_a, intervals=(3,)) == MinuteAggResult.SUCCESS
    assert await builder.aggregate_for_tracking_row(daily_b, intervals=(3,)) == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, event_id, close FROM pick_minute_agg"
        " ORDER BY daily_tracking_id"
    ).fetchall()
    conn.close()
    assert rows == [(daily_a, event_a, 101.0), (daily_b, event_b, 202.0)]


@pytest.mark.asyncio
async def test_trading_day_filter_limits_aggregate_all(db_path: str):
    conn = _connect(db_path)
    day_1, event_1, pick_1 = _seed_tracking(conn, trading_day="2026-05-06")
    day_2, event_2, pick_2 = _seed_tracking(conn, trading_day="2026-05-07")
    _insert_raw(conn, day_1, event_1, pick_1, "005930", "2026-05-06", "09:00")
    _insert_raw(conn, day_2, event_2, pick_2, "005930", "2026-05-07", "09:00")
    conn.commit()
    conn.close()

    counts = await MinuteAggBuilder(db_path).aggregate_all_d0(
        trading_day="2026-05-07", intervals=(3,)
    )
    assert counts[MinuteAggResult.SUCCESS.value] == 1

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, trading_day FROM pick_minute_agg"
    ).fetchall()
    conn.close()
    assert rows == [(day_2, "2026-05-07")]


@pytest.mark.asyncio
@pytest.mark.parametrize("intervals", [(0,), (-3,), (3, 3), (10,), ()])
async def test_invalid_intervals_fail_and_preserve_existing_agg(
    db_path: str, intervals: tuple[int, ...]
):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    _insert_raw(conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06", "09:00")
    conn.commit()
    conn.close()

    builder = MinuteAggBuilder(db_path)
    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SUCCESS
    assert await builder.aggregate_for_tracking_row(daily_id, intervals=intervals) == MinuteAggResult.FAILED

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT interval_minutes, bucket_start FROM pick_minute_agg"
        " WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [(3, "2026-05-06T09:00:00")]


@pytest.mark.asyncio
async def test_aggregate_all_invalid_intervals_returns_failed_count(db_path: str):
    counts = await MinuteAggBuilder(db_path).aggregate_all_d0(intervals=(0,))

    assert counts[MinuteAggResult.FAILED.value] == 1
    assert counts[MinuteAggResult.SUCCESS.value] == 0
    assert counts[MinuteAggResult.SKIPPED_NO_TARGET.value] == 0
    assert counts[MinuteAggResult.SKIPPED_NO_RAW.value] == 0


@pytest.mark.asyncio
async def test_raw_trading_day_filter_excludes_mismatched_rows(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn, trading_day="2026-05-06")
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:00", close=101,
    )
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-07",
        "09:01", close=202,
    )
    conn.commit()
    conn.close()

    result = await MinuteAggBuilder(db_path).aggregate_for_tracking_row(
        daily_id, intervals=(3,)
    )
    assert result == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT close, raw_count FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (101.0, 1)


@pytest.mark.asyncio
async def test_minute_time_date_mismatch_is_skipped(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn, trading_day="2026-05-06")
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:00", close=101,
    )
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:01", close=202, minute_time="2026-05-07T09:01:00",
    )
    conn.commit()
    conn.close()

    result = await MinuteAggBuilder(db_path).aggregate_for_tracking_row(
        daily_id, intervals=(3,)
    )
    assert result == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT close, raw_count FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (101.0, 1)


@pytest.mark.asyncio
async def test_only_minute_time_date_mismatch_returns_no_raw_and_preserves_agg(
    db_path: str,
):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn, trading_day="2026-05-06")
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:00", close=101,
    )
    conn.commit()
    conn.close()

    builder = MinuteAggBuilder(db_path)
    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SUCCESS

    conn = _connect(db_path)
    conn.execute("DELETE FROM pick_minute_raw WHERE daily_tracking_id = ?", (daily_id,))
    _insert_raw(
        conn, daily_id, event_id, stock_pick_id, "005930", "2026-05-06",
        "09:00", close=202, minute_time="2026-05-07T09:00:00",
    )
    conn.commit()
    conn.close()

    assert await builder.aggregate_for_tracking_row(daily_id, intervals=(3,)) == MinuteAggResult.SKIPPED_NO_RAW

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT close, raw_count FROM pick_minute_agg WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchone()
    conn.close()
    assert row == (101.0, 1)
