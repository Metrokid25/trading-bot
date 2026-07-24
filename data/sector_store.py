"""섹터 픽(스승님 워치리스트) SQLite 영속화."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

import aiosqlite
from loguru import logger

from config import settings
from core.market_calendar import add_trading_days, count_trading_days_between
from core.time_utils import now_kst, to_db_iso
from data.sector_models import PickStatus, SectorPick, SectorStock, UpsertResult


class AlertResult(Enum):
    INSERTED = "inserted"
    COOLDOWN_ACTIVE = "cooldown_active"
    INSERT_FAILED = "insert_failed"


def normalize_sector_name(value: str) -> str:
    """섹터 표시명을 정리한다. 단어 사이 공백은 하나로 유지한다."""
    return " ".join(value.split())


def sector_key(value: str) -> str:
    """대소문자와 불필요한 공백을 무시하는 섹터 식별 키."""
    return normalize_sector_name(value).casefold()


def materialize_expired_picks(db_path=None) -> int:
    """시간 경과로 만료된 active pick과 멤버십 이탈 이벤트를 함께 확정한다.

    이벤트 테이블과 상태 전환 트리거가 모두 설치된 DB에서만 갱신한다. 구버전
    DB를 새 paper runner가 먼저 열어 이벤트 없이 상태만 바꾸지 않기 위해서다.
    """
    path = str(db_path or settings.DB_PATH)
    con = sqlite3.connect(path, timeout=15)
    try:
        ready = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE "
            "(type='table' AND name='universe_membership_events') OR "
            "(type='trigger' AND name='trg_universe_pick_off')"
        ).fetchone()[0]
        if ready != 2:
            return 0
        cur = con.execute(
            "UPDATE sector_picks SET status=? "
            "WHERE status=? AND expires_at<=?",
            (PickStatus.EXPIRED.value, PickStatus.ACTIVE.value,
             to_db_iso(now_kst())),
        )
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


class SectorStore:
    """픽 이벤트 + 섹터-종목 매핑 저장소."""

    def __init__(self, db_path=None) -> None:
        self.db_path = str(db_path or settings.DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        # isolation_level=None: 자동 트랜잭션 비활성. BEGIN/COMMIT/ROLLBACK을 명시적으로 관리.
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None)
        await self._db.create_function("sector_key", 1, sector_key, deterministic=True)
        await self.init_tables()
        await self._migrate_alert_history_v2()
        await self._migrate_sector_stocks_repick()
        # 레거시 sector_stocks 컬럼 마이그레이션 후에 트리거/부트스트랩을 설치한다.
        await self._create_universe_event_triggers()
        await self._bootstrap_universe_membership_events()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def init_tables(self) -> None:
        if not self._db:
            return
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS sector_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_input TEXT DEFAULT ''
            )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS sector_stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_id INTEGER NOT NULL,
                sector_name TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                added_order INTEGER NOT NULL,
                is_repick INTEGER NOT NULL DEFAULT 0,
                prev_pick_id INTEGER,
                days_since_last_pick INTEGER,
                total_pick_count INTEGER NOT NULL DEFAULT 1,
                tracking_status TEXT DEFAULT 'active',
                tracking_start_date TEXT,
                tracking_end_date TEXT,
                FOREIGN KEY (pick_id) REFERENCES sector_picks(id)
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_picks_status_expires "
            "ON sector_picks (status, expires_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stocks_pick_sector "
            "ON sector_stocks (pick_id, sector_name)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stocks_code "
            "ON sector_stocks (stock_code)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stocks_dup_check "
            "ON sector_stocks (pick_id, sector_name, stock_code)"
        )
        # 페이퍼 forward의 실제 편입/이탈 경계. snapshot overwrite가 아니라
        # 코드 단위 active 상태 전환만 append-only로 남긴다.
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS universe_membership_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('activate','deactivate')),
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                source TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_universe_events_code_time "
            "ON universe_membership_events(stock_code, occurred_at, id)"
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_name TEXT NOT NULL,
                stage INTEGER NOT NULL,
                triggered_at TEXT NOT NULL,
                passed_stocks TEXT NOT NULL,
                metrics TEXT NOT NULL,
                threshold_used TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(delivery_status IN ('pending','sent','failed','disabled','crashed'))
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_sector_time "
            "ON alert_history (sector_name, triggered_at)"
        )

    async def _create_universe_event_triggers(self) -> None:
        """active 코드 집합의 경계만 기록하는 SQLite 트리거를 설치한다."""
        if not self._db:
            return
        trigger_names = (
            "trg_universe_stock_insert", "trg_universe_stock_delete",
            "trg_universe_tracking_off", "trg_universe_tracking_on",
            "trg_universe_pick_off", "trg_universe_pick_on",
            "trg_universe_pick_expiry_off", "trg_universe_pick_expiry_on",
            "trg_universe_stock_identity_off", "trg_universe_stock_identity_on",
            "trg_universe_pick_delete",
        )
        for name in trigger_names:
            await self._db.execute(f"DROP TRIGGER IF EXISTS {name}")
        active_other_new = (
            "SELECT 1 FROM sector_stocks x "
            "JOIN sector_picks p ON p.id=x.pick_id "
            "WHERE x.stock_code=NEW.stock_code AND x.id!=NEW.id "
            "AND COALESCE(x.tracking_status,'active')='active' "
            "AND p.status='active' "
            "AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')"
        )
        active_other_old = (
            "SELECT 1 FROM sector_stocks x "
            "JOIN sector_picks p ON p.id=x.pick_id "
            "WHERE x.stock_code=OLD.stock_code "
            "AND COALESCE(x.tracking_status,'active')='active' "
            "AND p.status='active' "
            "AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')"
        )
        statements = [
            """CREATE TRIGGER IF NOT EXISTS trg_universe_stock_insert
               AFTER INSERT ON sector_stocks
               WHEN COALESCE(NEW.tracking_status,'active')='active'
                AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=NEW.pick_id
                           AND p.status='active'
                           AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                AND NOT EXISTS(""" + active_other_new + """)
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'activate',NEW.stock_code,NEW.stock_name,'stock_insert');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_stock_delete
               AFTER DELETE ON sector_stocks
               WHEN COALESCE(OLD.tracking_status,'active')='active'
                AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=OLD.pick_id
                           AND p.status='active'
                           AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                AND NOT EXISTS(""" + active_other_old + """)
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'deactivate',OLD.stock_code,OLD.stock_name,'stock_delete');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_tracking_off
               AFTER UPDATE OF tracking_status ON sector_stocks
               WHEN COALESCE(OLD.tracking_status,'active')='active'
                AND COALESCE(NEW.tracking_status,'active')!='active'
                AND NOT EXISTS(""" + active_other_new + """)
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'deactivate',NEW.stock_code,NEW.stock_name,'tracking_off');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_tracking_on
               AFTER UPDATE OF tracking_status ON sector_stocks
               WHEN COALESCE(OLD.tracking_status,'active')!='active'
                AND COALESCE(NEW.tracking_status,'active')='active'
                AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=NEW.pick_id
                           AND p.status='active'
                           AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                AND NOT EXISTS(""" + active_other_new + """)
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'activate',NEW.stock_code,NEW.stock_name,'tracking_on');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_pick_off
               AFTER UPDATE OF status ON sector_picks
               WHEN OLD.status='active' AND NEW.status!='active'
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT CASE WHEN NEW.status='expired' THEN (
                            SELECT MAX(expiry_at) FROM (
                              SELECT OLD.expires_at AS expiry_at
                              UNION ALL
                              SELECT p2.expires_at
                              FROM sector_stocks x2
                              JOIN sector_picks p2 ON p2.id=x2.pick_id
                              WHERE x2.stock_code=s.stock_code
                               AND COALESCE(x2.tracking_status,'active')='active'
                               AND p2.status IN ('active','expired')
                               AND p2.expires_at<=strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
                               AND p2.expires_at>=COALESCE((
                                 SELECT MAX(e2.occurred_at)
                                 FROM universe_membership_events e2
                                 WHERE e2.stock_code=s.stock_code
                                  AND e2.action='activate'
                               ),'')
                            )
                          )
                             ELSE strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours') END,
                        'deactivate',s.stock_code,MAX(s.stock_name),
                        CASE WHEN NEW.status='expired' THEN 'pick_expired'
                             ELSE 'pick_off' END
                 FROM sector_stocks s WHERE s.pick_id=NEW.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND COALESCE((
                    SELECT e.action FROM universe_membership_events e
                    WHERE e.stock_code=s.stock_code ORDER BY e.id DESC LIMIT 1
                  ),'deactivate')='activate'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=NEW.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_pick_on
               AFTER UPDATE OF status ON sector_picks
               WHEN OLD.status!='active' AND NEW.status='active'
                AND NEW.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'activate',s.stock_code,MAX(s.stock_name),'pick_on'
                 FROM sector_stocks s WHERE s.pick_id=NEW.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=NEW.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_pick_expiry_off
               AFTER UPDATE OF expires_at ON sector_picks
               WHEN NEW.status='active'
                AND OLD.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
                AND NEW.expires_at<=strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'deactivate',s.stock_code,MAX(s.stock_name),'expiry_shortened'
                 FROM sector_stocks s WHERE s.pick_id=NEW.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=NEW.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_pick_expiry_on
               AFTER UPDATE OF expires_at ON sector_picks
               WHEN NEW.status='active'
                AND OLD.expires_at<=strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
                AND NEW.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT (
                          SELECT MAX(expiry_at) FROM (
                            SELECT OLD.expires_at AS expiry_at
                            UNION ALL
                            SELECT p2.expires_at
                            FROM sector_stocks x2
                            JOIN sector_picks p2 ON p2.id=x2.pick_id
                            WHERE x2.stock_code=s.stock_code
                             AND COALESCE(x2.tracking_status,'active')='active'
                             AND p2.status IN ('active','expired')
                             AND p2.expires_at<=strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
                             AND p2.expires_at>=COALESCE((
                               SELECT MAX(e2.occurred_at)
                               FROM universe_membership_events e2
                               WHERE e2.stock_code=s.stock_code
                                AND e2.action='activate'
                             ),'')
                          )
                        ),
                        'deactivate',s.stock_code,MAX(s.stock_name),
                        'expiry_elapsed_before_extension'
                 FROM sector_stocks s WHERE s.pick_id=NEW.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND COALESCE((
                    SELECT e.action FROM universe_membership_events e
                    WHERE e.stock_code=s.stock_code ORDER BY e.id DESC LIMIT 1
                  ),'deactivate')='activate'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=NEW.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'activate',s.stock_code,MAX(s.stock_name),'expiry_extended'
                 FROM sector_stocks s WHERE s.pick_id=NEW.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=NEW.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_stock_identity_off
               AFTER UPDATE OF pick_id,stock_code ON sector_stocks
               WHEN COALESCE(OLD.tracking_status,'active')='active'
                AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=OLD.pick_id
                           AND p.status='active'
                           AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                AND NOT EXISTS(SELECT 1 FROM sector_stocks x
                    JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=OLD.stock_code
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'deactivate',OLD.stock_code,OLD.stock_name,'stock_identity_off');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_stock_identity_on
               AFTER UPDATE OF pick_id,stock_code ON sector_stocks
               WHEN COALESCE(NEW.tracking_status,'active')='active'
                AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=NEW.pick_id
                           AND p.status='active'
                           AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                AND NOT (OLD.stock_code=NEW.stock_code
                         AND COALESCE(OLD.tracking_status,'active')='active'
                         AND EXISTS(SELECT 1 FROM sector_picks p WHERE p.id=OLD.pick_id
                                    AND p.status='active'
                                    AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')))
                AND NOT EXISTS(""" + active_other_new + """)
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 VALUES(strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'activate',NEW.stock_code,NEW.stock_name,'stock_identity_on');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_universe_pick_delete
               BEFORE DELETE ON sector_picks
               WHEN OLD.status='active'
                AND OLD.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')
               BEGIN
                 INSERT INTO universe_membership_events
                   (occurred_at,action,stock_code,stock_name,source)
                 SELECT strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'),
                        'deactivate',s.stock_code,MAX(s.stock_name),'pick_delete'
                 FROM sector_stocks s WHERE s.pick_id=OLD.id
                  AND COALESCE(s.tracking_status,'active')='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM sector_stocks x JOIN sector_picks p ON p.id=x.pick_id
                    WHERE x.stock_code=s.stock_code AND x.pick_id!=OLD.id
                     AND COALESCE(x.tracking_status,'active')='active'
                     AND p.status='active'
                     AND p.expires_at>strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours'))
                 GROUP BY s.stock_code;
               END""",
        ]
        for sql in statements:
            await self._db.execute(sql)

    async def _bootstrap_universe_membership_events(self) -> None:
        """기존 DB 최초 도입 시 현재 active 코드만 '지금'부터 관찰 시작한다."""
        if not self._db:
            return
        row = await (await self._db.execute(
            "SELECT COUNT(*) FROM universe_membership_events")).fetchone()
        if row and row[0]:
            return
        now_iso = to_db_iso(now_kst())
        await self._db.execute(
            "INSERT INTO universe_membership_events "
            "(occurred_at,action,stock_code,stock_name,source) "
            "SELECT ?, 'activate', ss.stock_code, MAX(ss.stock_name), 'bootstrap' "
            "FROM sector_stocks ss JOIN sector_picks sp ON sp.id=ss.pick_id "
            "WHERE sp.status='active' AND sp.expires_at>? "
            "AND COALESCE(ss.tracking_status,'active')='active' "
            "GROUP BY ss.stock_code",
            (now_iso, now_iso),
        )

    async def _migrate_alert_history_v2(self) -> None:
        """alert_history에 delivery_status 컬럼 추가 (멱등). 기존 행은 'sent'로 백필."""
        if not self._db:
            return
        cur = await self._db.execute("PRAGMA table_info(alert_history)")
        rows = await cur.fetchall()
        if not rows:
            return  # 테이블 미존재 — init_tables가 새 스키마로 생성
        col_names = [row[1] for row in rows]
        if 'delivery_status' in col_names:
            return  # 이미 마이그레이션 완료
        logger.info("[sector_store] alert_history v2 마이그레이션 시작")
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            await self._db.execute(
                """CREATE TABLE IF NOT EXISTS alert_history_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_name TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    triggered_at TEXT NOT NULL,
                    passed_stocks TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    threshold_used TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(delivery_status IN ('pending','sent','failed','disabled','crashed'))
                )"""
            )
            await self._db.execute(
                "INSERT INTO alert_history_new "
                "(id, sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used, delivery_status) "
                "SELECT id, sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used, 'sent' "
                "FROM alert_history"
            )
            await self._db.execute("DROP TABLE alert_history")
            await self._db.execute("ALTER TABLE alert_history_new RENAME TO alert_history")
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_sector_time "
                "ON alert_history (sector_name, triggered_at)"
            )
            await self._db.execute("COMMIT")
            logger.info("[sector_store] alert_history v2 마이그레이션 완료")
        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("[sector_store] alert_history v2 마이그레이션 실패")
            raise

    async def _migrate_sector_stocks_repick(self) -> None:
        """sector_stocks에 재픽업 마킹 7개 컬럼 추가 (멱등). 신규 DB는 init_tables가 처리."""
        if not self._db:
            return
        cur = await self._db.execute("PRAGMA table_info(sector_stocks)")
        rows = await cur.fetchall()
        if not rows:
            return
        existing_cols = {row[1] for row in rows}
        if "is_repick" in existing_cols:
            return
        logger.info("[sector_store] sector_stocks repick 마이그레이션 시작")
        for col_name, col_def in [
            ("is_repick", "INTEGER NOT NULL DEFAULT 0"),
            ("prev_pick_id", "INTEGER"),
            ("days_since_last_pick", "INTEGER"),
            ("total_pick_count", "INTEGER NOT NULL DEFAULT 1"),
            ("tracking_status", "TEXT DEFAULT 'active'"),
            ("tracking_start_date", "TEXT"),
            ("tracking_end_date", "TEXT"),
        ]:
            await self._db.execute(
                f"ALTER TABLE sector_stocks ADD COLUMN {col_name} {col_def}"
            )
        logger.info("[sector_store] sector_stocks repick 마이그레이션 완료")

    async def _record_sector_pick_event(
        self,
        sector_name: str,
        registered_at_kst: str,
        pick_date: date,
        pick_id: int,
    ) -> int:
        """sector_pick_events에 이벤트를 INSERT하고 새 event_id를 반환한다.

        호출자의 트랜잭션 안에서 실행된다 — 자체 commit 금지.
        gap 계산 기준: 직전 이벤트의 pick_date (NULL이면 첫 픽으로 처리).
        """
        cur = await self._db.execute(
            "SELECT event_id, pick_date "
            "FROM sector_pick_events "
            "WHERE sector_key(sector_name) = ? AND pick_date IS NOT NULL AND pick_date < ? "
            "ORDER BY pick_date DESC, event_id DESC LIMIT 1",
            (sector_key(sector_name), pick_date.isoformat()),
        )
        prev_row = await cur.fetchone()

        cur_total = await self._db.execute(
            "SELECT COUNT(*) "
            "FROM sector_pick_events WHERE sector_key(sector_name) = ?",
            (sector_key(sector_name),),
        )
        total_count = (await cur_total.fetchone())[0] + 1

        if prev_row is None:
            is_sector_repick = 0
            prev_event_id = None
            days_since = None
            trading_days_since = None
        else:
            prev_event_id, prev_pick_date_str = prev_row
            is_sector_repick = 1
            prev_pick_date = date.fromisoformat(prev_pick_date_str)
            days_since = (pick_date - prev_pick_date).days
            trading_days_since = count_trading_days_between(prev_pick_date, pick_date)

        cur2 = await self._db.execute(
            "INSERT INTO sector_pick_events "
            "(pick_id, sector_name, registered_at_kst, pick_date, is_sector_repick, prev_event_id, "
            "days_since_last_sector_pick, trading_days_since_last_sector_pick, "
            "total_sector_pick_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pick_id, sector_name, registered_at_kst, pick_date.isoformat(), is_sector_repick,
             prev_event_id, days_since, trading_days_since, total_count),
        )
        event_id = cur2.lastrowid
        if event_id is None:
            raise RuntimeError("lastrowid missing after sector_pick_events insert")
        return event_id

    async def _normalize_pick_event_sector_name(
        self, pick_id: int, key: str, canonical_name: str
    ) -> None:
        """해당 Pick의 이벤트 섹터명을 맞춘다. Phase2.5 테이블이 없으면 no-op."""
        cur = await self._db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sector_pick_events'"
        )
        if await cur.fetchone() is None:
            return
        await self._db.execute(
            "UPDATE sector_pick_events SET sector_name = ? "
            "WHERE pick_id = ? AND sector_key(sector_name) = ?",
            (canonical_name, pick_id, key),
        )

    async def _compute_repick_metadata(
        self,
        stock_code: str,
        pick_date_str: str,
    ) -> dict:
        """직전 픽(stock_code 기준, 모든 섹터 포함) 조회 후 재픽업 메타데이터 계산."""
        cur = await self._db.execute(
            "SELECT ss.id, ss.total_pick_count, sp.pick_date "
            "FROM sector_stocks ss "
            "JOIN sector_picks sp ON ss.pick_id = sp.id "
            "WHERE ss.stock_code = ? "
            "ORDER BY sp.pick_date DESC, sp.created_at DESC "
            "LIMIT 1",
            (stock_code,),
        )
        row = await cur.fetchone()
        if row is None:
            return {
                "is_repick": 0,
                "prev_pick_id": None,
                "days_since_last_pick": None,
                "total_pick_count": 1,
            }
        prev_ss_id, prev_total_count, prev_pick_date = row
        days_since = (date.fromisoformat(pick_date_str) - date.fromisoformat(prev_pick_date)).days
        return {
            "is_repick": 1,
            "prev_pick_id": prev_ss_id,
            "days_since_last_pick": days_since,
            "total_pick_count": prev_total_count + 1,
        }

    async def insert_pick(
        self,
        pick: SectorPick,
        stocks: list[SectorStock],
    ) -> int:
        if not self._db:
            raise RuntimeError("SectorStore not open")

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._db.execute(
                "INSERT INTO sector_picks "
                "(pick_date, created_at, expires_at, status, raw_input) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    pick.pick_date,
                    to_db_iso(pick.created_at),
                    to_db_iso(pick.expires_at),
                    pick.status.value,
                    pick.raw_input,
                ),
            )
            pick_id = cur.lastrowid
            if pick_id is None:
                raise RuntimeError("lastrowid missing after sector_picks insert")

            if stocks:
                await self._db.executemany(
                    "INSERT INTO sector_stocks "
                    "(pick_id, sector_name, stock_code, stock_name, added_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (pick_id, s.sector_name, s.stock_code, s.stock_name, s.added_order)
                        for s in stocks
                    ],
                )
            await self._db.execute("COMMIT")
        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("insert_pick failed, rolled back (pick_date=%s)", pick.pick_date)
            raise

        pick.id = pick_id
        return pick_id

    async def upsert_sector(
        self,
        sector_name: str,
        stocks: list[SectorStock],
        pick_template: SectorPick,
        record_pick_event: bool = False,
    ) -> UpsertResult:
        """섹터 단위 UPSERT: 대소문자를 무시한 동일 활성 섹터에 종목을 추가한다."""
        if not self._db:
            raise RuntimeError("SectorStore not open")

        requested_sector_name = normalize_sector_name(sector_name)
        if not requested_sector_name:
            raise ValueError("sector_name must not be blank")
        normalized_key = sector_key(requested_sector_name)
        now_iso = to_db_iso(now_kst())
        pick_created_at_iso = to_db_iso(pick_template.created_at)
        tracking_end_date_iso = add_trading_days(pick_template.created_at.date(), 20).isoformat()
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._db.execute(
                "SELECT ss.pick_id, ss.sector_name FROM sector_stocks ss "
                "JOIN sector_picks sp ON sp.id = ss.pick_id "
                "WHERE sector_key(ss.sector_name) = ? AND sp.status = ? AND sp.expires_at > ? "
                "ORDER BY sp.created_at DESC LIMIT 1",
                (normalized_key, PickStatus.ACTIVE.value, now_iso),
            )
            row = await cur.fetchone()

            if row:
                pick_id: int = row[0]
                canonical_sector_name: str = normalize_sector_name(row[1])
                is_new_pick = False

                # 기존 공백 변형도 같은 표시명으로 맞추고, 해당 Pick의 이벤트명도
                # 함께 바꿔 event↔stock 정확 일치 조인을 유지한다.
                await self._db.execute(
                    "UPDATE sector_stocks SET sector_name = ? "
                    "WHERE pick_id = ? AND sector_key(sector_name) = ?",
                    (canonical_sector_name, pick_id, normalized_key),
                )
                await self._normalize_pick_event_sector_name(
                    pick_id, normalized_key, canonical_sector_name
                )

                cur2 = await self._db.execute(
                    "SELECT stock_code FROM sector_stocks "
                    "WHERE pick_id = ? AND sector_key(sector_name) = ?",
                    (pick_id, normalized_key),
                )
                existing_codes = {r[0] for r in await cur2.fetchall()}

                cur3 = await self._db.execute(
                    "SELECT COALESCE(MAX(added_order), 0) FROM sector_stocks WHERE pick_id = ?",
                    (pick_id,),
                )
                max_order = (await cur3.fetchone())[0]

                added: list[SectorStock] = []
                skipped: list[SectorStock] = []
                seen_new: set[str] = set()
                for s in stocks:
                    s.sector_name = canonical_sector_name
                    if s.stock_code in existing_codes or s.stock_code in seen_new:
                        skipped.append(s)
                    else:
                        max_order += 1
                        s.added_order = max_order
                        meta = await self._compute_repick_metadata(s.stock_code, pick_template.pick_date)
                        s.is_repick = meta["is_repick"]
                        s.prev_pick_id = meta["prev_pick_id"]
                        s.days_since_last_pick = meta["days_since_last_pick"]
                        s.total_pick_count = meta["total_pick_count"]
                        s.tracking_status = "active"
                        s.tracking_start_date = pick_created_at_iso
                        s.tracking_end_date = tracking_end_date_iso
                        seen_new.add(s.stock_code)
                        added.append(s)

                if added:
                    await self._db.executemany(
                        "INSERT INTO sector_stocks "
                        "(pick_id, sector_name, stock_code, stock_name, added_order, "
                        "is_repick, prev_pick_id, days_since_last_pick, total_pick_count, "
                        "tracking_status, tracking_start_date, tracking_end_date) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(pick_id, s.sector_name, s.stock_code, s.stock_name, s.added_order,
                          s.is_repick, s.prev_pick_id, s.days_since_last_pick, s.total_pick_count,
                          s.tracking_status, s.tracking_start_date, s.tracking_end_date)
                         for s in added],
                    )

                total = len(existing_codes) + len(added)

            else:
                canonical_sector_name = requested_sector_name
                is_new_pick = True
                skipped = []

                cur4 = await self._db.execute(
                    "INSERT INTO sector_picks "
                    "(pick_date, created_at, expires_at, status, raw_input) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        pick_template.pick_date,
                        to_db_iso(pick_template.created_at),
                        to_db_iso(pick_template.expires_at),
                        pick_template.status.value,
                        pick_template.raw_input,
                    ),
                )
                pick_id = cur4.lastrowid
                if pick_id is None:
                    raise RuntimeError("lastrowid missing after sector_picks insert")

                seen_in_batch: set[str] = set()
                stocks_deduped: list[SectorStock] = []
                for s in stocks:
                    s.sector_name = canonical_sector_name
                    if s.stock_code not in seen_in_batch:
                        seen_in_batch.add(s.stock_code)
                        stocks_deduped.append(s)

                for i, s in enumerate(stocks_deduped, start=1):
                    s.added_order = i
                    meta = await self._compute_repick_metadata(s.stock_code, pick_template.pick_date)
                    s.is_repick = meta["is_repick"]
                    s.prev_pick_id = meta["prev_pick_id"]
                    s.days_since_last_pick = meta["days_since_last_pick"]
                    s.total_pick_count = meta["total_pick_count"]
                    s.tracking_status = "active"
                    s.tracking_start_date = pick_created_at_iso
                    s.tracking_end_date = tracking_end_date_iso

                if stocks_deduped:
                    await self._db.executemany(
                        "INSERT INTO sector_stocks "
                        "(pick_id, sector_name, stock_code, stock_name, added_order, "
                        "is_repick, prev_pick_id, days_since_last_pick, total_pick_count, "
                        "tracking_status, tracking_start_date, tracking_end_date) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(pick_id, s.sector_name, s.stock_code, s.stock_name, s.added_order,
                          s.is_repick, s.prev_pick_id, s.days_since_last_pick, s.total_pick_count,
                          s.tracking_status, s.tracking_start_date, s.tracking_end_date)
                         for s in stocks_deduped],
                    )

                added = list(stocks_deduped)
                total = len(stocks_deduped)

            await self._db.execute("COMMIT")

        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("upsert_sector failed, rolled back (sector=%s)", sector_name)
            raise

        # 본 트랜잭션 COMMIT 성공 후, 이벤트 기록은 best-effort (실패해도 픽 저장 유지)
        if record_pick_event:
            try:
                await self._db.execute("BEGIN")
                await self._record_sector_pick_event(
                    canonical_sector_name,
                    pick_created_at_iso,
                    date.fromisoformat(pick_template.pick_date),
                    pick_id,
                )
                await self._db.execute("COMMIT")
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                logger.warning(
                    "sector_pick_event 기록 실패 (sector=%s) — 픽 저장은 유지됨",
                    sector_name,
                    exc_info=True,
                )

        return UpsertResult(
            pick_id=pick_id,
            is_new_pick=is_new_pick,
            added_count=len(added),
            skipped_stocks=skipped,
            total_count=total,
        )

    async def consolidate_case_insensitive_sectors(self) -> dict[str, dict]:
        """대소문자만 다른 활성 섹터 픽을 최신 표기/픽으로 통합한다.

        웹 등록은 섹터별로 별도 Pick을 만들므로, 다른 섹터가 섞이지 않은 Pick만
        자동 통합한다. 이전 Pick의 종목 행은 과거 이벤트·추적 연결 보존을 위해
        그대로 두고, 최신 활성 Pick에 없는 종목만 새 행으로 복제한다. 같은 종목은
        최신 Pick의 행을 유지하고 이전 Pick을 archive해 활성 유니버스 중복을 제거한다.
        """
        if not self._db:
            raise RuntimeError("SectorStore not open")

        now_iso = to_db_iso(now_kst())
        cur = await self._db.execute(
            "SELECT ss.id, ss.pick_id, ss.sector_name, ss.stock_code, sp.created_at, "
            "ss.stock_name, ss.is_repick, ss.prev_pick_id, ss.days_since_last_pick, "
            "ss.total_pick_count, ss.tracking_status, ss.tracking_start_date, "
            "ss.tracking_end_date "
            "FROM sector_stocks ss "
            "JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sp.status = ? AND sp.expires_at > ? "
            "ORDER BY sp.created_at DESC, ss.id ASC",
            (PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()

        grouped: dict[str, list[tuple]] = {}
        keys_by_pick: dict[int, set[str]] = {}
        for row in rows:
            stock_id, pick_id, name, code, created_at = row[:5]
            key = sector_key(name)
            grouped.setdefault(key, []).append(row)
            keys_by_pick.setdefault(pick_id, set()).add(key)

        results: dict[str, dict] = {}
        for key, group_rows in grouped.items():
            pick_ids = list(dict.fromkeys(row[1] for row in group_rows))
            spellings = {normalize_sector_name(row[2]) for row in group_rows}
            has_unclean_name = any(row[2] != normalize_sector_name(row[2]) for row in group_rows)
            # 같은 표기의 여러 Pick은 정상적인 재픽업 이력일 수 있다. 자동 병합은
            # 대소문자 표기가 실제로 갈렸거나 공백 정리가 필요한 그룹에만 한정한다.
            if len(spellings) < 2 and not has_unclean_name:
                continue

            target_id = pick_ids[0]
            canonical_name = normalize_sector_name(group_rows[0][2])
            duplicate_ids = pick_ids[1:]

            picks_by_spelling: dict[str, set[int]] = {}
            for row in group_rows:
                picks_by_spelling.setdefault(
                    normalize_sector_name(row[2]), set()
                ).add(row[1])
            if any(len(ids) > 1 for ids in picks_by_spelling.values()):
                logger.warning(
                    "대소문자 중복 섹터 자동 병합 보류: 정상 재픽 이력과 혼재 "
                    "(sector=%s, spellings=%s)",
                    canonical_name,
                    {name: sorted(ids) for name, ids in picks_by_spelling.items()},
                )
                continue

            # 여러 섹터가 한 Pick에 섞인 레거시 데이터는 전체 Pick archive가
            # 다른 섹터까지 숨길 수 있으므로 자동 병합하지 않는다.
            if any(keys_by_pick[pick_id] != {key} for pick_id in duplicate_ids):
                logger.warning(
                    "대소문자 중복 섹터 자동 병합 보류: 다른 섹터가 섞인 pick (sector=%s, picks=%s)",
                    canonical_name,
                    duplicate_ids,
                )
                continue

            await self._db.execute("BEGIN IMMEDIATE")
            try:
                await self._db.execute(
                    "UPDATE sector_stocks SET sector_name = ? "
                    "WHERE pick_id = ? AND sector_key(sector_name) = ?",
                    (canonical_name, target_id, key),
                )
                await self._normalize_pick_event_sector_name(
                    target_id, key, canonical_name
                )
                cur_existing = await self._db.execute(
                    "SELECT stock_code FROM sector_stocks "
                    "WHERE pick_id = ? AND sector_key(sector_name) = ?",
                    (target_id, key),
                )
                existing_codes = {row[0] for row in await cur_existing.fetchall()}
                cur_order = await self._db.execute(
                    "SELECT COALESCE(MAX(added_order), 0) FROM sector_stocks WHERE pick_id = ?",
                    (target_id,),
                )
                next_order = (await cur_order.fetchone())[0]

                copied = 0
                for row in group_rows:
                    (
                        _stock_id,
                        pick_id,
                        _name,
                        code,
                        _created_at,
                        stock_name,
                        is_repick,
                        prev_pick_id,
                        days_since_last_pick,
                        total_pick_count,
                        tracking_status,
                        tracking_start_date,
                        tracking_end_date,
                    ) = row
                    if pick_id == target_id or code in existing_codes:
                        continue
                    next_order += 1
                    await self._db.execute(
                        "INSERT INTO sector_stocks "
                        "(pick_id, sector_name, stock_code, stock_name, added_order, "
                        "is_repick, prev_pick_id, days_since_last_pick, total_pick_count, "
                        "tracking_status, tracking_start_date, tracking_end_date) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            target_id,
                            canonical_name,
                            code,
                            stock_name,
                            next_order,
                            is_repick,
                            prev_pick_id,
                            days_since_last_pick,
                            total_pick_count,
                            tracking_status,
                            tracking_start_date,
                            tracking_end_date,
                        ),
                    )
                    existing_codes.add(code)
                    copied += 1

                await self._db.executemany(
                    "UPDATE sector_picks SET status = ? WHERE id = ?",
                    [(PickStatus.ARCHIVED.value, pick_id) for pick_id in duplicate_ids],
                )
                await self._db.execute("COMMIT")
            except Exception:
                await self._db.execute("ROLLBACK")
                logger.exception(
                    "대소문자 중복 섹터 병합 실패 (sector=%s)", canonical_name
                )
                raise

            results[canonical_name] = {
                "target_id": target_id,
                "merged_ids": duplicate_ids,
                "copied_stocks": copied,
                "total_stocks": len(existing_codes),
            }
            logger.info(
                "대소문자 중복 섹터 통합: sector=%s target=%s merged=%s total=%s",
                canonical_name,
                target_id,
                duplicate_ids,
                len(existing_codes),
            )

        return results

    async def get_active_picks(self) -> list[SectorPick]:
        if not self._db:
            return []
        # 조회 전 자동 만료 처리
        await self.expire_old_picks()
        now_iso = to_db_iso(now_kst())
        cur = await self._db.execute(
            "SELECT id, pick_date, created_at, expires_at, status, raw_input "
            "FROM sector_picks "
            "WHERE status = ? AND expires_at > ? "
            "ORDER BY created_at DESC",
            (PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()
        return [
            SectorPick(
                id=r[0],
                pick_date=r[1],
                created_at=datetime.fromisoformat(r[2]),
                expires_at=datetime.fromisoformat(r[3]),
                status=PickStatus(r[4]),
                raw_input=r[5] or "",
            )
            for r in rows
        ]

    async def get_stocks_by_pick(self, pick_id: int) -> list[SectorStock]:
        if not self._db:
            return []
        cur = await self._db.execute(
            "SELECT id, pick_id, sector_name, stock_code, stock_name, added_order, "
            "is_repick, prev_pick_id, days_since_last_pick, total_pick_count, "
            "tracking_status, tracking_start_date, tracking_end_date "
            "FROM sector_stocks WHERE pick_id = ? "
            "ORDER BY added_order",
            (pick_id,),
        )
        rows = await cur.fetchall()
        return [
            SectorStock(
                id=r[0],
                pick_id=r[1],
                sector_name=r[2],
                stock_code=r[3],
                stock_name=r[4],
                added_order=r[5],
                is_repick=r[6] or 0,
                prev_pick_id=r[7],
                days_since_last_pick=r[8],
                total_pick_count=r[9] or 1,
                tracking_status=r[10] or "active",
                tracking_start_date=r[11],
                tracking_end_date=r[12],
            )
            for r in rows
        ]

    async def get_stocks_by_sector(
        self, pick_id: int, sector_name: str
    ) -> list[SectorStock]:
        if not self._db:
            return []
        cur = await self._db.execute(
            "SELECT id, pick_id, sector_name, stock_code, stock_name, added_order, "
            "is_repick, prev_pick_id, days_since_last_pick, total_pick_count, "
            "tracking_status, tracking_start_date, tracking_end_date "
            "FROM sector_stocks WHERE pick_id = ? AND sector_key(sector_name) = ? "
            "ORDER BY added_order",
            (pick_id, sector_key(sector_name)),
        )
        rows = await cur.fetchall()
        return [
            SectorStock(
                id=r[0],
                pick_id=r[1],
                sector_name=r[2],
                stock_code=r[3],
                stock_name=r[4],
                added_order=r[5],
                is_repick=r[6] or 0,
                prev_pick_id=r[7],
                days_since_last_pick=r[8],
                total_pick_count=r[9] or 1,
                tracking_status=r[10] or "active",
                tracking_start_date=r[11],
                tracking_end_date=r[12],
            )
            for r in rows
        ]

    async def expire_old_picks(self) -> int:
        if not self._db:
            return 0
        now_iso = to_db_iso(now_kst())
        cur = await self._db.execute(
            "UPDATE sector_picks SET status = ? "
            "WHERE status = ? AND expires_at <= ?",
            (PickStatus.EXPIRED.value, PickStatus.ACTIVE.value, now_iso),
        )
        return cur.rowcount or 0

    async def ensure_pick_expiry(self, pick_id: int, min_days: int) -> None:
        """expires_at이 now+min_days 미만이면 그 시점까지 연장 (이미 더 길면 무변경).

        웹앱 등록 유니버스는 '삭제 전까지 상시 유지'가 운영 규칙(2026-07-10 오너)
        — 기존 활성 섹터에 종목을 추가할 때 낡은 만료시각이 그대로 남아
        유니버스가 장중 증발하는 사고를 막는다.
        """
        if not self._db:
            return
        target = to_db_iso(now_kst() + timedelta(days=min_days))
        await self._db.execute(
            "UPDATE sector_picks SET expires_at = ? WHERE id = ? AND expires_at < ?",
            (target, pick_id, target),
        )

    async def extend_pick(self, pick_id: int, days: int) -> None:
        if not self._db:
            return
        cur = await self._db.execute(
            "SELECT expires_at FROM sector_picks WHERE id = ?",
            (pick_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"sector_picks id={pick_id} not found")
        new_expires = datetime.fromisoformat(row[0]) + timedelta(days=days)
        await self._db.execute(
            "UPDATE sector_picks SET expires_at = ? WHERE id = ?",
            (new_expires.isoformat(), pick_id),
        )

    async def archive_pick(self, pick_id: int) -> None:
        if not self._db:
            return
        await self._db.execute(
            "UPDATE sector_picks SET status = ? WHERE id = ?",
            (PickStatus.ARCHIVED.value, pick_id),
        )

    async def get_sector_picks_info(self, sector_name: str) -> list[dict]:
        """해당 섹터명이 있는 active Pick들의 종목 수 반환 (미리보기 전용).

        Returns: [{"pick_id": 13, "sector_stock_count": 2, "other_stock_count": 3}, ...]
        created_at ASC 순.
        """
        if not self._db:
            return []
        now_iso = to_db_iso(now_kst())
        cur = await self._db.execute(
            "SELECT ss.pick_id, COUNT(ss.id) "
            "FROM sector_stocks ss "
            "JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sector_key(ss.sector_name) = ? AND sp.status = ? AND sp.expires_at > ? "
            "GROUP BY ss.pick_id "
            "ORDER BY sp.created_at ASC",
            (sector_key(sector_name), PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()
        result = []
        for pick_id, sector_cnt in rows:
            cur2 = await self._db.execute(
                "SELECT COUNT(*) FROM sector_stocks "
                "WHERE pick_id = ? AND sector_key(sector_name) != ?",
                (pick_id, sector_key(sector_name)),
            )
            other_cnt = (await cur2.fetchone())[0]
            result.append({
                "pick_id": pick_id,
                "sector_stock_count": sector_cnt,
                "other_stock_count": other_cnt,
            })
        return result

    async def archive_sector(self, sector_name: str) -> dict:
        """해당 섹터의 종목만 DELETE → 빈 Pick은 자동 archive. 다른 섹터 종목 보존.

        Returns: {"affected_picks": [3, 5], "auto_archived_picks": [5]}
        """
        if not self._db:
            raise RuntimeError("SectorStore not open")
        now_iso = to_db_iso(now_kst())

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._db.execute(
                "SELECT DISTINCT ss.pick_id FROM sector_stocks ss "
                "JOIN sector_picks sp ON sp.id = ss.pick_id "
                "WHERE sector_key(ss.sector_name) = ? AND sp.status = ? AND sp.expires_at > ?",
                (sector_key(sector_name), PickStatus.ACTIVE.value, now_iso),
            )
            affected_picks = [r[0] for r in await cur.fetchall()]

            auto_archived: list[int] = []
            for pick_id in affected_picks:
                await self._db.execute(
                    "DELETE FROM sector_stocks "
                    "WHERE pick_id = ? AND sector_key(sector_name) = ?",
                    (pick_id, sector_key(sector_name)),
                )
                cur2 = await self._db.execute(
                    "SELECT COUNT(*) FROM sector_stocks WHERE pick_id = ?",
                    (pick_id,),
                )
                if (await cur2.fetchone())[0] == 0:
                    await self._db.execute(
                        "UPDATE sector_picks SET status = ? WHERE id = ?",
                        (PickStatus.ARCHIVED.value, pick_id),
                    )
                    auto_archived.append(pick_id)

            await self._db.execute("COMMIT")
        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("archive_sector failed for sector=%s", sector_name)
            raise

        return {"affected_picks": affected_picks, "auto_archived_picks": auto_archived}

    async def remove_stock_from_sector(self, sector_name: str, stock_code: str) -> dict:
        """해당 섹터의 특정 종목 DELETE → 빈 Pick은 자동 archive.

        Returns: {"removed_from_picks": [3], "auto_archived_picks": []}
        """
        if not self._db:
            raise RuntimeError("SectorStore not open")
        now_iso = to_db_iso(now_kst())

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._db.execute(
                "SELECT DISTINCT ss.pick_id FROM sector_stocks ss "
                "JOIN sector_picks sp ON sp.id = ss.pick_id "
                "WHERE sector_key(ss.sector_name) = ? AND ss.stock_code = ? "
                "AND sp.status = ? AND sp.expires_at > ?",
                (sector_key(sector_name), stock_code, PickStatus.ACTIVE.value, now_iso),
            )
            affected_picks = [r[0] for r in await cur.fetchall()]

            auto_archived: list[int] = []
            for pick_id in affected_picks:
                await self._db.execute(
                    "DELETE FROM sector_stocks WHERE pick_id = ? "
                    "AND sector_key(sector_name) = ? AND stock_code = ?",
                    (pick_id, sector_key(sector_name), stock_code),
                )
                cur2 = await self._db.execute(
                    "SELECT COUNT(*) FROM sector_stocks WHERE pick_id = ?",
                    (pick_id,),
                )
                if (await cur2.fetchone())[0] == 0:
                    await self._db.execute(
                        "UPDATE sector_picks SET status = ? WHERE id = ?",
                        (PickStatus.ARCHIVED.value, pick_id),
                    )
                    auto_archived.append(pick_id)

            await self._db.execute("COMMIT")
        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("remove_stock_from_sector failed sector=%s code=%s", sector_name, stock_code)
            raise

        return {"removed_from_picks": affected_picks, "auto_archived_picks": auto_archived}

    async def find_duplicate_sectors(self) -> dict[str, dict]:
        """중복 sector_name 탐색 (읽기 전용, 실제 병합 없음).

        Returns: {
            sector_name: {"pick_ids": [3, 4, 5], "stock_counts": [3, 1, 1]}
        }
        pick_ids/stock_counts는 created_at ASC 순 (oldest first).
        pick이 1개뿐인 섹터는 제외.
        """
        if not self._db:
            return {}

        now_iso = to_db_iso(now_kst())
        cur = await self._db.execute(
            "SELECT ss.sector_name, sp.id, sp.created_at, COUNT(ss.id) "
            "FROM sector_stocks ss "
            "JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sp.status = ? AND sp.expires_at > ? "
            "GROUP BY ss.sector_name, sp.id "
            "ORDER BY ss.sector_name, sp.created_at ASC",
            (PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()

        sector_data: dict[str, dict] = {}
        for sector_name, pick_id, _, cnt in rows:
            entry = sector_data.setdefault(sector_name, {"pick_ids": [], "stock_counts": []})
            entry["pick_ids"].append(pick_id)
            entry["stock_counts"].append(cnt)

        return {k: v for k, v in sector_data.items() if len(v["pick_ids"]) >= 2}

    async def merge_duplicate_sectors(self) -> dict[str, dict]:
        """같은 sector_name을 가진 여러 active 픽을 가장 오래된 pick_id로 병합.

        Returns: {sector_name: {target_id, merged_ids, total_stocks}}
        병합된 픽은 archived 처리 (삭제 X).
        """
        if not self._db:
            raise RuntimeError("SectorStore not open")

        now_iso = to_db_iso(now_kst())

        cur = await self._db.execute(
            "SELECT ss.sector_name, sp.id as pick_id, sp.created_at "
            "FROM sector_stocks ss "
            "JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sp.status = ? AND sp.expires_at > ? "
            "GROUP BY ss.sector_name, sp.id "
            "ORDER BY ss.sector_name, sp.created_at ASC",
            (PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()

        sector_picks: dict[str, list[int]] = {}
        for sector_name, pick_id, _ in rows:
            sector_picks.setdefault(sector_name, []).append(pick_id)

        results: dict[str, dict] = {}
        for sector_name, pick_ids in sector_picks.items():
            if len(pick_ids) < 2:
                continue

            target_id = pick_ids[0]
            dup_ids = pick_ids[1:]

            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cur2 = await self._db.execute(
                    "SELECT stock_code FROM sector_stocks "
                    "WHERE pick_id = ? AND sector_name = ?",
                    (target_id, sector_name),
                )
                existing_codes = {r[0] for r in await cur2.fetchall()}

                # 섹터 스코프 max — 다른 섹터 번호와 점프 방지
                cur3 = await self._db.execute(
                    "SELECT COALESCE(MAX(added_order), 0) FROM sector_stocks "
                    "WHERE pick_id = ? AND sector_name = ?",
                    (target_id, sector_name),
                )
                next_order = (await cur3.fetchone())[0]

                for dup_id in dup_ids:
                    cur4 = await self._db.execute(
                        "SELECT stock_code, stock_name FROM sector_stocks "
                        "WHERE pick_id = ? AND sector_name = ?",
                        (dup_id, sector_name),
                    )
                    for code, name in await cur4.fetchall():
                        if code not in existing_codes:
                            next_order += 1
                            await self._db.execute(
                                "INSERT INTO sector_stocks "
                                "(pick_id, sector_name, stock_code, stock_name, added_order) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (target_id, sector_name, code, name, next_order),
                            )
                            existing_codes.add(code)

                await self._db.executemany(
                    "UPDATE sector_picks SET status = ? WHERE id = ?",
                    [(PickStatus.ARCHIVED.value, did) for did in dup_ids],
                )
                await self._db.execute("COMMIT")
            except Exception:
                await self._db.execute("ROLLBACK")
                logger.exception("merge_duplicate_sectors failed for sector=%s", sector_name)
                raise

            results[sector_name] = {
                "target_id": target_id,
                "merged_ids": dup_ids,
                "total_stocks": len(existing_codes),
            }

        return results

    # --- Phase 2: 알림 이력 ---
    async def insert_alert(
        self,
        sector_name: str,
        stage: int,
        triggered_at: datetime,
        passed_stocks: list[dict[str, Any]] | dict[str, Any],
        metrics: dict[str, Any],
        threshold_used: dict[str, Any],
    ) -> int:
        if not self._db:
            raise RuntimeError("SectorStore not open")
        cur = await self._db.execute(
            "INSERT INTO alert_history "
            "(sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used, delivery_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sector_name,
                stage,
                to_db_iso(triggered_at),
                json.dumps(passed_stocks, ensure_ascii=False),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(threshold_used, ensure_ascii=False),
                'sent',
            ),
        )
        alert_id = cur.lastrowid
        if alert_id is None:
            raise RuntimeError("lastrowid missing after alert_history insert")
        return alert_id

    async def try_insert_alert_with_cooldown(
        self,
        sector_name: str,
        stage: int,
        cooldown_min: int,
        triggered_at: datetime,
        passed_stocks: list[dict[str, Any]] | dict[str, Any],
        metrics: dict[str, Any],
        threshold_used: dict[str, Any],
        initial_status: str = 'pending',
    ) -> tuple[AlertResult, int | None]:
        """쿨다운 체크와 INSERT를 단일 SQL 문으로 원자 실행.

        - 쿨다운 기간 내 동일 (sector_name, stage) 기록이 있으면 COOLDOWN_ACTIVE 반환.
        - 없으면 delivery_status=initial_status 로 INSERT 후 (INSERTED, row_id) 반환.
        - sqlite3.OperationalError(locked/busy) 시 최대 3회 재시도 (100/300/1000ms).
        - 재시도 소진 시 INSERT_FAILED 반환 (notify 억제).
        """
        if not self._db:
            raise RuntimeError("SectorStore not open")
        threshold_iso = to_db_iso(triggered_at - timedelta(minutes=cooldown_min))
        now_iso = to_db_iso(triggered_at)
        sql = (
            "INSERT INTO alert_history "
            "(sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used, delivery_status) "
            "SELECT ?, ?, ?, ?, ?, ?, ? "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM alert_history "
            "  WHERE sector_name = ? AND stage = ? AND triggered_at > ?"
            ")"
        )
        params = (
            sector_name,
            stage,
            now_iso,
            json.dumps(passed_stocks, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            json.dumps(threshold_used, ensure_ascii=False),
            initial_status,
            sector_name,
            stage,
            threshold_iso,
        )
        _retry_delays_ms = [100, 300, 1000]
        for attempt, delay_ms in enumerate([0] + _retry_delays_ms):
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            try:
                cur = await self._db.execute(sql, params)
                if cur.rowcount > 0:
                    return AlertResult.INSERTED, cur.lastrowid
                return AlertResult.COOLDOWN_ACTIVE, None
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" in msg or "busy" in msg) and attempt < len(_retry_delays_ms):
                    logger.warning(
                        "[sector_store] DB locked/busy, alert insert retry %d: %s",
                        attempt + 1, exc,
                    )
                    continue
                logger.error(
                    "[sector_store] alert insert failed after %d attempts: %s",
                    attempt + 1, exc,
                )
                return AlertResult.INSERT_FAILED, None
        return AlertResult.INSERT_FAILED, None  # unreachable, satisfies type checker

    async def update_delivery_status(self, alert_id: int, status: str) -> None:
        """alert_history 행의 delivery_status를 갱신. UPDATE 실패 시 예외 전파."""
        if not self._db:
            raise RuntimeError("SectorStore not open")
        await self._db.execute(
            "UPDATE alert_history SET delivery_status = ? WHERE id = ?",
            (status, alert_id),
        )

    async def should_alert(
        self,
        sector_name: str,
        stage: int,
        cooldown_min: int,
    ) -> bool:
        """동일 (sector_name, stage) 최근 알림이 cooldown 내면 False.

        Stage별 독립 쿨다운: Stage 1 알림이 있어도 Stage 2/3은 별개 판정.
        봇 재시작 후에도 DB 이력 기준으로 일관되게 동작.
        """
        if not self._db:
            return True
        threshold_iso = to_db_iso(now_kst() - timedelta(minutes=cooldown_min))
        cur = await self._db.execute(
            "SELECT 1 FROM alert_history "
            "WHERE sector_name = ? AND stage = ? AND triggered_at > ? "
            "LIMIT 1",
            (sector_name, stage, threshold_iso),
        )
        row = await cur.fetchone()
        return row is None
