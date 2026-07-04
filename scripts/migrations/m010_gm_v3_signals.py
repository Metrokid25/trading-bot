"""Create gm_v3_signals — gm_v3 룰 엔진 시그널 로깅 (신규 테이블만, ALTER 없음)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "010"
NAME = "gm_v3_signals"


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gm_v3_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at    TEXT NOT NULL,             -- 기록 시각 (KST ISO)
            bar_day     TEXT NOT NULL,             -- 신호가 발생한 일봉 날짜
            stock_code  TEXT NOT NULL,
            signal_type TEXT NOT NULL
                CHECK(signal_type IN ('BUY','SELL','WATCH','MARK','HOLD')),
            rule        TEXT NOT NULL,             -- 'R1' ~ 'R12'
            weight      REAL NOT NULL,             -- BUY 투입/SELL 청산 비율
            price       REAL NOT NULL,             -- 당시 참조/트리거 가격
            reason_json TEXT NOT NULL DEFAULT '{}',-- 근거 수치
            run_id      TEXT,                      -- 백테스트/페이퍼 런 식별자
            source      TEXT NOT NULL DEFAULT 'backtest',  -- backtest | paper
            UNIQUE(run_id, bar_day, stock_code, rule, signal_type)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gm_v3_signals_stock_day"
        " ON gm_v3_signals(stock_code, bar_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gm_v3_signals_run"
        " ON gm_v3_signals(run_id)"
    )
