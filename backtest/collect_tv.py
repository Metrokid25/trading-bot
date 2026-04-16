"""tvDatafeed 로 TradingView 3분봉 히스토리컬 데이터 수집 → SQLite 저장.

TradingView 는 KRX:028300 같은 심볼 체계를 사용한다. 코스피/코스닥 모두 exchange="KRX".
익명(비로그인) 모드에서도 데이터를 가져올 수 있으나 n_bars 상한이 낮을 수 있어,
안전하게 5000봉 단위로 요청한다. (3분봉 × 5000 ≒ 약 38거래일)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Iterable

import pandas as pd
from loguru import logger
from tvDatafeed import Interval, TvDatafeed

from data.candle_store import CandleStore
from data.models import Candle


def fetch_3m(symbol: str, exchange: str = "KRX", n_bars: int = 5000,
             username: str | None = None, password: str | None = None) -> pd.DataFrame:
    tv = TvDatafeed(username=username, password=password) if username else TvDatafeed()
    df = tv.get_hist(
        symbol=symbol,
        exchange=exchange,
        interval=Interval.in_3_minute,
        n_bars=n_bars,
        extended_session=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Seoul").tz_localize(None)
    return df


def to_candles(df: pd.DataFrame, code: str) -> list[Candle]:
    out: list[Candle] = []
    for ts, row in df.iterrows():
        try:
            vol = int(row["volume"]) if pd.notna(row["volume"]) else 0
        except Exception:
            vol = 0
        out.append(Candle(
            code=code,
            ts=ts.to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=vol,
        ))
    return out


async def collect(code: str, symbol: str | None = None, exchange: str = "KRX",
                  n_bars: int = 5000) -> int:
    sym = symbol or code
    logger.info(f"[TV] {exchange}:{sym} 3분봉 fetch (n_bars={n_bars}) ...")
    df = fetch_3m(sym, exchange=exchange, n_bars=n_bars)
    if df.empty:
        logger.warning(f"{exchange}:{sym}: 데이터 없음")
        return 0

    candles = to_candles(df, code)
    store = CandleStore()
    await store.open()
    try:
        for c in candles:
            await store.save(c)
    finally:
        await store.close()
    logger.info(f"[TV] {code}: 3분봉 {len(candles)}개 저장 "
                f"({candles[0].ts} ~ {candles[-1].ts})")
    return len(candles)


async def collect_many(pairs: Iterable[tuple[str, str]], exchange: str = "KRX",
                       n_bars: int = 5000) -> dict[str, int]:
    stats: dict[str, int] = {}
    for code, symbol in pairs:
        stats[code] = await collect(code, symbol=symbol, exchange=exchange, n_bars=n_bars)
        await asyncio.sleep(1.0)
    return stats


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "028300"
    symbol = sys.argv[2] if len(sys.argv) > 2 else code
    n_bars = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
    asyncio.run(collect(code, symbol=symbol, n_bars=n_bars))
