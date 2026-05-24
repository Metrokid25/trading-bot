"""Create pick_minute_agg for Phase 2.5 minute aggregation."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "008"
NAME = "phase25_minute_agg"


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pick_minute_agg (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_tracking_id INTEGER NOT NULL REFERENCES pick_daily_tracking(id),
            event_id          INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
            stock_pick_id     INTEGER NOT NULL REFERENCES sector_stocks(id),
            stock_code        TEXT    NOT NULL,
            trading_day       TEXT    NOT NULL,
            day_offset        INTEGER NOT NULL,
            interval_minutes  INTEGER NOT NULL,
            bucket_start      TEXT    NOT NULL,
            bucket_end        TEXT    NOT NULL,
            open              REAL,
            high              REAL,
            low               REAL,
            close             REAL,
            volume            INTEGER,
            value             INTEGER,
            raw_count         INTEGER NOT NULL,
            expected_count    INTEGER NOT NULL,
            is_complete       INTEGER NOT NULL,
            source            TEXT    NOT NULL,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL,
            UNIQUE(daily_tracking_id, interval_minutes, bucket_start)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_agg_event_day"
        " ON pick_minute_agg(event_id, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_agg_stock_day_interval"
        " ON pick_minute_agg(stock_code, trading_day, interval_minutes)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pick_minute_agg_tracking_interval"
        " ON pick_minute_agg(daily_tracking_id, interval_minutes)"
    )
