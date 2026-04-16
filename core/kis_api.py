"""한국투자증권 KIS Developers REST API 래퍼.

인증 토큰 캐시, 현금 매수/매도, 현재가 조회, 3분봉 조회, 잔고 조회를 지원한다.
TR_ID는 실전/모의에 따라 다르므로 settings.KIS_ENV를 참조해 분기한다.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from config import settings

_TOKEN_CACHE_PATH = Path(settings.DB_PATH).parent / "kis_token.json"


@dataclass
class AccessToken:
    value: str
    expires_at: float  # epoch seconds

    def is_valid(self) -> bool:
        return self.value and time.time() < self.expires_at - 60


class KISClient:
    def __init__(self) -> None:
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(base_url=settings.base_url, timeout=10.0)
        self._is_paper = settings.KIS_ENV == "PAPER"

    async def close(self) -> None:
        await self._client.aclose()

    # ----- 인증 -----
    def _load_cached_token(self) -> AccessToken | None:
        try:
            if not _TOKEN_CACHE_PATH.exists():
                return None
            data = json.loads(_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            if data.get("env") != settings.KIS_ENV:
                return None
            tok = AccessToken(value=data["value"], expires_at=float(data["expires_at"]))
            return tok if tok.is_valid() else None
        except Exception:
            return None

    def _save_cached_token(self, tok: AccessToken) -> None:
        try:
            _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_CACHE_PATH.write_text(
                json.dumps({"env": settings.KIS_ENV, "value": tok.value,
                            "expires_at": tok.expires_at}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"토큰 캐시 저장 실패: {e}")

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._token and self._token.is_valid():
                return self._token.value
            cached = self._load_cached_token()
            if cached:
                self._token = cached
                return cached.value
            r = await self._client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": settings.app_key,
                    "appsecret": settings.app_secret,
                },
            )
            r.raise_for_status()
            data = r.json()
            self._token = AccessToken(
                value=data["access_token"],
                expires_at=time.time() + int(data.get("expires_in", 86400)),
            )
            self._save_cached_token(self._token)
            logger.info("KIS 토큰 발급 완료")
            return self._token.value

    async def _headers(self, tr_id: str) -> dict[str, str]:
        token = await self._ensure_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": settings.app_key,
            "appsecret": settings.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # ----- 시세 -----
    async def get_current_price(self, code: str) -> int:
        """현재가 조회 (국내주식 현재가 시세)."""
        headers = await self._headers("FHKST01010100")
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        r = await self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return int(r.json()["output"]["stck_prpr"])

    async def get_minute_candles(self, code: str, interval: str = "3") -> list[dict[str, Any]]:
        """분봉 조회. KIS는 1/3/5/10/15/30/60분 지원."""
        headers = await self._headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": interval,
            "FID_PW_DATA_INCU_YN": "N",
        }
        r = await self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return r.json().get("output2", [])

    async def get_minute_candles_at(
        self, code: str, hhmmss: str, past_data: bool = True
    ) -> list[dict[str, Any]]:
        """특정 시각 기준 과거 방향으로 분봉 30개 반환 (당일 내).

        KIS 주식당일분봉조회는 `FID_INPUT_HOUR_1` 에 HHMMSS(6자리)를 주면
        그 시각 기준 이전 30개 분봉을 돌려준다. 페이지네이션에 사용.
        """
        headers = await self._headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": hhmmss,
            "FID_PW_DATA_INCU_YN": "Y" if past_data else "N",
        }
        r = await self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return r.json().get("output2", [])

    async def get_investor_trend(self, code: str) -> list[dict[str, Any]]:
        """종목별 투자자 매매동향 (최근 약 30영업일, 일자별).

        TR FHKST01010900 / inquire-investor. output 배열 각 행이 하루치.
        거래대금 필드(*_tr_pbmn)의 단위는 '백만원'.
        PAPER 환경에서 다른 TR 직후 호출 시 500이 나는 경우가 있어 간단 백오프.
        """
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        for attempt in range(4):
            headers = await self._headers("FHKST01010900")
            r = await self._client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                headers=headers,
                params=params,
            )
            if r.status_code == 200:
                return r.json().get("output", [])
            if r.status_code >= 500 and attempt < 3:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            r.raise_for_status()
        return []

    async def get_daily_candles(
        self, code: str, start: str, end: str, period: str = "D"
    ) -> list[dict[str, Any]]:
        """일봉(또는 주/월) 조회. PAPER 환경 간헐 500 에 대한 백오프 포함."""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        }
        for attempt in range(4):
            headers = await self._headers("FHKST03010100")
            r = await self._client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=headers,
                params=params,
            )
            if r.status_code == 200:
                return r.json().get("output2", [])
            if r.status_code >= 500 and attempt < 3:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            r.raise_for_status()
        return []

    # ----- 주문 -----
    async def order_cash(self, code: str, qty: int, price: int = 0, side: str = "BUY") -> dict:
        """현금 매수/매도. price=0 이면 시장가."""
        if side == "BUY":
            tr_id = "VTTC0802U" if self._is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self._is_paper else "TTTC0801U"

        acc = settings.account_no.split("-")
        cano, prdt = acc[0], acc[1] if len(acc) > 1 else "01"

        headers = await self._headers(tr_id)
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "PDNO": code,
            "ORD_DVSN": "01" if price == 0 else "00",  # 01=시장가 00=지정가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        r = await self._client.post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        logger.info(f"[ORDER/{side}] {code} qty={qty} price={price} → {data.get('msg1')}")
        return data

    async def buy_market(self, code: str, qty: int) -> dict:
        return await self.order_cash(code, qty, 0, "BUY")

    async def sell_market(self, code: str, qty: int) -> dict:
        return await self.order_cash(code, qty, 0, "SELL")

    # ----- 잔고 -----
    async def get_balance(self) -> dict:
        tr_id = "VTTC8434R" if self._is_paper else "TTTC8434R"
        acc = settings.account_no.split("-")
        cano, prdt = acc[0], acc[1] if len(acc) > 1 else "01"
        headers = await self._headers(tr_id)
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        r = await self._client.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return r.json()
