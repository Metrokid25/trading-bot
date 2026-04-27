"""SQLite 마이그레이션 러너.

프로젝트 루트에서 실행:
    python scripts/migrations/migration_runner.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

# scripts/migrations/ → scripts/ → PROJECT_ROOT
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.time_utils import now_kst, to_db_iso


def backup_db(db_path: str) -> str:
    """KST 기준 timestamp로 DB 백업 파일을 생성한다.

    Args:
        db_path: 백업할 SQLite DB 파일 경로.

    Returns:
        생성된 백업 파일의 절대 경로.

    Raises:
        FileNotFoundError: db_path 파일이 존재하지 않으면.
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {db_path}")
    ts = now_kst().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.backup_{ts}"
    shutil.copy2(src, backup_path)
    print(f"[마이그레이션] 백업 생성: {backup_path}")
    return backup_path


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version TEXT PRIMARY KEY,"
        "  name    TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL"
        ")"
    )


def run_migrations(db_path: str, migrations: list, *, backup: bool = True) -> None:
    """등록된 마이그레이션을 순차 실행한다.

    각 마이그레이션 모듈은 다음을 export해야 한다:
        VERSION: str  (예: "001")
        NAME:    str  (예: "phase25_tracking")
        up(conn: sqlite3.Connection) -> None

    이미 적용된 version은 skip (멱등). 실패 시 자동 롤백 후 raise.

    Args:
        db_path: 대상 SQLite DB 파일 경로.
        migrations: 마이그레이션 모듈 목록 (VERSION 오름차순으로 전달).
        backup: True면 실행 전 DB 백업을 먼저 생성한다.

    Raises:
        FileNotFoundError: backup=True인데 DB 파일이 없으면.
        Exception: 마이그레이션 up() 실패 시 롤백 후 re-raise.
    """
    if backup:
        backup_db(db_path)

    # isolation_level=None: 수동 BEGIN/COMMIT/ROLLBACK 사용
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        _ensure_migrations_table(conn)

        for migration in migrations:
            version: str = migration.VERSION
            name: str = migration.NAME

            already_applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()

            if already_applied:
                print(f"[마이그레이션] {version} ({name}) 이미 적용됨, 건너뜀")
                continue

            print(f"[마이그레이션] {version} ({name}) 적용 시작...")
            try:
                conn.execute("BEGIN")
                migration.up(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at)"
                    " VALUES (?, ?, ?)",
                    (version, name, to_db_iso(now_kst())),
                )
                conn.execute("COMMIT")
                print(f"[마이그레이션] {version} ({name}) 완료")
            except Exception as exc:
                conn.execute("ROLLBACK")
                print(f"[마이그레이션] {version} ({name}) 실패, 롤백 완료: {exc}")
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    from config.settings import settings
    from scripts.migrations import m001_phase25_tracking

    run_migrations(str(settings.DB_PATH), [m001_phase25_tracking], backup=True)
