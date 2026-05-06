"""픽 종목 일봉 추적기.

픽 시점(pick_date)부터 D+N 일봉 데이터를 KIS에서 가져와 DailyOHLCV 리스트로 반환한다.
DB 적재(pick_daily_tracking INSERT)는 D3에서 구현한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from core.kis_api import KISClient


@dataclass(frozen=True, slots=True)
class DailyOHLCV:
    trade_date: str  # 'YYYY-MM-DD' (KIS 'YYYYMMDD' 변환)
    open: int
    high: int
    low: int
    close: int
    volume: int   # 누적 거래량 (KIS acml_vol)
    value: int    # 누적 거래대금, KRW (KIS acml_tr_pbmn)


async def fetch_daily_candles_for_pick(
    client: KISClient,
    ticker: str,
    pick_date: str,
    lookback_days: int = 20,
) -> list[DailyOHLCV]:
    """픽 시점(pick_date)부터 D+lookback_days까지의 일봉을 KIS에서 조회.

    영업일/비영업일 필터링은 하지 않음 — KIS가 거래일만 반환함.

    Returns:
        list[DailyOHLCV]: trade_date 오름차순. 빈 list 가능(휴장일 직후 등).

    Raises:
        ValueError: ticker 형식 불량, pick_date 형식 불량, lookback_days < 0
        httpx.HTTPError: KIS 4회 재시도 후에도 실패 (KIS 클라이언트 전파)

    Notes:
        - KIS 일봉 응답이 rt_cd != "0"이어도 현재 raise하지 않음
          (TODO: rt_cd 검증은 별도 위생 작업, 빈 list로 간주됨).
        - lookback_days=20이면 pick_date 포함 21일치 범위 요청
          (캘린더 일수 기준, KIS는 거래일만 반환하므로 실제 응답 수는 더 적음).
    """
    if not ticker or not ticker.isdigit() or len(ticker) != 6:
        raise ValueError(f"ticker must be 6-digit numeric string: {ticker!r}")

    if lookback_days < 0:
        raise ValueError(f"lookback_days must be >= 0, got {lookback_days}")

    try:
        start_dt = datetime.strptime(pick_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"pick_date must be 'YYYY-MM-DD', got {pick_date!r}")

    end_dt = start_dt + timedelta(days=lookback_days)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")

    rows = await client.get_daily_candles(ticker, start_str, end_str, period="D")

    result: list[DailyOHLCV] = []
    for row in rows:
        try:
            date_raw = row.get("stck_bsop_date", "")
            trade_date = datetime.strptime(date_raw, "%Y%m%d").strftime("%Y-%m-%d")
            candle = DailyOHLCV(
                trade_date=trade_date,
                open=int(row["stck_oprc"]),
                high=int(row["stck_hgpr"]),
                low=int(row["stck_lwpr"]),
                close=int(row["stck_clpr"]),
                volume=int(row["acml_vol"]),
                value=int(row["acml_tr_pbmn"]),
            )
            result.append(candle)
        except (KeyError, ValueError, TypeError):
            logger.warning(f"malformed daily candle row: ticker={ticker!r} row={row!r}")
            continue

    result.sort(key=lambda c: c.trade_date)
    return result
