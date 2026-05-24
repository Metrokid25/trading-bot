from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.minute_raw_tracker import MinuteCollectResult, MinuteRawTracker
from scripts.migrations import m007_phase25_minute_raw_rebuild as m007


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
    path = str(tmp_path / "minute_raw.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    conn.commit()
    conn.close()
    return path


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_pick(conn: sqlite3.Connection, stock_code: str, *, active: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES ('2026-05-06', '2026-05-06T09:00:00', '2026-05-30T09:00:00', 'active')"
    )
    pick_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO sector_stocks"
        " (pick_id, sector_name, stock_code, stock_name, added_order, tracking_status)"
        " VALUES (?, '반도체', ?, '삼성전자', 1, ?)",
        (pick_id, stock_code, "active" if active else "inactive"),
    )
    return int(cur.lastrowid)


def _seed_event(conn: sqlite3.Connection, pick_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, '반도체', '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id,),
    )
    return int(cur.lastrowid)


def _seed_daily(
    conn: sqlite3.Connection,
    stock_pick_id: int,
    event_id: int,
    *,
    trading_day: str = "2026-05-06",
    day_offset: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO pick_daily_tracking"
        " (stock_pick_id, trading_day, day_offset, created_at, status, event_id)"
        " VALUES (?, ?, ?, '2026-05-06T09:00:00', 'pending', ?)",
        (stock_pick_id, trading_day, day_offset, event_id),
    )
    return int(cur.lastrowid)


def _kis_row(
    yyyymmdd: str = "20260506",
    hhmmss: str = "093000",
    *,
    close: int = 1030,
    volume: int = 500,
    value: int | None = None,
) -> dict:
    row = {
        "stck_bsop_date": yyyymmdd,
        "stck_cntg_hour": hhmmss,
        "stck_oprc": "1000",
        "stck_hgpr": "1050",
        "stck_lwpr": "990",
        "stck_prpr": str(close),
        "cntg_vol": str(volume),
    }
    if value is not None:
        row["acml_tr_pbmn"] = str(value)
    return row


def test_m007_rebuilds_empty_legacy_table_with_new_schema(tmp_path: Path):
    path = str(tmp_path / "migration.db")
    conn = _connect(path)
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)

    m007.up(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(pick_minute_raw)")}
    assert {
        "daily_tracking_id",
        "event_id",
        "stock_code",
        "day_offset",
        "minute_time",
        "value",
        "source",
        "updated_at",
    }.issubset(cols)
    assert "bar_time" not in cols
    assert "minute_idx" not in cols
    assert "transaction_amount" not in cols

    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    daily_tracking_id = _seed_daily(conn, stock_pick_id, event_id)

    params = (
        daily_tracking_id,
        event_id,
        stock_pick_id,
        "005930",
        "2026-05-06",
        0,
        "2026-05-06T09:30:00",
        "KIS",
        "2026-05-06T16:00:00",
        "2026-05-06T16:00:00",
    )
    conn.execute(
        "INSERT INTO pick_minute_raw"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, minute_time, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pick_minute_raw"
            " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
            "  day_offset, minute_time, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
    conn.close()


def test_m007_aborts_when_legacy_table_has_data(tmp_path: Path):
    path = str(tmp_path / "migration_abort.db")
    conn = _connect(path)
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    stock_pick_id = _seed_pick(conn, "005930")
    conn.execute(
        "INSERT INTO pick_minute_raw"
        " (stock_pick_id, trading_day, bar_time, minute_idx, created_at)"
        " VALUES (?, '2026-05-06', '2026-05-06T09:30:00', 30, '2026-05-06T16:00:00')",
        (stock_pick_id,),
    )

    with pytest.raises(RuntimeError, match="m007 aborted"):
        m007.up(conn)
    conn.close()


@pytest.mark.asyncio
async def test_list_d0_targets_filters_offset_and_active_and_keeps_events(db_path: str):
    conn = _connect(db_path)
    active_stock = _seed_pick(conn, "005930", active=True)
    active_pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (active_stock,)
    ).fetchone()[0]
    event_a = _seed_event(conn, active_pick_id)
    event_b = _seed_event(conn, active_pick_id)
    d0_a = _seed_daily(conn, active_stock, event_a)
    d0_b = _seed_daily(conn, active_stock, event_b)
    _seed_daily(conn, active_stock, event_a, trading_day="2026-05-07", day_offset=1)

    inactive_stock = _seed_pick(conn, "000660", active=False)
    inactive_pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (inactive_stock,)
    ).fetchone()[0]
    inactive_event = _seed_event(conn, inactive_pick_id)
    _seed_daily(conn, inactive_stock, inactive_event)
    conn.commit()
    conn.close()

    tracker = MinuteRawTracker(db_path, MagicMock())
    targets = await tracker.list_d0_targets()

    assert [target.daily_tracking_id for target in targets] == [d0_a, d0_b]
    assert [target.event_id for target in targets] == [event_a, event_b]
    assert all(target.stock_code == "005930" for target in targets)


@pytest.mark.asyncio
async def test_trading_day_filter_limits_targets_and_collect_all(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    day_1 = _seed_daily(
        conn, stock_pick_id, event_id, trading_day="2026-05-06"
    )
    day_2 = _seed_daily(
        conn, stock_pick_id, event_id, trading_day="2026-05-07"
    )
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(
        side_effect=[[_kis_row("20260507", "093000")], []]
    )
    tracker = MinuteRawTracker(db_path, client)

    targets = await tracker.list_d0_targets(trading_day="2026-05-07")
    assert [target.daily_tracking_id for target in targets] == [day_2]

    counts = await tracker.collect_d0_all(trading_day="2026-05-07")
    assert counts[MinuteCollectResult.SUCCESS.value] == 1

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, trading_day, minute_time FROM pick_minute_raw"
    ).fetchall()
    conn.close()
    assert rows == [(day_2, "2026-05-07", "2026-05-07T09:30:00")]
    assert day_1 != day_2


@pytest.mark.asyncio
async def test_fetch_minute_raw_parses_filters_malformed_and_dedupes():
    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(
        side_effect=[
            [
                _kis_row("20260506", "093000", value=123456),
                _kis_row("20260506", "093000", value=123456),
                _kis_row("20260507", "093100"),
                {"stck_bsop_date": "20260506", "stck_cntg_hour": "bad"},
            ],
            [],
        ]
    )
    tracker = MinuteRawTracker(":memory:", client)

    bars = await tracker.fetch_minute_raw_for_day("005930", "2026-05-06")

    assert len(bars) == 1
    assert bars[0].minute_time == "2026-05-06T09:30:00"
    assert bars[0].open == 1000
    assert bars[0].close == 1030
    assert bars[0].volume == 500
    assert bars[0].value == 123456


@pytest.mark.asyncio
async def test_collect_replaces_existing_rows_on_success(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_a = _seed_event(conn, pick_id)
    daily_a = _seed_daily(conn, stock_pick_id, event_a)
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(
        side_effect=[
            [_kis_row(hhmmss="093000")],
            [],
            [_kis_row(hhmmss="093100", close=1040)],
            [],
        ]
    )
    tracker = MinuteRawTracker(db_path, client)

    assert await tracker.collect_d0_for_tracking_row(daily_a) == MinuteCollectResult.SUCCESS
    assert await tracker.collect_d0_for_tracking_row(daily_a) == MinuteCollectResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT minute_time, close FROM pick_minute_raw WHERE daily_tracking_id = ?",
        (daily_a,),
    ).fetchall()
    conn.close()
    assert rows == [("2026-05-06T09:31:00", 1040.0)]


@pytest.mark.asyncio
async def test_collect_preserves_existing_rows_when_refetch_has_no_bars(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    daily_id = _seed_daily(conn, stock_pick_id, event_id)
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(side_effect=[[_kis_row()], [], []])
    tracker = MinuteRawTracker(db_path, client)

    assert await tracker.collect_d0_for_tracking_row(daily_id) == MinuteCollectResult.SUCCESS
    assert (
        await tracker.collect_d0_for_tracking_row(daily_id)
        == MinuteCollectResult.SKIPPED_NO_BARS
    )

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT minute_time FROM pick_minute_raw WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [("2026-05-06T09:30:00",)]


@pytest.mark.asyncio
async def test_collect_preserves_existing_rows_when_refetch_fails(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    daily_id = _seed_daily(conn, stock_pick_id, event_id)
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(
        side_effect=[[_kis_row()], [], RuntimeError("KIS down")]
    )
    tracker = MinuteRawTracker(db_path, client)

    assert await tracker.collect_d0_for_tracking_row(daily_id) == MinuteCollectResult.SUCCESS
    assert await tracker.collect_d0_for_tracking_row(daily_id) == MinuteCollectResult.FAILED

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT minute_time FROM pick_minute_raw WHERE daily_tracking_id = ?",
        (daily_id,),
    ).fetchall()
    conn.close()
    assert rows == [("2026-05-06T09:30:00",)]


@pytest.mark.asyncio
async def test_collect_allows_multi_event_same_stock_same_minute(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_a = _seed_event(conn, pick_id)
    event_b = _seed_event(conn, pick_id)
    daily_a = _seed_daily(conn, stock_pick_id, event_a)
    daily_b = _seed_daily(conn, stock_pick_id, event_b)
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(side_effect=[[_kis_row()], [], [_kis_row()], []])
    tracker = MinuteRawTracker(db_path, client)

    assert await tracker.collect_d0_for_tracking_row(daily_a) == MinuteCollectResult.SUCCESS
    assert await tracker.collect_d0_for_tracking_row(daily_b) == MinuteCollectResult.SUCCESS

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT daily_tracking_id, event_id, stock_code, minute_time FROM pick_minute_raw"
        " ORDER BY daily_tracking_id"
    ).fetchall()
    conn.close()
    assert rows == [
        (daily_a, event_a, "005930", "2026-05-06T09:30:00"),
        (daily_b, event_b, "005930", "2026-05-06T09:30:00"),
    ]


@pytest.mark.asyncio
async def test_collect_result_enums(db_path: str):
    no_target_tracker = MinuteRawTracker(db_path, MagicMock())
    assert (
        await no_target_tracker.collect_d0_for_tracking_row(9999)
        == MinuteCollectResult.SKIPPED_NO_TARGET
    )

    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    daily_id = _seed_daily(conn, stock_pick_id, event_id)
    conn.commit()
    conn.close()

    empty_client = MagicMock()
    empty_client.get_minute_candles_at = AsyncMock(return_value=[])
    empty_tracker = MinuteRawTracker(db_path, empty_client)
    assert (
        await empty_tracker.collect_d0_for_tracking_row(daily_id)
        == MinuteCollectResult.SKIPPED_NO_BARS
    )

    ok_client = MagicMock()
    ok_client.get_minute_candles_at = AsyncMock(side_effect=[[_kis_row()], []])
    ok_tracker = MinuteRawTracker(db_path, ok_client)
    assert await ok_tracker.collect_d0_for_tracking_row(daily_id) == MinuteCollectResult.SUCCESS

    conn = _connect(db_path)
    stock_pick_id_2 = _seed_pick(conn, "000660")
    pick_id_2 = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id_2,)
    ).fetchone()[0]
    event_id_2 = _seed_event(conn, pick_id_2)
    daily_id_2 = _seed_daily(conn, stock_pick_id_2, event_id_2)
    conn.commit()
    conn.close()

    failing_client = MagicMock()
    failing_client.get_minute_candles_at = AsyncMock(side_effect=RuntimeError("KIS down"))
    failing_tracker = MinuteRawTracker(db_path, failing_client)
    assert (
        await failing_tracker.collect_d0_for_tracking_row(daily_id_2)
        == MinuteCollectResult.FAILED
    )


@pytest.mark.asyncio
async def test_collect_d0_all_returns_counts(db_path: str):
    conn = _connect(db_path)
    stock_pick_id = _seed_pick(conn, "005930")
    pick_id = conn.execute(
        "SELECT pick_id FROM sector_stocks WHERE id = ?", (stock_pick_id,)
    ).fetchone()[0]
    event_id = _seed_event(conn, pick_id)
    _seed_daily(conn, stock_pick_id, event_id)
    conn.commit()
    conn.close()

    client = MagicMock()
    client.get_minute_candles_at = AsyncMock(side_effect=[[_kis_row()], []])
    tracker = MinuteRawTracker(db_path, client)

    counts = await tracker.collect_d0_all()

    assert counts[MinuteCollectResult.SUCCESS.value] == 1
    assert counts[MinuteCollectResult.FAILED.value] == 0
