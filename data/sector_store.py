"""섹터 픽(스승님 워치리스트) SQLite 영속화."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import aiosqlite
from loguru import logger

from config import settings
from data.sector_models import PickStatus, SectorPick, SectorStock


class SectorStore:
    """픽 이벤트 + 섹터-종목 매핑 저장소."""

    def __init__(self, db_path=None) -> None:
        self.db_path = str(db_path or settings.DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        # isolation_level=None: 자동 트랜잭션 비활성. BEGIN/COMMIT/ROLLBACK을 명시적으로 관리.
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None)
        await self.init_tables()

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
            """CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_name TEXT NOT NULL,
                stage INTEGER NOT NULL,
                triggered_at TEXT NOT NULL,
                passed_stocks TEXT NOT NULL,
                metrics TEXT NOT NULL,
                threshold_used TEXT NOT NULL
            )"""
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_sector_time "
            "ON alert_history (sector_name, triggered_at)"
        )

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
                    pick.created_at.isoformat(),
                    pick.expires_at.isoformat(),
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

    async def get_active_picks(self) -> list[SectorPick]:
        if not self._db:
            return []
        # 조회 전 자동 만료 처리
        await self.expire_old_picks()
        now_iso = datetime.now().isoformat()
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
        now_iso = datetime.now().isoformat()
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
            "(sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                sector_name,
                stage,
                triggered_at.isoformat(),
                json.dumps(passed_stocks, ensure_ascii=False),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(threshold_used, ensure_ascii=False),
            ),
        )
        alert_id = cur.lastrowid
        if alert_id is None:
            raise RuntimeError("lastrowid missing after alert_history insert")
        return alert_id

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
        threshold_iso = (datetime.now() - timedelta(minutes=cooldown_min)).isoformat()
        cur = await self._db.execute(
            "SELECT 1 FROM alert_history "
            "WHERE sector_name = ? AND stage = ? AND triggered_at > ? "
            "LIMIT 1",
            (sector_name, stage, threshold_iso),
        )
        row = await cur.fetchone()
        return row is None
