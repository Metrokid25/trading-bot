"""일봉 기반 추세 필터.

KIS `inquire-daily-itemchartprice` 로 최근 일봉 로드 → MA20 > MA60 정배열 체크.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from loguru import logger

from config.constants import DAILY_MA_LONG, DAILY_MA_SHORT
from core.kis_api import KISClient


async def get_daily_closes(kis: KISClient, code: str, n_days: int = 100) -> list[tuple[datetime, float]]:
    """최근 n_days 영업일 일봉 종가 (오래된 → 최신 순)."""
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=n_days * 2)  # 주말/휴일 여유
    rows = await kis.get_daily_candles(
        code, start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"), "D"
    )
    out: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            d = datetime.strptime(r.get("stck_bsop_date", ""), "%Y%m%d")
            c = float(r.get("stck_clpr") or 0)
            if c > 0:
                out.append((d, c))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    return out[-n_days:]


async def daily_ma_passed(
    kis: KISClient, code: str,
    short: int = DAILY_MA_SHORT, long: int = DAILY_MA_LONG,
) -> tuple[bool, float, float]:
    """일봉 MA20 > MA60 정배열 체크. 반환 (passed, ma_short, ma_long)."""
    candles = await get_daily_closes(kis, code, n_days=long + 10)
    if len(candles) < long:
        logger.warning(f"[DAILY] {code}: 일봉 부족 {len(candles)}/{long}")
        return False, 0.0, 0.0
    closes = np.array([c[1] for c in candles], dtype=float)
    ma_s = float(closes[-short:].mean())
    ma_l = float(closes[-long:].mean())
    passed = ma_s > ma_l
    logger.info(
        f"[DAILY] {code} MA{short}={ma_s:,.0f} MA{long}={ma_l:,.0f} → "
        f"{'PASS' if passed else 'FAIL'}"
    )
    return passed, ma_s, ma_l
