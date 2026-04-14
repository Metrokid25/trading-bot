"""백테스트 실행 예시.

사용법:
    python -m backtest.run_backtest 005930 2025-01-01 2025-03-01

히스토리컬 3분봉은 CandleStore(SQLite)에 미리 적재되어 있어야 한다.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.report import print_summary, save_equity_csv, save_trades_csv
from data.candle_store import CandleStore


async def main(codes: list[str], start: str, end: str) -> None:
    store = CandleStore()
    await store.open()
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    data = {c: await store.load(c, start_dt, end_dt) for c in codes}
    await store.close()

    engine = BacktestEngine(BacktestConfig())
    result = engine.run(data)
    print_summary(result)
    save_trades_csv(result, "backtest/results/trades.csv")
    save_equity_csv(result, "backtest/results/equity.csv")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("사용법: python -m backtest.run_backtest <CODE1,CODE2> <YYYY-MM-DD> <YYYY-MM-DD>")
        sys.exit(1)
    codes = sys.argv[1].split(",")
    asyncio.run(main(codes, sys.argv[2], sys.argv[3]))
