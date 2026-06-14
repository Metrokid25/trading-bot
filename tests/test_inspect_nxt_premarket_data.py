from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.inspect_nxt_premarket_data import (
    _connect_readonly,
    format_report,
    inspect_db,
)


def _create_db(path: Path, *, migrations: tuple[str, ...] = ("007", "008", "009")) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    for version in migrations:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, f"m{version}", "2026-05-06T16:00:00"),
        )
    conn.execute(
        """
        CREATE TABLE pick_minute_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_tracking_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            stock_pick_id INTEGER NOT NULL,
            stock_code TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            day_offset INTEGER NOT NULL,
            minute_time TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            value INTEGER,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE pick_minute_agg (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_tracking_id INTEGER NOT NULL,
            interval_minutes INTEGER NOT NULL,
            trading_day TEXT NOT NULL,
            bucket_start TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _create_legacy_db(
    path: Path, *, migrations: tuple[str, ...] = ("001", "002", "003", "004", "005", "006")
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    for version in migrations:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, f"m{version}", "2026-05-06T16:00:00"),
        )
    conn.execute(
        """
        CREATE TABLE pick_minute_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id INTEGER NOT NULL,
            trading_day TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            minute_idx INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            transaction_amount INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _create_db_without_raw_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_raw(
    path: Path,
    *,
    stock_code: str = "005930",
    trading_day: str = "2026-05-06",
    minute_time: str = "2026-05-06T08:00:00",
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO pick_minute_raw
            (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,
             day_offset, minute_time, open, high, low, close, volume, value,
             source, created_at, updated_at)
        VALUES
            (1, 1, 1, ?, ?, 0, ?, 100, 110, 90, 105, 10, 1000,
             'TEST', '2026-05-06T16:00:00', '2026-05-06T16:00:00')
        """,
        (stock_code, trading_day, minute_time),
    )
    conn.commit()
    conn.close()


def _insert_legacy_raw(
    path: Path,
    *,
    trading_day: str = "2026-05-06",
    bar_time: str = "2026-05-06T08:00:00",
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO pick_minute_raw
            (stock_pick_id, trading_day, bar_time, minute_idx, open, high, low,
             close, volume, transaction_amount, created_at)
        VALUES
            (1, ?, ?, 0, 100, 110, 90, 105, 10, 1000, '2026-05-06T16:00:00')
        """,
        (trading_day, bar_time),
    )
    conn.commit()
    conn.close()


def _insert_agg(
    path: Path,
    *,
    trading_day: str,
    bucket_start: str,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO pick_minute_agg
            (daily_tracking_id, interval_minutes, trading_day, bucket_start)
        VALUES
            (1, 3, ?, ?)
        """,
        (trading_day, bucket_start),
    )
    conn.commit()
    conn.close()


def test_premarket_rows_present_returns_nxt_db_data_present(tmp_path: Path):
    db_path = tmp_path / "present.db"
    _create_db(db_path)
    _insert_raw(db_path, minute_time="2026-05-06T08:00:00")
    _insert_raw(db_path, minute_time="2026-05-06T08:50:00")

    result = inspect_db(db_path, trading_day="2026-05-06")

    assert result.premarket_rows == 2
    assert "NXT_DB_DATA_PRESENT" in result.verdicts
    assert result.premarket_stock_counts == [("005930", 2)]
    assert result.premarket_ohlcv[0] == ("005930", 2, 100.0, 110.0, 90.0, 105.0, 20, 2000)


def test_no_premarket_rows_returns_nxt_db_data_absent(tmp_path: Path):
    db_path = tmp_path / "absent.db"
    _create_db(db_path)
    _insert_raw(db_path, minute_time="2026-05-06T09:00:00")

    result = inspect_db(db_path, trading_day="2026-05-06")

    assert result.premarket_rows == 0
    assert result.regular_open_rows == 1
    assert "NXT_DB_DATA_ABSENT" in result.verdicts


def test_missing_migrations_returns_migrations_missing(tmp_path: Path):
    db_path = tmp_path / "missing_migrations.db"
    _create_db(db_path, migrations=("007", "008"))

    result = inspect_db(db_path)

    assert result.applied_migrations == {"007": True, "008": True, "009": False}
    assert "MIGRATIONS_MISSING" in result.verdicts


def test_readonly_connection_rejects_db_writes(tmp_path: Path):
    db_path = tmp_path / "readonly.db"
    _create_db(db_path)

    with _connect_readonly(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO pick_minute_agg"
                " (daily_tracking_id, interval_minutes, trading_day, bucket_start)"
                " VALUES (1, 3, '2026-05-06', '2026-05-06T08:00:00')"
            )

    result = inspect_db(db_path)
    assert result.total_raw_rows == 0


def test_legacy_bar_time_schema_does_not_crash(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_db(db_path)
    _insert_legacy_raw(db_path, bar_time="2026-05-06T08:00:00")

    result = inspect_db(db_path, trading_day="2026-05-06")
    report = format_report(result)

    assert result.raw_schema == "legacy_bar_time"
    assert result.total_raw_rows == 1
    assert result.trading_day_raw_rows == 1
    assert result.premarket_rows == 0
    assert "MIGRATIONS_MISSING" in result.verdicts
    assert "NXT_AGG_NEEDS_CHANGE" in result.verdicts
    assert "pick_minute_raw schema: legacy_bar_time" in report


def test_premarket_boundaries_include_0800_0850_and_exclude_0851(tmp_path: Path):
    db_path = tmp_path / "premarket_boundaries.db"
    _create_db(db_path)
    _insert_raw(db_path, minute_time="2026-05-06T08:00:00")
    _insert_raw(db_path, minute_time="2026-05-06T08:50:00")
    _insert_raw(db_path, minute_time="2026-05-06T08:51:00")

    result = inspect_db(db_path, trading_day="2026-05-06")

    assert result.premarket_rows == 2
    assert result.premarket_stock_counts == [("005930", 2)]


def test_regular_window_boundaries_do_not_overlap(tmp_path: Path):
    db_path = tmp_path / "regular_boundaries.db"
    _create_db(db_path)
    _insert_raw(db_path, minute_time="2026-05-06T09:00:00")
    _insert_raw(db_path, minute_time="2026-05-06T09:29:00")
    _insert_raw(db_path, minute_time="2026-05-06T09:30:00")
    _insert_raw(db_path, minute_time="2026-05-06T09:59:00")
    _insert_raw(db_path, minute_time="2026-05-06T10:00:00")

    result = inspect_db(db_path, trading_day="2026-05-06")

    assert result.regular_open_rows == 2
    assert result.regular_0930_rows == 2


def test_latest_trading_day_uses_latest_valid_yyyy_mm_dd(tmp_path: Path):
    db_path = tmp_path / "latest_valid_day.db"
    _create_db(db_path)
    _insert_raw(
        db_path,
        trading_day="2026-05-06",
        minute_time="2026-05-06T09:00:00",
    )
    _insert_raw(
        db_path,
        trading_day="2026-05-07",
        minute_time="2026-05-07T09:00:00",
    )
    _insert_raw(
        db_path,
        trading_day="not-a-date",
        minute_time="2026-12-31T09:00:00",
    )
    _insert_raw(
        db_path,
        trading_day="2026-99-99",
        minute_time="2026-12-31T09:00:00",
    )

    result = inspect_db(db_path)

    assert result.trading_day == "2026-05-07"
    assert result.trading_day_raw_rows == 1


def test_agg_08_bucket_count_is_limited_to_selected_trading_day(tmp_path: Path):
    db_path = tmp_path / "agg_day_filter.db"
    _create_db(db_path)
    _insert_raw(
        db_path,
        trading_day="2026-05-06",
        minute_time="2026-05-06T09:00:00",
    )
    _insert_raw(
        db_path,
        trading_day="2026-05-07",
        minute_time="2026-05-07T09:00:00",
    )
    _insert_agg(
        db_path,
        trading_day="2026-05-07",
        bucket_start="2026-05-07T08:00:00",
    )

    result_a = inspect_db(db_path, trading_day="2026-05-06")
    result_b = inspect_db(db_path, trading_day="2026-05-07")

    assert result_a.agg_08_bucket_count == 0
    assert result_b.agg_08_bucket_count == 1


def test_missing_m007_m008_m009_reports_safely_on_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "legacy_missing_migrations.db"
    _create_legacy_db(db_path, migrations=())

    result = inspect_db(db_path, trading_day="2026-05-06")
    report = format_report(result)

    assert result.applied_migrations == {"007": False, "008": False, "009": False}
    assert "MIGRATIONS_MISSING" in result.verdicts
    assert "pick_minute_raw schema: legacy_bar_time" in report


def test_missing_raw_table_with_explicit_trading_day_does_not_crash(tmp_path: Path):
    db_path = tmp_path / "missing_raw_table.db"
    _create_db_without_raw_table(db_path)

    result = inspect_db(db_path, trading_day="2026-05-06")
    report = format_report(result)

    assert result.raw_schema == "missing"
    assert result.trading_day == "2026-05-06"
    assert result.total_raw_rows == 0
    assert result.trading_day_raw_rows == 0
    assert "NXT_DB_DATA_ABSENT" in result.verdicts
    assert "MIGRATIONS_MISSING" in result.verdicts
    assert "pick_minute_raw schema: missing" in report
