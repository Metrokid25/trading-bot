"""sector_pick_events에 거래일 갭 컬럼 추가.

변경 내용:
  A. sector_pick_events.trading_days_since_last_sector_pick (INTEGER, nullable) 추가

자연일 갭은 days_since_last_sector_pick (m001) 에 이미 있다.
이 마이그레이션은 거래일 기준 갭 컬럼만 추가한다 — 백필 없음, 신규 행부터 채움.
트랜잭션 범위는 migration_runner가 관리한다 (up() 내부에서 BEGIN/COMMIT 금지).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "002"
NAME = "sector_repick_trading_days"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def up(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "sector_pick_events", "trading_days_since_last_sector_pick"):
        conn.execute(
            "ALTER TABLE sector_pick_events"
            " ADD COLUMN trading_days_since_last_sector_pick INTEGER"
        )
