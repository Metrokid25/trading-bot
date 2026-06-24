"""테스트로 등록된 픽을 운영 DB에서 완전 제거한다.

웹/텔레그램으로 잘못 등록했거나 테스트한 픽을 정리하는 일회성 유틸.
실행 전 KST 타임스탬프로 DB를 자동 백업한다.

사용법:
    python scripts/cleanup_test_pick.py <pick_id> [<pick_id> ...]
예:
    .venv/Scripts/python.exe scripts/cleanup_test_pick.py 14
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from core.time_utils import now_kst  # noqa: E402

# 자식 → 부모 순. event_id로 연결된 Phase 2.5 데이터부터 지운 뒤 픽/종목/이벤트.
_EVENT_SCOPED_TABLES = (
    "pick_breakout_marks",
    "pick_minute_agg",
    "pick_minute_raw",
    "pick_daily_tracking",
)


def cleanup(db_path: str, pick_ids: list[int]) -> None:
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(db_path)

    ts = now_kst().strftime("%Y%m%d_%H%M%S")
    backup = f"{db_path}.backup_{ts}"
    shutil.copy2(src, backup)
    print(f"[cleanup] 백업 생성: {backup}")

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for pick_id in pick_ids:
            events = [
                int(r[0])
                for r in conn.execute(
                    "SELECT event_id FROM sector_pick_events WHERE pick_id = ?",
                    (pick_id,),
                )
            ]
            conn.execute("BEGIN")
            try:
                deleted: dict[str, int] = {}
                if events:
                    placeholders = ",".join("?" for _ in events)
                    for table in _EVENT_SCOPED_TABLES:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE event_id IN ({placeholders})",
                            events,
                        )
                        deleted[table] = cur.rowcount or 0
                deleted["sector_pick_events"] = conn.execute(
                    "DELETE FROM sector_pick_events WHERE pick_id = ?", (pick_id,)
                ).rowcount or 0
                deleted["sector_stocks"] = conn.execute(
                    "DELETE FROM sector_stocks WHERE pick_id = ?", (pick_id,)
                ).rowcount or 0
                deleted["sector_picks"] = conn.execute(
                    "DELETE FROM sector_picks WHERE id = ?", (pick_id,)
                ).rowcount or 0
                conn.execute("COMMIT")
                summary = ", ".join(f"{k}={v}" for k, v in deleted.items() if v)
                print(f"[cleanup] pick_id={pick_id} 제거: {summary or '(대상 없음)'}")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        remaining = conn.execute(
            "SELECT COUNT(*) FROM sector_picks WHERE status = 'active'"
        ).fetchone()[0]
        print(f"[cleanup] 남은 active pick: {remaining}")
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("사용법: python scripts/cleanup_test_pick.py <pick_id> [<pick_id> ...]")
        return 2
    try:
        pick_ids = [int(a) for a in sys.argv[1:]]
    except ValueError:
        print("pick_id는 정수여야 합니다.")
        return 2
    cleanup(str(settings.DB_PATH), pick_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
