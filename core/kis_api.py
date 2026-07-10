"""한국투자증권 KIS Developers REST API 래퍼.

인증 토큰 캐시, 현금 매수/매도, 현재가 조회, 3분봉 조회, 잔고 조회를 지원한다.
시세 API는 VTS 미지원으로 항상 실전(REAL) 서버를 사용한다.
매매 API는 KIS_ENV 에 따라 실전/모의 서버를 분기한다.
"""
from __future__ import annotations

import asyncio
import collections
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


def _safe_float(value: Any) -> float:
    """KIS 숫자 문자열 → float. 결측/형식 오류는 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    """KIS 숫자 문자열 → int (소수점 문자열 허용). 결측/형식 오류는 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class _RateLimiter:
    """초당 max_per_sec 회 제한 토큰 버킷 (sliding window)."""

    def __init__(self, max_per_sec: int) -> None:
        self.max = max_per_sec
        self.window = 1.0
        self.timestamps: collections.deque[float] = collections.deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            while self.timestamps and self.timestamps[0] < now - self.window:
                self.timestamps.popleft()
            if len(self.timestamps) >= self.max:
                wait = self.window - (now - self.timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    while self.timestamps and self.timestamps[0] < now - self.window:
                        self.timestamps.popleft()
            self.timestamps.append(now)


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
        self._market_limiter = _RateLimiter(max_per_sec=15)

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
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHKST01010100")
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        return int(r.json()["output"]["stck_prpr"])

    async def get_quote(self, code: str, market_code: str = "J") -> dict[str, Any]:
        """현재가 + 전일대비 등락률(%) 조회. inquire-price output 파싱.

        market_code: FID_COND_MRKT_DIV_CODE. "J"(KRX 정규장) / "NX"(NXT) /
        "UN"(KRX+NXT 통합). NXT 프리장·애프터장 시세는 "UN"으로 받는다
        (2026-07-10 프로브 확인).
        """
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHKST01010100")
        params = {"FID_COND_MRKT_DIV_CODE": market_code, "FID_INPUT_ISCD": code}
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        out = r.json().get("output", {}) or {}
        return {
            "code": code,
            "price": _safe_int(out.get("stck_prpr")),
            "change_rate": _safe_float(out.get("prdy_ctrt")),  # 부호 포함 등락률 %
            "volume": _safe_int(out.get("acml_vol")),  # 누적 거래량(주)
            "value": _safe_int(out.get("acml_tr_pbmn")),  # 누적 거래대금(원)
        }

    async def get_index(self, code: str) -> dict[str, Any]:
        """국내 업종지수 현재가. code: '0001'(코스피) / '1001'(코스닥).

        inquire-index-price (TR FHPUP02100000), 시장구분 'U'(업종).
        """
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHPUP02100000")
        params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code}
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        out = r.json().get("output", {}) or {}
        return {
            "code": code,
            "value": _safe_float(out.get("bstp_nmix_prpr")),  # 업종지수 현재가
            "change": _safe_float(out.get("bstp_nmix_prdy_vrss")),  # 전일대비
            "change_rate": _safe_float(out.get("bstp_nmix_prdy_ctrt")),  # 전일대비율 %
            # 시장 폭 (상승/상한/보합/하락/하한 종목수) — 하한은 lslm_issu_cnt
            "up_count": _safe_int(out.get("ascn_issu_cnt")),
            "upper_count": _safe_int(out.get("uplm_issu_cnt")),
            "flat_count": _safe_int(out.get("stnr_issu_cnt")),
            "down_count": _safe_int(out.get("down_issu_cnt")),
            "lower_count": _safe_int(out.get("lslm_issu_cnt")),
        }

    async def get_index_minute_chart(
        self, code: str, interval_sec: int = 300
    ) -> dict[str, Any]:
        """업종지수 분봉차트 (TR FHKUP03500200, inquire-time-indexchartprice).

        FID_INPUT_HOUR_1은 기준 시각이 아니라 '봉 간격(초)'이다. 최근 102봉을
        최신→과거 순으로 돌려주므로, 당일 전체를 한 번에 받으려면 300초(5분봉)
        이상을 쓴다 (102봉 × 5분 = 510분 > 정규장 390분).

        반환: {"summary": output1(지수 현재가·전일대비 등), "bars": output2(봉 목록)}
        """
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHKUP03500200")
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": str(interval_sec),
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "0",
        }
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        body = r.json()
        return {
            "summary": body.get("output1", {}) or {},
            "bars": body.get("output2", []) or [],
        }

    # 시장별 투자자매매동향 파라미터 — (FID_INPUT_ISCD, FID_INPUT_ISCD_2).
    # 다른 조합은 rt_cd=0이지만 전부 0을 돌려준다 (2026-07-10 프로브로 확정).
    _MARKET_FLOW_PARAMS = {"KOSPI": ("KSP", "0001"), "KOSDAQ": ("KSQ", "1001")}

    async def get_market_investor_flow(self, market: str) -> dict[str, int]:
        """시장 전체 투자자별 순매수 대금 (TR FHPTJ04030000). 단위: 백만원.

        market: "KOSPI" | "KOSDAQ". 반환: 개인/외국인/기관 순매수 대금.
        """
        iscd, iscd2 = self._MARKET_FLOW_PARAMS[market]
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHPTJ04030000")
        params = {"FID_INPUT_ISCD": iscd, "FID_INPUT_ISCD_2": iscd2}
        r = await self._real_client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        # 응답은 현재 스냅샷 단일 행 (2026-07-10 프로브: output list len=1)
        rows = r.json().get("output", []) or []
        row = rows[0] if rows else {}
        return {
            "individual": _safe_int(row.get("prsn_ntby_tr_pbmn")),
            "foreign": _safe_int(row.get("frgn_ntby_tr_pbmn")),
            "institution": _safe_int(row.get("orgn_ntby_tr_pbmn")),
        }

    async def get_minute_candles(
        self, code: str, market_code: str = "J"
    ) -> list[dict[str, Any]]:
        """당일 1분봉 30개 조회 (현재 시각 기준 과거 방향).

        KIS inquire-time-itemchartprice는 1분봉만 지원하며,
        FID_INPUT_HOUR_1에는 조회 기준 시각 HHMMSS 6자리를 넘겨야 한다.
        이 엔드포인트는 분봉 간격(interval) 선택을 지원하지 않는다.

        market_code: "J"(KRX) / "NX"(NXT) / "UN"(통합). NXT 프리장·애프터장
        분봉은 "UN"으로 받는다 (get_minute_candles_at 프로브와 동일).
        """
        await self._market_limiter.acquire()
        from core.time_utils import now_kst
        hhmmss = now_kst().strftime("%H%M%S")
        headers = await self._real_headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": market_code,
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": hhmmss,
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
        self, code: str, hhmmss: str, past_data: bool = True, market_code: str = "J"
    ) -> list[dict[str, Any]]:
        """특정 시각 기준 과거 방향으로 분봉 30개 반환 (당일 내).

        KIS 주식당일분봉조회는 `FID_INPUT_HOUR_1` 에 HHMMSS(6자리)를 주면
        그 시각 기준 이전 30개 분봉을 돌려준다. 페이지네이션에 사용.

        market_code: FID_COND_MRKT_DIV_CODE. 기본 "J"(KRX 정규장). NXT 장전
        (08:00~09:00)을 받으려면 "NX"(NXT) 또는 "UN"(통합)을 넘긴다. "J"는
        장전 시각을 무시하고 정규장 데이터만 돌려준다(프로브로 확인).
        """
        await self._market_limiter.acquire()
        headers = await self._real_headers("FHKST03010200")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": market_code,
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
            await self._market_limiter.acquire()
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
            await self._market_limiter.acquire()
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
