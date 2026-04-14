"""틱 스트림을 3분봉으로 집계하는 메모리 버퍼 + SQLite 영속화."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Deque

import aiosqlite
from loguru import logger

from config import settings
from config.constants import CANDLE_INTERVAL_SEC
from data.models import Candle


class CandleBuffer:
    """종목 단위 3분봉 집계기."""

    def __init__(self, code: str, max_len: int = 300) -> None:
        self.code = code
        self.closed: Deque[Candle] = deque(maxlen=max_len)
        self._cur: Candle | None = None

    def _bucket_start(self, ts: datetime) -> datetime:
        sec = (ts.hour * 3600 + ts.minute * 60 + ts.second)
        bucket = (sec // CANDLE_INTERVAL_SEC) * CANDLE_INTERVAL_SEC
        return ts.replace(hour=bucket // 3600, minute=(bucket % 3600) // 60, second=0, microsecond=0)

    def on_tick(self, price: float, ts: datetime, volume: int = 0) -> Candle | None:
        """틱을 반영하고, 봉이 확정되면 그 봉을 반환."""
        bucket = self._bucket_start(ts)
        closed: Candle | None = None
        if self._cur is None:
            self._cur = Candle(self.code, bucket, price, price, price, price, volume)
        elif bucket > self._cur.ts:
            self.closed.append(self._cur)
            closed = self._cur
            self._cur = Candle(self.code, bucket, price, price, price, price, volume)
        else:
            c = self._cur
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume
        return closed

    def closes(self) -> list[float]:
        arr = [c.close for c in self.closed]
        if self._cur:
            arr.append(self._cur.close)
        return arr

    def highs(self) -> list[float]:
        return [c.high for c in self.closed] + ([self._cur.high] if self._cur else [])

    def lows(self) -> list[float]:
        return [c.low for c in self.closed] + ([self._cur.low] if self._cur else [])


class CandleStore:
    """SQLite 저장. 백테스트/분석용."""

    def __init__(self, db_path=None) -> None:
        self.db_path = str(db_path or settings.DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS candles (
                code TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                PRIMARY KEY (code, ts)
            )"""
        )
        await self._db.commit()

    async def save(self, c: Candle) -> None:
        if not self._db:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c.code, c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume),
        )
        await self._db.commit()

    async def load(self, code: str, start: datetime, end: datetime) -> list[Candle]:
        if not self._db:
            return []
        cur = await self._db.execute(
            "SELECT code, ts, open, high, low, close, volume FROM candles "
            "WHERE code=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (code, start.isoformat(), end.isoformat()),
        )
        rows = await cur.fetchall()
        return [Candle(r[0], datetime.fromisoformat(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows]

    async def close(self) -> None:
        if self._db:
            await self._db.close()
