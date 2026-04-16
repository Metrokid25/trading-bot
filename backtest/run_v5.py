"""v5 백테스트: v4 게이트 + PULLBACK/BREAKOUT 듀얼 시그널."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.report import save_equity_csv, save_trades_csv
from backtest.run_full import ascii_equity_curve, extended_summary
from backtest.run_v3 import gate_check
from core.kis_api import KISClient
from data.candle_store import CandleStore


async def main(codes: list[str]) -> None:
    kis = KISClient()
    eligible: set[str] = set()
    gate_info: dict[str, dict] = {}
    try:
        for code in codes:
            passed, info = await gate_check(kis, code)
            gate_info[code] = info
            if passed:
                eligible.add(code)
    finally:
        await kis.close()

    print("\n[게이트 결과]")
    for code in codes:
        gi = gate_info[code]
        mark = "PASS" if code in eligible else "SKIP"
        print(f"  {code} [{mark}]  MA20={gi['ma_short']:,.0f} vs MA60={gi['ma_long']:,.0f}"
              f"  | 외국인5일={gi['foreign_mwon']:+,}백만 기관5일={gi['institution_mwon']:+,}백만")

    if not eligible:
        print("\n진입 후보 없음.")
        return

    store = CandleStore()
    await store.open()
    start = datetime(2000, 1, 1)
    end = datetime.now() + timedelta(days=1)
    data = {c: await store.load(c, start, end) for c in codes}
    await store.close()

    engine = BacktestEngine(BacktestConfig(eligible_codes=eligible, allow_breakout=True))
    result = engine.run(data)

    print()
    extended_summary(result)
    ascii_equity_curve(result)

    # PULLBACK/BREAKOUT 진입 카운트
    pb = sum(1 for t in result.trades if t.side == "BUY" and t.reason.startswith("[PULLBACK]"))
    bo = sum(1 for t in result.trades if t.side == "BUY" and t.reason.startswith("[BREAKOUT]"))
    print(f"  진입 종류 : PULLBACK {pb}건 / BREAKOUT {bo}건")

    Path("backtest/results").mkdir(parents=True, exist_ok=True)
    tag = "_".join(sorted(eligible))
    save_trades_csv(result, f"backtest/results/v5_{tag}_trades.csv")
    save_equity_csv(result, f"backtest/results/v5_{tag}_equity.csv")
    print(f"\n  저장: backtest/results/v5_{tag}_*.csv")


if __name__ == "__main__":
    codes = sys.argv[1].split(",") if len(sys.argv) > 1 else ["100790"]
    asyncio.run(main(codes))
