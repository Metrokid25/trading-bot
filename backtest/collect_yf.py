"""yfinance로 1분봉 수집 → 3분봉 리샘플 → SQLite 저장."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import yfinance as yf
from loguru import logger

from data.candle_store import CandleStore
from data.models import Candle


def fetch_1m(ticker: str, period: str = "7d") -> pd.DataFrame:
    """yfinance 1분봉. period: 최대 7d (1m 한정)."""
    df = yf.download(ticker, period=period, interval="1m",
                     auto_adjust=False, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    # KST로 변환
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Seoul").tz_localize(None)
    return df


def resample_3m(df: pd.DataFrame) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    out = df.resample("3min", label="left", closed="left").agg(agg).dropna(subset=["Open"])
    return out


def to_candles(df: pd.DataFrame, code: str) -> list[Candle]:
    out: list[Candle] = []
    for ts, row in df.iterrows():
        try:
            vol = int(row["Volume"]) if pd.notna(row["Volume"]) else 0
        except Exception:
            vol = 0
        out.append(Candle(
            code=code,
            ts=ts.to_pydatetime(),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=vol,
        ))
    return out


async def collect(code: str, ticker: str, period: str = "7d") -> int:
    logger.info(f"[YF] {ticker} 1분봉 fetch ...")
    df1 = fetch_1m(ticker, period)
    if df1.empty:
        logger.warning(f"{ticker}: 데이터 없음")
        return 0
    logger.info(f"[YF] 1분봉 {len(df1)}개 → 3분봉 리샘플")
    df3 = resample_3m(df1)
    candles = to_candles(df3, code)

    store = CandleStore()
    await store.open()
    try:
        for c in candles:
            await store.save(c)
    finally:
        await store.close()
    logger.info(f"[YF] {code}: 3분봉 {len(candles)}개 저장 ({candles[0].ts} ~ {candles[-1].ts})")
    return len(candles)


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "046970"
    ticker = sys.argv[2] if len(sys.argv) > 2 else f"{code}.KQ"
    asyncio.run(collect(code, ticker))
