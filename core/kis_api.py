"""한국투자증권 KIS Developers REST API 래퍼.

인증 토큰 캐시, 현금 매수/매도, 현재가 조회, 3분봉 조회, 잔고 조회를 지원한다.
시세 API는 VTS 미지원으로 항상 실전(REAL) 서버를 사용한다.
매매 API는 KIS_ENV 에 따라 실전/모의 서버를 분기한다.
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

_TOKEN_CACHE_REAL = Path(settings.DB_PATH).parent / "kis_token_real.json"
_TOKEN_CACHE_PAPER = Path(settings.DB_PATH).parent / "kis_token_paper.json"


@dataclass
class AccessToken:
    value: str
    expires_at: float  # epoch seconds

    def is_valid(self) -> bool:
        return self.value and time.time() < self.expires_at - 60


class KISClient:
    def __init__(self) -> None:
        self._is_paper = settings.KIS_ENV == "PAPER"

        # 시세: 항상 실전 서버
        self._real_client = httpx.AsyncClient(base_url=settings.KIS_REAL_BASE_URL, timeout=10.0)
        self._real_token: AccessToken | None = None
        self._real_lock = asyncio.Lock()

        # 매매: PAPER 모드 시 모의 서버, REAL 모드 시 실전 서버(동일 클라이언트 재사용)
        if self._is_paper:
            self._trade_client = httpx.AsyncClient(
                base_url=settings.KIS_PAPER_BASE_URL, timeout=10.0
            )
            self._trade_token: AccessToken | None = None
            self._trade_lock = asyncio.Lock()
        else:
            self._trade_client = self._real_client

    async def close(self) -> None:
        await self._real_client.aclose()
        if self._is_paper:
            await self._trade_client.aclose()

    # ----- 토큰 캐시 헬퍼 -----
    def _load_token_cache(self, path: Path, env_label: str) -> AccessToken | None:
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("env") != env_label:
                return None
            tok = AccessToken(value=data["value"], expires_at=float(data["expires_at"]))
            return tok if tok.is_valid() else None
        except Exception:
            return None

    def _save_token_cache(self, tok: AccessToken, path: Path, env_label: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"env": env_label, "value": tok.value, "expires_at": tok.expires_at}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"토큰 캐시 저장 실패: {e}")

    # ----- 토큰 발급 -----
    async def _ensure_real_token(self) -> str:
        async with self._real_lock:
            if self._real_token and self._real_token.is_valid():
                return self._real_token.value
            cached = self._load_token_cache(_TOKEN_CACHE_REAL, "REAL")
            if cached:
                self._real_token = cached
                return cached.value
            r = await self._real_client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": settings.KIS_REAL_APP_KEY,
                    "appsecret": settings.KIS_REAL_APP_SECRET,
                },
            )
            r.raise_for_status()
            data = r.json()
            self._real_token = AccessToken(
                value=data["access_token"],
                expires_at=time.time() + int(data.get("expires_in", 86400)),
            )
            self._save_token_cache(self._real_token, _TOKEN_CACHE_REAL, "REAL")
            logger.info("KIS REAL 토큰 발급 완료")
            return self._real_token.value

    async def _ensure_trade_token(self) -> str:
        if not self._is_paper:
            return await self._ensure_real_token()
        async with self._trade_lock:
            if self._trade_token and self._trade_token.is_valid():
                return self._trade_token.value
            cached = self._load_token_cache(_TOKEN_CACHE_PAPER, "PAPER")
            if cached:
                self._trade_token = cached
                return cached.value
            r = await self._trade_client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": settings.KIS_PAPER_APP_KEY,
                    "appsecret": settings.KIS_PAPER_APP_SECRET,
                },
            )
            r.raise_for_status()
            data = r.json()
            self._trade_token = AccessToken(
                value=data["access_token"],
                expires_at=time.time() + int(data.get("expires_in", 86400)),
            )
            self._save_token_cache(self._trade_token, _TOKEN_CACHE_PAPER, "PAPER")
            logger.info("KIS PAPER 토큰 발급 완료")
            return self._trade_token.value

    # ----- 헤더 -----
    async def _real_headers(self, tr_id: str) -> dict[str, str]:
        token = await self._ensure_real_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": settings.KIS_REAL_APP_KEY,
            "appsecret": settings.KIS_REAL_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def _trade_headers(self, tr_id: str) -> dict[str, str]:
        token = await self._ensure_trade_token()
        key = settings.KIS_PAPER_APP_KEY if self._is_paper else settings.KIS_REAL_APP_KEY
        secret = settings.KIS_PAPER_APP_SECRET if self._is_paper else settings.KIS_REAL_APP_SECRET
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": key,
            "appsecret": secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # ----- 시세 (항상 실전 서버) -----
    async def get_current_price(self, code: str) -> int:
        """현재가 조회 (국내주식 현재가 시세)."""
        headers = await self._real_headers("FHKST01010100")
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return int(r.json()["output"]["stck_prpr"])

    async def get_minute_candles(self, code: str, interval: str = "3") -> list[dict[str, Any]]:
        """분봉 조회. KIS는 1/3/5/10/15/30/60분 지원."""
        headers = await self._real_headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": interval,
            "FID_PW_DATA_INCU_YN": "N",
        }
        r = await self._real_client.get(
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
        headers = await self._real_headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": hhmmss,
            "FID_PW_DATA_INCU_YN": "Y" if past_data else "N",
        }
        r = await self._real_client.get(
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
        """
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        for attempt in range(4):
            headers = await self._real_headers("FHKST01010900")
            r = await self._real_client.get(
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
        """일봉(또는 주/월) 조회."""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        }
        for attempt in range(4):
            headers = await self._real_headers("FHKST03010100")
            r = await self._real_client.get(
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

    # ----- 주문 (ENV에 따라 실전/모의 분기) -----
    async def order_cash(self, code: str, qty: int, price: int = 0, side: str = "BUY") -> dict:
        """현금 매수/매도. price=0 이면 시장가."""
        if side == "BUY":
            tr_id = "VTTC0802U" if self._is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self._is_paper else "TTTC0801U"

        acc = settings.account_no.split("-")
        cano, prdt = acc[0], acc[1] if len(acc) > 1 else "01"

        headers = await self._trade_headers(tr_id)
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "PDNO": code,
            "ORD_DVSN": "01" if price == 0 else "00",  # 01=시장가 00=지정가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        r = await self._trade_client.post(
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
        headers = await self._trade_headers(tr_id)
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
        r = await self._trade_client.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return r.json()
