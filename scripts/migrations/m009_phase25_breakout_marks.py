"""Create pick_breakout_marks for Phase 2.5 breakout labeling."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "009"
NAME = "phase25_breakout_marks"


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pick_breakout_marks (
            id                             INTEGER PRIMARY KEY AUTOINCREMENT,
            daily_tracking_id              INTEGER NOT NULL REFERENCES pick_daily_tracking(id),
            event_id                       INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
            stock_pick_id                  INTEGER NOT NULL REFERENCES sector_stocks(id),
            stock_code                     TEXT    NOT NULL,
            trading_day                    TEXT    NOT NULL,
            day_offset                     INTEGER NOT NULL,
            interval_minutes               INTEGER NOT NULL,
            agg_id                         INTEGER NOT NULL REFERENCES pick_minute_agg(id),
            bucket_start                   TEXT    NOT NULL,
            bucket_end                     TEXT    NOT NULL,
            breakout_type                  TEXT    NOT NULL,
            prev_close                     REAL,
            current_close                  REAL,
            day_open                       REAL,
            prev_close_change_rate         REAL,
            day_open_change_rate           REAL,
            value                          INTEGER,
            prev_value                     INTEGER,
            value_ratio                    REAL,
            threshold_prev_change_rate     REAL    NOT NULL,
            threshold_day_open_change_rate REAL    NOT NULL,
            threshold_value                INTEGER NOT NULL,
            threshold_value_ratio          REAL    NOT NULL,
            rule_version                   TEXT    NOT NULL,
            created_at                     TEXT    NOT NULL,
            updated_at                     TEXT    NOT NULL,
            UNIQUE(daily_tracking_id, rule_version, interval_minutes, bucket_start, breakout_type)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breakout_marks_event_day"
        " ON pick_breakout_marks(event_id, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breakout_marks_stock_day"
        " ON pick_breakout_marks(stock_code, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breakout_marks_tracking_rule"
        " ON pick_breakout_marks(daily_tracking_id, rule_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breakout_marks_type_day"
        " ON pick_breakout_marks(breakout_type, trading_day)"
    )
