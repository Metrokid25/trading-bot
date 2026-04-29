"""sector_pick_events에 pick_date 컬럼 추가.

변경 내용:
  A. sector_pick_events.pick_date (TEXT, nullable) 추가

registered_at_kst는 명령 처리 시각(항상 오늘), pick_date는 사용자가
지정한 픽 날짜(백필 시 과거 날짜 가능). 두 값을 분리 보관한다.
백필 없음, 신규 행부터 채움.
트랜잭션 범위는 migration_runner가 관리한다 (up() 내부에서 BEGIN/COMMIT 금지).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "003"
NAME = "sector_pick_event_pick_date"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def up(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "sector_pick_events", "pick_date"):
        conn.execute(
            "ALTER TABLE sector_pick_events"
            " ADD COLUMN pick_date TEXT"
        )
