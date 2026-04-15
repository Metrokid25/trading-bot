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

    def opens(self) -> list[float]:
        return [c.open for c in self.closed] + ([self._cur.open] if self._cur else [])

    def volumes(self) -> list[int]:
        return [c.volume for c in self.closed] + ([self._cur.volume] if self._cur else [])

    def candles(self) -> list[Candle]:
        arr = list(self.closed)
        if self._cur:
            arr.append(self._cur)
        return arr

    def resample(self, factor: int) -> list[Candle]:
        """3분봉을 `factor` 개씩 묶어 상위 타임프레임 봉으로 리샘플."""
        src = self.candles()
        if factor <= 1 or not src:
            return src
        # factor 개씩 묶되, 마지막 미완 그룹은 부분봉으로 포함
        out: list[Candle] = []
        for i in range(0, len(src), factor):
            chunk = src[i : i + factor]
            if not chunk:
                continue
            out.append(
                Candle(
                    code=self.code,
                    ts=chunk[0].ts,
                    open=chunk[0].open,
                    high=max(c.high for c in chunk),
                    low=min(c.low for c in chunk),
                    close=chunk[-1].close,
                    volume=sum(c.volume for c in chunk),
                )
            )
        return out


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
