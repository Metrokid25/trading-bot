"""v3 백테스트 러너: 일봉 MA + 5일 수급 게이트 → 통과 시 3분봉 백테스트.

사용법:
    python -m backtest.run_v3 100790
    python -m backtest.run_v3 100790,028300,440110
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.report import save_equity_csv, save_trades_csv
from backtest.run_full import ascii_equity_curve, extended_summary
from core.kis_api import KISClient
from data.candle_store import CandleStore
from data.daily_data import daily_ma_passed
from data.flow_data import flow_passed


async def gate_check(
    kis: KISClient, code: str,
    flow_threshold_mwon: int | None = None,
) -> tuple[bool, dict]:
    """일봉 MA + 수급 게이트. 둘 다 PASS 면 진입 후보.

    flow_threshold_mwon: None 이면 config.constants.FLOW_THRESHOLD_MWON 사용.
    """
    from config.constants import FLOW_THRESHOLD_MWON
    th = FLOW_THRESHOLD_MWON if flow_threshold_mwon is None else flow_threshold_mwon
    ma_ok, ma_s, ma_l = await daily_ma_passed(kis, code)
    fl_ok, f_sum, i_sum = await flow_passed(kis, code, threshold_mwon=th)
    info = {
        "ma_short": ma_s, "ma_long": ma_l, "ma_ok": ma_ok,
        "foreign_mwon": f_sum, "institution_mwon": i_sum, "flow_ok": fl_ok,
        "flow_threshold": th,
    }
    return (ma_ok and fl_ok), info


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
        print(
            f"  {code} [{mark}]  MA20={gi['ma_short']:,.0f} vs MA60={gi['ma_long']:,.0f}"
            f"  | 외국인5일={gi['foreign_mwon']:+,}백만 기관5일={gi['institution_mwon']:+,}백만"
        )

    if not eligible:
        print("\n진입 후보 없음 — 백테스트 건너뜀.")
        return

    # 3분봉 로드
    store = CandleStore()
    await store.open()
    start = datetime(2000, 1, 1)
    end = datetime.now() + timedelta(days=1)
    data: dict[str, list] = {}
    for code in codes:
        data[code] = await store.load(code, start, end)
    await store.close()

    print("\n[데이터 로드]")
    for c, cs in data.items():
        if cs:
            print(f"  {c}: {len(cs)}봉 ({cs[0].ts} ~ {cs[-1].ts})")

    engine = BacktestEngine(BacktestConfig(eligible_codes=eligible))
    result = engine.run(data)

    print()
    extended_summary(result)
    ascii_equity_curve(result)

    Path("backtest/results").mkdir(parents=True, exist_ok=True)
    tag = "_".join(sorted(eligible))
    save_trades_csv(result, f"backtest/results/v3_{tag}_trades.csv")
    save_equity_csv(result, f"backtest/results/v3_{tag}_equity.csv")
    print(f"\n  저장: backtest/results/v3_{tag}_*.csv")


if __name__ == "__main__":
    codes = sys.argv[1].split(",") if len(sys.argv) > 1 else ["100790"]
    asyncio.run(main(codes))
