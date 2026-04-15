"""KIS API로 3분봉 히스토리컬 데이터 수집 → SQLite(CandleStore) 저장.

KIS 주식당일분봉조회(FHKST03010200)는 공식적으로는 "당일" 분봉만 반환하지만,
`FID_INPUT_HOUR_1=HHMMSS` 로 시각을 지정하면 그 시점 이전 30개 분봉을 반환한다.
이를 HHMMSS 를 역방향으로 옮겨가며 반복 호출해 하루치(3분봉 ~130개)를 채운다.

과거 일자 분봉 조회는 KIS 공식 미지원 → 스크립트 실행 시점의 '오늘' 포함
최근 영업일들에 대해 시도하되, 응답이 비거나 오늘 외 데이터가 안 오면 거기까지만 수집.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from core.kis_api import KISClient
from data.candle_store import CandleStore
from data.models import Candle


def _parse_candle(code: str, row: dict[str, Any]) -> Candle | None:
    """output2 row 를 Candle 로 변환. 필드명: stck_bsop_date(YYYYMMDD),
    stck_cntg_hour(HHMMSS), stck_oprc/hgpr/lwpr/prpr, cntg_vol."""
    try:
        date_s = row.get("stck_bsop_date") or ""
        hour_s = row.get("stck_cntg_hour") or ""
        if not date_s or not hour_s:
            return None
        ts = datetime.strptime(date_s + hour_s.zfill(6), "%Y%m%d%H%M%S")
        return Candle(
            code=code,
            ts=ts,
            open=float(row.get("stck_oprc") or 0),
            high=float(row.get("stck_hgpr") or 0),
            low=float(row.get("stck_lwpr") or 0),
            close=float(row.get("stck_prpr") or 0),
            volume=int(row.get("cntg_vol") or 0),
        )
    except Exception as e:
        logger.debug(f"parse fail: {e} {row}")
        return None


async def collect_intraday(kis: KISClient, code: str) -> list[Candle]:
    """오늘 날짜 3분봉을 장마감 시각(153000)부터 역방향으로 페이지네이션 수집."""
    collected: dict[datetime, Candle] = {}
    hhmmss = "153000"
    for _ in range(20):  # 최대 20회 (30*20=600 캔들, 실제 하루치 ~130)
        try:
            rows = await kis.get_minute_candles_at(code, hhmmss, past_data=False)
        except Exception as e:
            logger.warning(f"{code} {hhmmss}: {e}")
            break
        if not rows:
            break
        oldest_ts: datetime | None = None
        new_count = 0
        for r in rows:
            c = _parse_candle(code, r)
            if not c:
                continue
            # 3분봉만 유지 (분봉 주기에 맞춰 필터)
            if c.ts.minute % 3 != 0:
                continue
            if c.ts in collected:
                continue
            collected[c.ts] = c
            new_count += 1
            if oldest_ts is None or c.ts < oldest_ts:
                oldest_ts = c.ts
        if new_count == 0 or oldest_ts is None:
            break
        # 다음 조회 시각 = oldest 직전 시각
        next_dt = oldest_ts - timedelta(seconds=1)
        hhmmss = next_dt.strftime("%H%M%S")
        if next_dt.hour < 9:
            break
        await asyncio.sleep(0.1)  # rate limit
    return sorted(collected.values(), key=lambda c: c.ts)


async def collect_and_save(codes: list[str], db_path: str | None = None) -> dict[str, int]:
    """각 종목별 3분봉 수집 → SQLite 저장. 반환: {code: 저장건수}."""
    kis = KISClient()
    store = CandleStore(db_path)
    await store.open()
    stats: dict[str, int] = {}
    try:
        for code in codes:
            logger.info(f"[COLLECT] {code} ...")
            candles = await collect_intraday(kis, code)
            for c in candles:
                await store.save(c)
            stats[code] = len(candles)
            logger.info(f"[COLLECT] {code}: {len(candles)}개 저장")
            await asyncio.sleep(0.3)
    finally:
        await store.close()
        await kis.close()
    return stats


if __name__ == "__main__":
    import sys
    codes = sys.argv[1].split(",") if len(sys.argv) > 1 else ["005930", "000660", "035720"]
    stats = asyncio.run(collect_and_save(codes))
    for k, v in stats.items():
        print(f"{k}: {v}")
