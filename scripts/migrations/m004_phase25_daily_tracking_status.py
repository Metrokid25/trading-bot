"""pick_daily_tracking에 수집 상태 추적 컬럼 추가.

변경 내용:
  A. pick_daily_tracking.status (TEXT NOT NULL DEFAULT 'pending') 추가
     값: 'pending' | 'success' | 'failed_temp' | 'failed_permanent'
  B. pick_daily_tracking.retry_count (INTEGER NOT NULL DEFAULT 0) 추가
     범위: 0~3, 재시도 횟수 추적
  C. pick_daily_tracking.event_id (INTEGER, nullable) 추가
     FK → sector_pick_events(event_id), NULL 허용 (기존 행 + best-effort 실패)
  D. idx_pdt_event 인덱스 추가 (event_id 기준 조회 최적화)

백필 없음 — 기존 행은 DEFAULT 값(pending/0/NULL)으로 채워진다.
트랜잭션 범위는 migration_runner가 관리한다 (up() 내부에서 BEGIN/COMMIT 금지).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "004"
NAME = "phase25_daily_tracking_status"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def up(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "pick_daily_tracking", "status"):
        conn.execute(
            "ALTER TABLE pick_daily_tracking"
            " ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )

    if not _column_exists(conn, "pick_daily_tracking", "retry_count"):
        conn.execute(
            "ALTER TABLE pick_daily_tracking"
            " ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
        )

    if not _column_exists(conn, "pick_daily_tracking", "event_id"):
        conn.execute(
            "ALTER TABLE pick_daily_tracking"
            " ADD COLUMN event_id INTEGER REFERENCES sector_pick_events(event_id)"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdt_event"
        " ON pick_daily_tracking(event_id)"
    )
