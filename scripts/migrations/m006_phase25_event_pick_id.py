"""sector_pick_events에 pick_id (sector_picks FK, NOT NULL) 추가.

변경 내용:
  A. sector_pick_events 테이블 재생성 — pick_id INTEGER NOT NULL REFERENCES sector_picks(id)
  B. idx_spe_sector_at 인덱스 재생성

동작:
  - sector_pick_events row 수 0 확인 → 아니면 명시적 에러 (backfill 필요 시 별도 처리)
  - sector_pick_events_new 생성 → DROP 기존 → RENAME
  - migration_runner가 트랜잭션 관리 (up() 내 BEGIN/COMMIT 금지)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "006"
NAME = "phase25_event_pick_id"


def up(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) FROM sector_pick_events").fetchone()
    if row[0] != 0:
        raise RuntimeError(
            f"m006 aborted: sector_pick_events has {row[0]} rows. "
            "Backfill pick_id manually before running this migration."
        )

    conn.execute(
        """
        CREATE TABLE sector_pick_events_new (
            event_id                            INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id                             INTEGER NOT NULL REFERENCES sector_picks(id),
            sector_name                         TEXT    NOT NULL,
            registered_at_kst                   TEXT    NOT NULL,
            is_sector_repick                    INTEGER DEFAULT 0,
            prev_event_id                       INTEGER,
            days_since_last_sector_pick         INTEGER,
            trading_days_since_last_sector_pick INTEGER,
            total_sector_pick_count             INTEGER DEFAULT 1,
            pick_date                           TEXT
        )
        """
    )
    conn.execute("DROP TABLE sector_pick_events")
    conn.execute("ALTER TABLE sector_pick_events_new RENAME TO sector_pick_events")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spe_sector_at"
        " ON sector_pick_events(sector_name, registered_at_kst)"
    )
