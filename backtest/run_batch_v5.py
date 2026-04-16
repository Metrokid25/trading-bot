"""v1 / v4 / v5 3종 배치 비교.

사용:
    python -m backtest.run_batch_v5 100790,440110,028300,...
"""
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.run_batch import _parse_codes_arg, _stats, ensure_candles
from backtest.run_v3 import gate_check
from core.kis_api import KISClient
from data.candle_store import CandleStore
from data.models import Candle
from data.stock_master import StockMaster

MIN_BARS = 500


def _run(code: str, candles: list[Candle], eligible: set[str] | None,
         allow_breakout: bool) -> BacktestResult:
    cfg = BacktestConfig(eligible_codes=eligible, allow_breakout=allow_breakout)
    return BacktestEngine(cfg).run({code: candles})


async def main(codes: list[str]) -> None:
    sm = StockMaster()
    await sm._ensure_loaded()

    store = CandleStore()
    await store.open()
    candle_map: dict[str, list[Candle]] = {}
    try:
        for code in codes:
            candle_map[code] = await ensure_candles(code, store)
            await asyncio.sleep(0.3)
    finally:
        await store.close()

    kis = KISClient()
    gate_info: dict[str, dict] = {}
    try:
        for code in codes:
            passed, info = await gate_check(kis, code)
            gate_info[code] = {**info, "passed": passed}
            await asyncio.sleep(0.3)
    finally:
        await kis.close()

    rows: list[dict] = []
    for code in codes:
        candles = candle_map[code]
        name = sm._by_code.get(code, "")
        if len(candles) < MIN_BARS:
            rows.append({"code": code, "name": name, "skip": True, "bars": len(candles)})
            continue
        gi = gate_info[code]
        eligible = {code} if gi["passed"] else set()

        r1 = _run(code, candles, None, False)                  # v1: 게이트 X, PULLBACK
        r4 = _run(code, candles, eligible, False)              # v4: 게이트 O, PULLBACK
        r5 = _run(code, candles, eligible, True)               # v5: 게이트 O, PULLBACK+BREAKOUT

        # v5 의 PULLBACK/BREAKOUT 진입 카운트
        v5_pb = sum(1 for t in r5.trades if t.side == "BUY" and t.reason.startswith("[PULLBACK]"))
        v5_bo = sum(1 for t in r5.trades if t.side == "BUY" and t.reason.startswith("[BREAKOUT]"))

        rows.append({
            "code": code, "name": name, "skip": False, "bars": len(candles),
            "gate": "PASS" if gi["passed"] else "FAIL",
            "gate_f": gi["foreign_mwon"], "gate_i": gi["institution_mwon"],
            "v1": _stats(r1), "v4": _stats(r4), "v5": _stats(r5),
            "v5_pb": v5_pb, "v5_bo": v5_bo,
        })

    # 표
    print("\n" + "=" * 140)
    print(f"{'종목':<20} {'게이트':<6} "
          f"{'v1 수익':>8} {'v4 수익':>8} {'v5 수익':>8}  "
          f"{'v1 MDD':>8} {'v4 MDD':>8} {'v5 MDD':>8}  "
          f"{'v1 거래':>6} {'v4 거래':>6} {'v5 거래':>6}  "
          f"{'v5 P/B':>8}")
    print("-" * 140)
    for r in rows:
        if r["skip"]:
            print(f"{r['code']+' '+r['name']:<20} [데이터부족: {r['bars']}봉]")
            continue
        v1, v4, v5 = r["v1"], r["v4"], r["v5"]
        print(f"{r['code']+' '+r['name']:<20} {r['gate']:<6} "
              f"{v1['ret_pct']:>+7.2f}% {v4['ret_pct']:>+7.2f}% {v5['ret_pct']:>+7.2f}%  "
              f"{v1['mdd_pct']:>+7.2f}% {v4['mdd_pct']:>+7.2f}% {v5['mdd_pct']:>+7.2f}%  "
              f"{v1['trades']:>6d} {v4['trades']:>6d} {v5['trades']:>6d}  "
              f"{r['v5_pb']:>3d}/{r['v5_bo']:<3d}")

    active = [r for r in rows if not r["skip"]]
    if active:
        def avg(xs): return sum(xs) / len(xs)
        v1r = [r["v1"]["ret_pct"] for r in active]
        v4r = [r["v4"]["ret_pct"] for r in active]
        v5r = [r["v5"]["ret_pct"] for r in active]
        v1m = [r["v1"]["mdd_pct"] for r in active]
        v4m = [r["v4"]["mdd_pct"] for r in active]
        v5m = [r["v5"]["mdd_pct"] for r in active]
        pass_cnt = sum(1 for r in active if r["gate"] == "PASS")
        v5_gt_v4 = sum(1 for r in active if r["v5"]["ret_pct"] > r["v4"]["ret_pct"])
        v5_eq_v4 = sum(1 for r in active if abs(r["v5"]["ret_pct"] - r["v4"]["ret_pct"]) < 1e-9)
        v5_lt_v4 = len(active) - v5_gt_v4 - v5_eq_v4
        tot_pb = sum(r["v5_pb"] for r in active)
        tot_bo = sum(r["v5_bo"] for r in active)
        print("-" * 140)
        print(f"[요약] N={len(active)}  게이트 PASS={pass_cnt}  FAIL={len(active)-pass_cnt}")
        print(f"       평균 수익률 — v1 {avg(v1r):+.2f}%  v4 {avg(v4r):+.2f}%  v5 {avg(v5r):+.2f}%")
        print(f"       평균 MDD    — v1 {avg(v1m):+.2f}%  v4 {avg(v4m):+.2f}%  v5 {avg(v5m):+.2f}%")
        print(f"       v5 > v4 : {v5_gt_v4}  /  동일 : {v5_eq_v4}  /  v5 < v4 : {v5_lt_v4}")
        print(f"       v5 전체 BUY — PULLBACK {tot_pb}건 / BREAKOUT {tot_bo}건")
        print("=" * 140)

    out = Path("backtest/results/batch_v5.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "code", "name", "gate", "foreign_mwon", "institution_mwon",
            "v1_ret", "v1_trades", "v1_win", "v1_mdd",
            "v4_ret", "v4_trades", "v4_win", "v4_mdd",
            "v5_ret", "v5_trades", "v5_win", "v5_mdd",
            "v5_pullback", "v5_breakout",
        ])
        for r in rows:
            if r["skip"]:
                w.writerow([r["code"], r["name"], "", "", ""] + [""] * 14)
                continue
            v1, v4, v5 = r["v1"], r["v4"], r["v5"]
            w.writerow([
                r["code"], r["name"], r["gate"], r["gate_f"], r["gate_i"],
                f"{v1['ret_pct']:.3f}", v1["trades"], f"{v1['win_rate']:.1f}", f"{v1['mdd_pct']:.3f}",
                f"{v4['ret_pct']:.3f}", v4["trades"], f"{v4['win_rate']:.1f}", f"{v4['mdd_pct']:.3f}",
                f"{v5['ret_pct']:.3f}", v5["trades"], f"{v5['win_rate']:.1f}", f"{v5['mdd_pct']:.3f}",
                r["v5_pb"], r["v5_bo"],
            ])
    print(f"\n저장: {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python -m backtest.run_batch_v5 100790,440110,028300,...")
        sys.exit(1)
    codes = _parse_codes_arg(sys.argv[1])
    asyncio.run(main(codes))
