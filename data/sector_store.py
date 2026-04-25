"""섹터 픽(스승님 워치리스트) SQLite 영속화."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import aiosqlite
from loguru import logger

from config import settings
from core.time_utils import now_kst, to_db_iso
from data.sector_models import PickStatus, SectorPick, SectorStock, UpsertResult


class AlertResult(Enum):
    INSERTED = "inserted"
    COOLDOWN_ACTIVE = "cooldown_active"
    INSERT_FAILED = "insert_failed"


class SectorStore:
    """픽 이벤트 + 섹터-종목 매핑 저장소."""

    def __init__(self, db_path=None) -> None:
        self.db_path = str(db_path or settings.DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        # isolation_level=None: 자동 트랜잭션 비활성. BEGIN/COMMIT/ROLLBACK을 명시적으로 관리.
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None)
        await self.init_tables()
        await self._migrate_alert_history_v2()

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
    ) -> UpsertResult:
        """섹터 단위 UPSERT: 동일 sector_name의 활성 픽이 있으면 종목만 추가, 없으면 새 픽 생성."""
        if not self._db:
            raise RuntimeError("SectorStore not open")

        now_iso = to_db_iso(now_kst())
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._db.execute(
                "SELECT ss.pick_id FROM sector_stocks ss "
                "JOIN sector_picks sp ON sp.id = ss.pick_id "
                "WHERE ss.sector_name = ? AND sp.status = ? AND sp.expires_at > ? "
                "ORDER BY sp.created_at DESC LIMIT 1",
                (sector_name, PickStatus.ACTIVE.value, now_iso),
            )
            row = await cur.fetchone()

            if row:
                pick_id: int = row[0]
                is_new_pick = False

                cur2 = await self._db.execute(
                    "SELECT stock_code FROM sector_stocks WHERE pick_id = ? AND sector_name = ?",
                    (pick_id, sector_name),
                )
                existing_codes = {r[0] for r in await cur2.fetchall()}

                cur3 = await self._db.execute(
                    "SELECT COALESCE(MAX(added_order), 0) FROM sector_stocks WHERE pick_id = ?",
                    (pick_id,),
                )
                max_order = (await cur3.fetchone())[0]

                added: list[SectorStock] = []
                skipped: list[SectorStock] = []
                for s in stocks:
                    if s.stock_code in existing_codes:
                        skipped.append(s)
                    else:
                        max_order += 1
                        s.added_order = max_order
                        added.append(s)

                if added:
                    await self._db.executemany(
                        "INSERT INTO sector_stocks "
                        "(pick_id, sector_name, stock_code, stock_name, added_order) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(pick_id, s.sector_name, s.stock_code, s.stock_name, s.added_order)
                         for s in added],
                    )

                total = len(existing_codes) + len(added)

            else:
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

                for i, s in enumerate(stocks, start=1):
                    s.added_order = i

                if stocks:
                    await self._db.executemany(
                        "INSERT INTO sector_stocks "
                        "(pick_id, sector_name, stock_code, stock_name, added_order) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(pick_id, s.sector_name, s.stock_code, s.stock_name, s.added_order)
                         for s in stocks],
                    )

                added = list(stocks)
                total = len(stocks)

            await self._db.execute("COMMIT")

        except Exception:
            await self._db.execute("ROLLBACK")
            logger.exception("upsert_sector failed, rolled back (sector=%s)", sector_name)
            raise

        return UpsertResult(
            pick_id=pick_id,
            is_new_pick=is_new_pick,
            added_count=len(added),
            skipped_stocks=skipped,
            total_count=total,
        )

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
            "SELECT id, pick_id, sector_name, stock_code, stock_name, added_order "
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
            )
            for r in rows
        ]

    async def get_stocks_by_sector(
        self, pick_id: int, sector_name: str
    ) -> list[SectorStock]:
        if not self._db:
            return []
        cur = await self._db.execute(
            "SELECT id, pick_id, sector_name, stock_code, stock_name, added_order "
            "FROM sector_stocks WHERE pick_id = ? AND sector_name = ? "
            "ORDER BY added_order",
            (pick_id, sector_name),
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
            "WHERE ss.sector_name = ? AND sp.status = ? AND sp.expires_at > ? "
            "GROUP BY ss.pick_id "
            "ORDER BY sp.created_at ASC",
            (sector_name, PickStatus.ACTIVE.value, now_iso),
        )
        rows = await cur.fetchall()
        result = []
        for pick_id, sector_cnt in rows:
            cur2 = await self._db.execute(
                "SELECT COUNT(*) FROM sector_stocks WHERE pick_id = ? AND sector_name != ?",
                (pick_id, sector_name),
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
                "WHERE ss.sector_name = ? AND sp.status = ? AND sp.expires_at > ?",
                (sector_name, PickStatus.ACTIVE.value, now_iso),
            )
            affected_picks = [r[0] for r in await cur.fetchall()]

            auto_archived: list[int] = []
            for pick_id in affected_picks:
                await self._db.execute(
                    "DELETE FROM sector_stocks WHERE pick_id = ? AND sector_name = ?",
                    (pick_id, sector_name),
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
                "WHERE ss.sector_name = ? AND ss.stock_code = ? AND sp.status = ? AND sp.expires_at > ?",
                (sector_name, stock_code, PickStatus.ACTIVE.value, now_iso),
            )
            affected_picks = [r[0] for r in await cur.fetchall()]

            auto_archived: list[int] = []
            for pick_id in affected_picks:
                await self._db.execute(
                    "DELETE FROM sector_stocks WHERE pick_id = ? AND sector_name = ? AND stock_code = ?",
                    (pick_id, sector_name, stock_code),
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
