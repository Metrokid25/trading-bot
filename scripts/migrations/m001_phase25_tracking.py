"""Phase 2.5 추적 인프라 마이그레이션.

변경 내용:
  A. sector_stocks에 Phase 2.5 추적 컬럼 7개 추가 (멱등)
  B. sector_pick_events 신규 생성 (섹터 재픽업 추적)
  C. pick_daily_tracking 신규 생성 (D+0~D+20 일봉)
  D. pick_minute_raw 신규 생성 (분봉 raw)
  E. pick_daily_minute_stats 신규 생성 (분봉 집계)
  F. explosion_events 신규 생성 (+10% 폭발 마킹)
  G. 인덱스 4개 생성

모든 신규 테이블 FK: stock_pick_id → sector_stocks(id)
트랜잭션 범위는 migration_runner가 관리한다 (up() 내부에서 BEGIN/COMMIT 금지).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

VERSION = "001"
NAME = "phase25_tracking"


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """테이블에 해당 컬럼이 존재하면 True를 반환한다."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, type_def: str
) -> None:
    """컬럼이 없을 때만 ALTER TABLE로 추가한다 (멱등)."""
    if not column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")


def up(conn: sqlite3.Connection) -> None:
    """마이그레이션을 적용한다."""

    # ── A. sector_stocks 추적 컬럼 추가 ────────────────────────────────────
    # 재픽업 추적
    add_column_if_missing(conn, "sector_stocks", "is_repick",            "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "sector_stocks", "prev_pick_id",         "INTEGER")            # sector_stocks.id 자기참조
    add_column_if_missing(conn, "sector_stocks", "days_since_last_pick", "INTEGER")
    add_column_if_missing(conn, "sector_stocks", "total_pick_count",     "INTEGER DEFAULT 1")
    # D+20 추적 상태
    add_column_if_missing(conn, "sector_stocks", "tracking_status",      "TEXT DEFAULT 'active'")  # 'active' | 'completed' | 'expired'
    add_column_if_missing(conn, "sector_stocks", "tracking_start_date",  "TEXT")                   # KST ISO, 픽 등록 시점
    add_column_if_missing(conn, "sector_stocks", "tracking_end_date",    "TEXT")                   # D+20 만료 시점

    # ── B. sector_pick_events (섹터 단위 재픽업 추적) ───────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sector_pick_events (
            event_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_name                 TEXT    NOT NULL,
            registered_at_kst           TEXT    NOT NULL,
            is_sector_repick            INTEGER DEFAULT 0,
            prev_event_id               INTEGER,
            days_since_last_sector_pick INTEGER,
            total_sector_pick_count     INTEGER DEFAULT 1
        )"""
    )

    # ── C. pick_daily_tracking (D+0~D+20 일봉) ─────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pick_daily_tracking (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id        INTEGER NOT NULL REFERENCES sector_stocks(id),
            trading_day          TEXT    NOT NULL,
            day_offset           INTEGER NOT NULL,  -- 0-based: 픽 당일=0, D+1=1, ..., D+20=20
            open                 REAL,
            high                 REAL,
            low                  REAL,
            close                REAL,
            volume               INTEGER,
            transaction_amount   INTEGER,           -- 거래대금(원), SQLite INTEGER는 64bit이므로 안전
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
            created_at           TEXT    NOT NULL,  -- KST ISO, 레코드 INSERT 시각
            UNIQUE(stock_pick_id, trading_day)
        )"""
    )

    # ── D. pick_minute_raw (분봉 raw 저장) ─────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pick_minute_raw (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id      INTEGER NOT NULL REFERENCES sector_stocks(id),
            trading_day        TEXT    NOT NULL,
            bar_time           TEXT    NOT NULL,
            minute_idx         INTEGER NOT NULL,    -- 0-based 분봉 인덱스: 09:00=0, 09:01=1, ..., 15:00=360
            open               REAL,
            high               REAL,
            low                REAL,
            close              REAL,
            volume             INTEGER,
            transaction_amount INTEGER,             -- 거래대금(원), SQLite INTEGER는 64bit이므로 안전
            created_at         TEXT    NOT NULL,    -- KST ISO, 레코드 INSERT 시각
            UNIQUE(stock_pick_id, trading_day, minute_idx)
        )"""
    )

    # ── E. pick_daily_minute_stats (분봉 집계) ─────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pick_daily_minute_stats (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id      INTEGER NOT NULL REFERENCES sector_stocks(id),
            trading_day        TEXT    NOT NULL,
            bars_count         INTEGER,
            vol_ratio_max      REAL,
            vol_ratio_avg      REAL,
            vol_x3_count       INTEGER,
            vol_x5_count       INTEGER,
            vol_x10_count      INTEGER,
            max_1min_return    REAL,
            min_1min_return    REAL,
            bullish_bar_count  INTEGER,
            bearish_bar_count  INTEGER,
            morning_volume_pct REAL,
            lunch_volume_pct   REAL,
            closing_volume_pct REAL,
            created_at         TEXT    NOT NULL,  -- KST ISO, 레코드 INSERT 시각
            UNIQUE(stock_pick_id, trading_day)
        )"""
    )

    # ── F. explosion_events (+10% 폭발 마킹) ───────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS explosion_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_pick_id  INTEGER NOT NULL REFERENCES sector_stocks(id),
            explosion_day  TEXT    NOT NULL,
            day_offset     INTEGER,
            peak_return    REAL,
            peak_time      TEXT,
            created_at     TEXT    NOT NULL,  -- KST ISO, 레코드 INSERT 시각
            UNIQUE(stock_pick_id, explosion_day)
        )"""
    )

    # ── G. 인덱스 ──────────────────────────────────────────────────────────
    # pick_daily_tracking: 픽 + 날짜 (UNIQUE 제약과 동일 컬럼이나 명시적 이름 부여)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdt_pick_day"
        " ON pick_daily_tracking(stock_pick_id, trading_day)"
    )
    # pick_minute_raw: 픽 + 날짜 + minute_idx 복합 (분봉 순차 조회 패턴)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pmr_pick_day_min"
        " ON pick_minute_raw(stock_pick_id, trading_day, minute_idx)"
    )
    # explosion_events: 픽 기준 폭발 이벤트 조회
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ee_pick"
        " ON explosion_events(stock_pick_id)"
    )
    # sector_pick_events: 섹터명 + 등록시각 복합 (재픽업 판단 조회 패턴)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spe_sector_at"
        " ON sector_pick_events(sector_name, registered_at_kst)"
    )
