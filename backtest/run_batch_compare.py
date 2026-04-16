"""v1 vs v4(임계1) vs v4(임계2) 3종 비교 배치.

사용:
    python -m backtest.run_batch_compare 100790,440110,028300 500,300

첫 인자: 종목코드(쉼표) 또는 파일
둘째 인자(선택): 수급 임계값(백만원) 두 개 (기본 "500,300")
"""
from __future__ import annotations

import asyncio
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.run_batch import _parse_codes_arg, _stats, ensure_candles
from backtest.run_v3 import gate_check
from core.kis_api import KISClient
from data.candle_store import CandleStore
from data.flow_data import get_recent_flow
from data.models import Candle
from data.daily_data import daily_ma_passed
from data.stock_master import StockMaster

MIN_BARS = 500


def _run(code: str, candles: list[Candle], eligible_codes: set[str] | None) -> BacktestResult:
    cfg = BacktestConfig(eligible_codes=eligible_codes)
    return BacktestEngine(cfg).run({code: candles})


async def main(codes: list[str], thresholds: tuple[int, int]) -> None:
    th_a, th_b = thresholds
    sm = StockMaster()
    await sm._ensure_loaded()

    # 1) 수집
    store = CandleStore()
    await store.open()
    candle_map: dict[str, list[Candle]] = {}
    try:
        for code in codes:
            candle_map[code] = await ensure_candles(code, store)
            await asyncio.sleep(0.3)
    finally:
        await store.close()

    # 2) 일봉MA + 수급 한 번만 조회 (임계값은 나중에 로컬 비교)
    kis = KISClient()
    gate_raw: dict[str, dict] = {}
    try:
        for code in codes:
            ma_ok, ma_s, ma_l = await daily_ma_passed(kis, code)
            flows = await get_recent_flow(kis, code)
            f_sum = sum(f.foreign_mwon for f in flows)
            i_sum = sum(f.institution_mwon for f in flows)
            gate_raw[code] = {
                "ma_ok": ma_ok, "ma_short": ma_s, "ma_long": ma_l,
                "foreign_mwon": f_sum, "institution_mwon": i_sum,
            }
            await asyncio.sleep(0.3)
    finally:
        await kis.close()

    def gate_at(code: str, th: int) -> bool:
        g = gate_raw[code]
        flow_ok = max(g["foreign_mwon"], g["institution_mwon"]) >= th
        return bool(g["ma_ok"] and flow_ok)

    # 3) 종목별 3-way
    rows: list[dict] = []
    for code in codes:
        candles = candle_map[code]
        name = sm._by_code.get(code, "")
        if len(candles) < MIN_BARS:
            rows.append({"code": code, "name": name, "skip": True, "bars": len(candles)})
            continue
        pass_a = gate_at(code, th_a)
        pass_b = gate_at(code, th_b)
        r_v1 = _run(code, candles, None)
        r_a = _run(code, candles, {code} if pass_a else set())
        r_b = _run(code, candles, {code} if pass_b else set())
        rows.append({
            "code": code, "name": name, "skip": False, "bars": len(candles),
            "gate_raw": gate_raw[code],
            "pass_a": pass_a, "pass_b": pass_b,
            "v1": _stats(r_v1), "a": _stats(r_a), "b": _stats(r_b),
        })

    # 4) 출력
    print("\n" + "=" * 130)
    print(f"{'종목':<20} {'외인':>8} {'기관':>8}  "
          f"{'G'+str(th_a):>4} {'G'+str(th_b):>4}  "
          f"{'v1 수익':>8} {'v4@'+str(th_a)+' 수익':>10} {'v4@'+str(th_b)+' 수익':>10}  "
          f"{'v1 MDD':>8} {'a MDD':>8} {'b MDD':>8}  "
          f"{'v1 거래':>6} {'a 거래':>6} {'b 거래':>6}")
    print("-" * 130)
    for r in rows:
        if r["skip"]:
            print(f"{r['code']+' '+r['name']:<20} [데이터부족: {r['bars']}봉]")
            continue
        g = r["gate_raw"]
        v1, a, b = r["v1"], r["a"], r["b"]
        print(f"{r['code']+' '+r['name']:<20} "
              f"{g['foreign_mwon']:>+8,} {g['institution_mwon']:>+8,}  "
              f"{'P' if r['pass_a'] else 'F':>4} {'P' if r['pass_b'] else 'F':>4}  "
              f"{v1['ret_pct']:>+7.2f}% {a['ret_pct']:>+9.2f}% {b['ret_pct']:>+9.2f}%  "
              f"{v1['mdd_pct']:>+7.2f}% {a['mdd_pct']:>+7.2f}% {b['mdd_pct']:>+7.2f}%  "
              f"{v1['trades']:>6d} {a['trades']:>6d} {b['trades']:>6d}")

    # 5) 요약
    active = [r for r in rows if not r["skip"]]
    if active:
        def avg(xs): return sum(xs) / len(xs)
        v1r = [r["v1"]["ret_pct"] for r in active]
        ar = [r["a"]["ret_pct"] for r in active]
        br = [r["b"]["ret_pct"] for r in active]
        v1m = [r["v1"]["mdd_pct"] for r in active]
        am = [r["a"]["mdd_pct"] for r in active]
        bm = [r["b"]["mdd_pct"] for r in active]
        pass_a = sum(1 for r in active if r["pass_a"])
        pass_b = sum(1 for r in active if r["pass_b"])
        diff = sum(1 for r in active if r["pass_a"] != r["pass_b"])
        print("-" * 130)
        print(f"[요약] N={len(active)}  PASS  v4@{th_a}={pass_a}  v4@{th_b}={pass_b}  (통과여부 달라진 종목: {diff})")
        print(f"       평균 수익률 — v1 {avg(v1r):+.2f}%  "
              f"v4@{th_a} {avg(ar):+.2f}%  v4@{th_b} {avg(br):+.2f}%")
        print(f"       평균 MDD    — v1 {avg(v1m):+.2f}%  "
              f"v4@{th_a} {avg(am):+.2f}%  v4@{th_b} {avg(bm):+.2f}%")
        print("=" * 130)

    # 6) CSV
    out = Path("backtest/results/batch_compare.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "code", "name", "foreign_mwon", "institution_mwon",
            f"pass@{th_a}", f"pass@{th_b}",
            "v1_ret", "v1_trades", "v1_win", "v1_mdd",
            f"v4@{th_a}_ret", f"v4@{th_a}_trades", f"v4@{th_a}_win", f"v4@{th_a}_mdd",
            f"v4@{th_b}_ret", f"v4@{th_b}_trades", f"v4@{th_b}_win", f"v4@{th_b}_mdd",
        ])
        for r in rows:
            if r["skip"]:
                w.writerow([r["code"], r["name"], "", "", "", "", ""] + [""] * 12)
                continue
            g = r["gate_raw"]
            v1, a, b = r["v1"], r["a"], r["b"]
            w.writerow([
                r["code"], r["name"], g["foreign_mwon"], g["institution_mwon"],
                "P" if r["pass_a"] else "F", "P" if r["pass_b"] else "F",
                f"{v1['ret_pct']:.3f}", v1["trades"], f"{v1['win_rate']:.1f}", f"{v1['mdd_pct']:.3f}",
                f"{a['ret_pct']:.3f}", a["trades"], f"{a['win_rate']:.1f}", f"{a['mdd_pct']:.3f}",
                f"{b['ret_pct']:.3f}", b["trades"], f"{b['win_rate']:.1f}", f"{b['mdd_pct']:.3f}",
            ])
    print(f"\n저장: {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python -m backtest.run_batch_compare CODES [TH_A,TH_B]")
        sys.exit(1)
    codes = _parse_codes_arg(sys.argv[1])
    th_arg = sys.argv[2] if len(sys.argv) > 2 else "500,300"
    a, b = [int(x) for x in th_arg.split(",")]
    asyncio.run(main(codes, (a, b)))
