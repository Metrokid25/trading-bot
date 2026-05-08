"""pick_daily_tracking UNIQUE 제약을 event 단위로 확장.

변경 내용:
  A. UNIQUE(stock_pick_id, trading_day) → UNIQUE(event_id, stock_pick_id, trading_day)
     동일한 종목+날짜라도 event_id가 다르면 별도 행으로 격리 저장 가능.
     한 event의 UPSERT가 다른 event 행을 silently 덮어쓰는 버그 수정.
  B. event_id NOT NULL 적용 (m004 nullable → NOT NULL)
     모든 추적 행은 반드시 sector_pick_events와 연결되어야 함.
  C. 인덱스 재생성 (DROP TABLE이 기존 인덱스도 삭제하므로 재생성 필요):
     - idx_pdt_pick_day (stock_pick_id, trading_day)
     - idx_pdt_event (event_id)

SQLite는 UNIQUE 제약 직접 변경 불가 → 테이블 재생성 패턴 사용.
사전 조건: pick_daily_tracking 행 수 0 (보존 마이그레이션 미구현 가드).
트랜잭션 범위는 migration_runner가 관리한다 (up() 내부에서 BEGIN/COMMIT 금지).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "005"
NAME = "phase25_event_scoped_uniqueness"


def up(conn: sqlite3.Connection) -> None:
    # 사전 조건 가드: 기존 행이 있으면 데이터 손실 위험 → 중단
    count = conn.execute("SELECT COUNT(*) FROM pick_daily_tracking").fetchone()[0]
    if count > 0:
        raise RuntimeError(
            f"pick_daily_tracking에 {count}개 행이 존재합니다. "
            "데이터 보존 마이그레이션이 구현되지 않아 중단합니다."
        )

    # 새 테이블 생성 (컬럼 순서 동일, UNIQUE 변경 + event_id NOT NULL)
    conn.execute(
        """CREATE TABLE pick_daily_tracking_new (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id        INTEGER NOT NULL REFERENCES sector_stocks(id),
            trading_day          TEXT    NOT NULL,
            day_offset           INTEGER NOT NULL,
            open                 REAL,
            high                 REAL,
            low                  REAL,
            close                REAL,
            volume               INTEGER,
            transaction_amount   INTEGER,
            return_vs_pick       REAL,
            return_vs_prev_close REAL,
            vi_count             INTEGER DEFAULT 0,
            vi_first_time        TEXT,
            upper_limit_hit      INTEGER DEFAULT 0,
            lower_limit_hit      INTEGER DEFAULT 0,
            foreign_net          INTEGER,
            inst_net             INTEGER,
            individual_net       INTEGER,
            kospi_return         REAL,
            kosdaq_return        REAL,
            relative_strength    REAL,
            sector_avg_return    REAL,
            created_at           TEXT    NOT NULL,
            status               TEXT    NOT NULL DEFAULT 'pending',
            retry_count          INTEGER NOT NULL DEFAULT 0,
            event_id             INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
            UNIQUE(event_id, stock_pick_id, trading_day)
        )"""
    )

    # 기존 행 이전 (행 0개 가정, 안전)
    conn.execute(
        "INSERT INTO pick_daily_tracking_new SELECT * FROM pick_daily_tracking"
    )

    # 기존 테이블 제거 (연결된 인덱스도 자동 삭제됨)
    conn.execute("DROP TABLE pick_daily_tracking")

    # 이름 변경
    conn.execute("ALTER TABLE pick_daily_tracking_new RENAME TO pick_daily_tracking")

    # 인덱스 재생성
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdt_pick_day"
        " ON pick_daily_tracking(stock_pick_id, trading_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdt_event"
        " ON pick_daily_tracking(event_id)"
    )
