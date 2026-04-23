"""AnalysisAgent — 3분봉 집계 / 시그널 생성 / SQLite 누적 저장 / 장후 보완수집."""
from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from agents.base_agent import BaseAgent, E, Event, EventBus
from config.constants import MARKET_CLOSE
from core.kis_api import KISClient
from core.time_utils import now_kst
from core.websocket_client import KISWebSocket
from data.candle_store import CandleBuffer, CandleStore
from data.models import Candle, WatchItem
from strategy.signal import evaluate_buy


class AnalysisAgent(BaseAgent):
    def __init__(
        self,
        bus: EventBus,
        kis: KISClient,
        store: CandleStore | None = None,
    ) -> None:
        super().__init__("AnalysisAgent", bus)
        self.kis = kis
        self.store = store
        self.buffers: dict[str, CandleBuffer] = {}
        self.watchlist: dict[str, WatchItem] = {}
        self._ws = KISWebSocket(on_tick=self._on_tick)
        self._ws_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._backfilled_dates: dict[str, str] = {}  # {code: 'YYYYMMDD'}

        bus.subscribe(E.WATCHLIST_UPDATED, self._on_watchlist)

    async def _on_watchlist(self, evt: Event) -> None:
        new: dict[str, WatchItem] = evt.payload
        added = set(new) - set(self.watchlist)
        removed = set(self.watchlist) - set(new)
        self.watchlist = dict(new)
        for code in added:
            self.buffers.setdefault(code, CandleBuffer(code))
            await self._ws.subscribe(code)
            await self._prime_from_store(code)
        for code in removed:
            self.buffers.pop(code, None)
            await self._ws.unsubscribe(code)

    async def _prime_from_store(self, code: str) -> None:
        """SQLite에 있는 최근 데이터를 버퍼에 프리로드 → 지표 워밍업 즉시 확보."""
        if not self.store:
            return
        from datetime import timedelta
        end = now_kst()
        start = end - timedelta(days=10)
        try:
            rows = await self.store.load(code, start, end)
        except Exception as e:
            logger.warning(f"[PRIME] {code}: {e}")
            return
        buf = self.buffers.get(code)
        if not buf or not rows:
            return
        for c in rows:
            buf.closed.append(c)
        logger.info(f"[PRIME] {code}: {len(rows)}봉 프리로드 ({rows[0].ts} ~ {rows[-1].ts})")

    async def _on_tick(self, code: str, price: int, ts_str: str) -> None:
        buf = self.buffers.get(code)
        if not buf:
            return
        try:
            now = now_kst().replace(
                hour=int(ts_str[:2]), minute=int(ts_str[2:4]), second=int(ts_str[4:6]), microsecond=0
            )
        except Exception:
            now = now_kst()
        await self.bus.publish(Event(E.TICK, {"code": code, "price": price, "ts": now}))

        closed = buf.on_tick(float(price), now)
        if closed:
            # SQLite 저장 (비동기 fire-and-forget)
            if self.store:
                asyncio.create_task(self._save_candle(closed))
            await self.bus.publish(Event(E.CANDLE_CLOSED, {"code": code, "candle": closed, "buf": buf}))
            sig = evaluate_buy(code, buf, now)
            if sig:
                logger.info(f"[BUY SIGNAL] {code} @ {sig.price} — {sig.reason}")
                await self.bus.publish(Event(E.BUY_SIGNAL, sig))

    async def _save_candle(self, c: Candle) -> None:
        try:
            await self.store.save(c)  # type: ignore[union-attr]
        except Exception as e:
            logger.debug(f"candle save fail {c.code} {c.ts}: {e}")

    # ----- 장 마감 후 보완 수집 -----
    async def _backfill_loop(self) -> None:
        """5분 간격 체크. 장 종료 후 하루 1회 당일 분봉을 조회해 누락분 UPSERT."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=300)
                return
            except asyncio.TimeoutError:
                pass

            now = now_kst()
            if now.time() < MARKET_CLOSE:
                continue
            today = now.strftime("%Y%m%d")
            for code in list(self.watchlist.keys()):
                if self._backfilled_dates.get(code) == today:
                    continue
                saved = await self._backfill_code(code)
                if saved > 0:
                    self._backfilled_dates[code] = today
                    logger.info(f"[BACKFILL] {code}: {saved}봉 보완 저장")

    async def _backfill_code(self, code: str) -> int:
        if not self.store:
            return 0
        try:
            rows = await self.kis.get_minute_candles(code, "3")
        except Exception as e:
            logger.warning(f"[BACKFILL] {code} fetch 실패: {e}")
            return 0
        saved = 0
        buf = self.buffers.get(code)
        for r in rows:
            c = self._parse_kis_row(code, r)
            if not c or c.ts.minute % 3 != 0:
                continue
            try:
                await self.store.save(c)
                saved += 1
                if buf is not None and (not buf.closed or buf.closed[-1].ts < c.ts):
                    buf.closed.append(c)
            except Exception:
                pass
        return saved

    @staticmethod
    def _parse_kis_row(code: str, row: dict) -> Candle | None:
        try:
            date_s = row.get("stck_bsop_date") or ""
            hour_s = (row.get("stck_cntg_hour") or "").zfill(6)
            if not date_s or not hour_s:
                return None
            ts = datetime.strptime(date_s + hour_s, "%Y%m%d%H%M%S")
            return Candle(
                code=code,
                ts=ts,
                open=float(row.get("stck_oprc") or 0),
                high=float(row.get("stck_hgpr") or 0),
                low=float(row.get("stck_lwpr") or 0),
                close=float(row.get("stck_prpr") or 0),
                volume=int(row.get("cntg_vol") or 0),
            )
        except Exception:
            return None

    async def run(self) -> None:
        codes = list(self.watchlist.keys())
        self._ws_task = asyncio.create_task(self._ws.run(codes))
        self._backfill_task = asyncio.create_task(self._backfill_loop())
        await self._stop.wait()
        self._ws.stop()
        for t in (self._ws_task, self._backfill_task):
            if t:
                t.cancel()
        logger.info("[AnalysisAgent] stopped")
