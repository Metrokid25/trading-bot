"""Rebuild empty pick_minute_raw for Phase 2.5 minute raw collection.

This migration intentionally aborts when legacy pick_minute_raw contains data.
The legacy schema is only safe to drop while the table is empty.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "007"
NAME = "phase25_minute_raw_rebuild"


def up(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'pick_minute_raw'"
    ).fetchone()
    table_exists = row[0] > 0

    if table_exists:
        count = conn.execute("SELECT COUNT(*) FROM pick_minute_raw").fetchone()[0]
        if count != 0:
            raise RuntimeError(
                f"m007 aborted: pick_minute_raw has {count} rows. "
                "Backfill or preserve existing minute raw data before rebuilding."
            )
        conn.execute("DROP TABLE pick_minute_raw")

    conn.execute(
        """
        CREATE TABLE pick_minute_raw (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_tracking_id INTEGER NOT NULL REFERENCES pick_daily_tracking(id),
            event_id          INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
            stock_pick_id     INTEGER NOT NULL REFERENCES sector_stocks(id),
            stock_code        TEXT    NOT NULL,
            trading_day       TEXT    NOT NULL,
            day_offset        INTEGER NOT NULL,
            minute_time       TEXT    NOT NULL,
            open              REAL,
            high              REAL,
            low               REAL,
            close             REAL,
            volume            INTEGER,
            value             INTEGER,
            source            TEXT    NOT NULL,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL,
            UNIQUE(daily_tracking_id, minute_time)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_raw_event_day"
        " ON pick_minute_raw(event_id, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_raw_stock_day"
        " ON pick_minute_raw(stock_code, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_raw_tracking"
        " ON pick_minute_raw(daily_tracking_id)"
    )
