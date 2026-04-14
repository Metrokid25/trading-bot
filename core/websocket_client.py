"""KIS 실시간 체결가 WebSocket 클라이언트.

간단히 체결가(H0STCNT0) 스트림만 수신하고 콜백으로 넘긴다.
승인키는 /oauth2/Approval 로 별도 발급 필요.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx
import websockets
from loguru import logger

from config import settings

TickCallback = Callable[[str, int, str], Awaitable[None]]  # (code, price, timestamp)


class KISWebSocket:
    def __init__(self, on_tick: TickCallback) -> None:
        self._on_tick = on_tick
        self._approval_key: str | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subscribed: set[str] = set()
        self._stop = asyncio.Event()

    async def _get_approval_key(self) -> str:
        if self._approval_key:
            return self._approval_key
        async with httpx.AsyncClient(base_url=settings.base_url, timeout=10.0) as c:
            r = await c.post(
                "/oauth2/Approval",
                json={
                    "grant_type": "client_credentials",
                    "appkey": settings.app_key,
                    "secretkey": settings.app_secret,
                },
            )
            r.raise_for_status()
            self._approval_key = r.json()["approval_key"]
            return self._approval_key

    async def subscribe(self, code: str) -> None:
        if code in self._subscribed or not self._ws:
            return
        key = await self._get_approval_key()
        msg = {
            "header": {"approval_key": key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        }
        await self._ws.send(json.dumps(msg))
        self._subscribed.add(code)
        logger.info(f"[WS] subscribed {code}")

    async def unsubscribe(self, code: str) -> None:
        if code not in self._subscribed or not self._ws:
            return
        key = await self._get_approval_key()
        msg = {
            "header": {"approval_key": key, "custtype": "P", "tr_type": "2", "content-type": "utf-8"},
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        }
        await self._ws.send(json.dumps(msg))
        self._subscribed.discard(code)

    async def run(self, codes: list[str]) -> None:
        await self._get_approval_key()
        while not self._stop.is_set():
            try:
                async with websockets.connect(settings.ws_url, ping_interval=30) as ws:
                    self._ws = ws
                    self._subscribed.clear()
                    for c in codes:
                        await self.subscribe(c)
                    async for raw in ws:
                        await self._dispatch(raw)
            except Exception as e:
                logger.warning(f"[WS] reconnect due to: {e}")
                await asyncio.sleep(3)

    async def _dispatch(self, raw: str) -> None:
        # 실시간 체결 데이터는 '0|H0STCNT0|001|...' 파이프 구분 포맷
        if not raw or raw[0] not in "01":
            return
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != "H0STCNT0":
            return
        body = parts[3]
        fields = body.split("^")
        # fields[0]=code, fields[1]=체결시간, fields[2]=현재가
        try:
            code = fields[0]
            ts = fields[1]
            price = int(fields[2])
            await self._on_tick(code, price, ts)
        except (IndexError, ValueError):
            pass

    def stop(self) -> None:
        self._stop.set()
