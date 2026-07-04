"""gm_v3 시그널 로깅 — gm_v3_signals 테이블 (m010) 적재.

INSERT OR IGNORE + UNIQUE(run_id, bar_day, stock_code, rule, signal_type) 로
같은 런의 재실행이 중복 행을 만들지 않는다(멱등).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.time_utils import now_kst, to_db_iso
from strategy.gm_v3.models import Signal


def log_signals(db_path: str | Path, signals: list[Signal], *,
                run_id: str, source: str = "backtest") -> int:
    """시그널 목록을 적재하고 신규 삽입 행 수를 반환.

    주의: 같은 run_id 재적재는 무시(멱등)되므로, config 를 바꿔 재실행할 땐
    새 run_id 를 써야 한다 — weight/price 변경은 기존 행에 반영되지 않음.
    """
    if not run_id:
        raise ValueError("run_id 필수 — 빈 값/None 이면 멱등 dedup 이 깨짐 "
                         "(SQLite UNIQUE 는 NULL 을 서로 다른 값으로 취급)")
    if not signals:
        return 0
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=30000")
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='gm_v3_signals'").fetchone()
        if not has_table:
            raise RuntimeError(
                "gm_v3_signals 테이블 없음 — 먼저 마이그레이션 실행: "
                "python scripts/migrations/migration_runner.py (m010)")
        now_iso = to_db_iso(now_kst())
        cur = con.executemany(
            "INSERT OR IGNORE INTO gm_v3_signals "
            "(fired_at, bar_day, stock_code, signal_type, rule, weight, "
            " price, reason_json, run_id, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(now_iso, s.day.isoformat(), s.stock_code, s.type.value, s.rule,
              s.weight, s.price, json.dumps(s.reason, ensure_ascii=False),
              run_id, source) for s in signals])
        con.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 \
            else 0
    finally:
        con.close()
