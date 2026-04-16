"""v6 백테스트 — v4 베이스 + 단계별 개선 토글.

step 1: ATR 기반 동적 포지션 사이징 (atr_avg20 / current_atr 비율)
step 2~4: 추후 구현 예정.

기존 v4 동작은 건드리지 않는다. v6은 별도 러너로 분리.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.report import save_equity_csv, save_trades_csv
from backtest.run_full import ascii_equity_curve, extended_summary
from backtest.run_v3 import gate_check
from core.kis_api import KISClient
from data.candle_store import CandleStore


def _build_config(eligible: set[str], step: int) -> BacktestConfig:
    kwargs: dict = {"eligible_codes": eligible}
    if step >= 1:
        kwargs["atr_sizing_mode"] = "dynamic"
    if step >= 2:
        kwargs["quality_sizing_mode"] = "on"
    # step >= 3, 4 자리 (추후 토글 추가)
    return BacktestConfig(**kwargs)


async def main(codes: list[str], step: int) -> None:
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

    print(f"\n[v6 step={step} | 게이트 결과]")
    for code in codes:
        gi = gate_info[code]
        mark = "PASS" if code in eligible else "SKIP"
        print(
            f"  {code} [{mark}]  MA20={gi['ma_short']:,.0f} vs MA60={gi['ma_long']:,.0f}"
            f"  | 외국인5일={gi['foreign_mwon']:+,}백만 기관5일={gi['institution_mwon']:+,}백만"
        )

    if not eligible:
        print("\n진입 후보 없음.")
        return

    store = CandleStore()
    await store.open()
    start = datetime(2000, 1, 1)
    end = datetime.now() + timedelta(days=1)
    data: dict[str, list] = {c: await store.load(c, start, end) for c in codes}
    await store.close()

    print("\n[데이터 로드]")
    for c, cs in data.items():
        if cs:
            print(f"  {c}: {len(cs)}봉")

    cfg = _build_config(eligible, step)
    print(
        f"\n[설정] atr_sizing_mode={cfg.atr_sizing_mode}  "
        f"quality_sizing_mode={cfg.quality_sizing_mode}"
    )

    engine = BacktestEngine(cfg)
    result = engine.run(data)

    print()
    extended_summary(result)
    ascii_equity_curve(result)

    Path("backtest/results").mkdir(parents=True, exist_ok=True)
    tag = "_".join(sorted(eligible))
    save_trades_csv(result, f"backtest/results/v6_step{step}_{tag}_trades.csv")
    save_equity_csv(result, f"backtest/results/v6_step{step}_{tag}_equity.csv")
    print(f"\n  저장: backtest/results/v6_step{step}_{tag}_*.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v6 백테스트 (단계별 개선)")
    parser.add_argument("codes", help="콤마 구분 종목코드 (예: 100790,005930)")
    parser.add_argument(
        "--step", type=int, default=1, choices=[1, 2, 3, 4],
        help="개선 단계 (1=동적 ATR 사이징)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.codes.split(","), args.step))
