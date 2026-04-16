"""v4 vs v6 step1 vs v6 step2 배치 3-way 비교.

사용:
    python -m backtest.run_batch_v6 100790,440110,028300,...
    python -m backtest.run_batch_v6 codes.txt
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
         atr_sizing_mode: str = "fixed",
         quality_sizing_mode: str = "off") -> BacktestResult:
    cfg = BacktestConfig(
        eligible_codes=eligible,
        atr_sizing_mode=atr_sizing_mode,
        quality_sizing_mode=quality_sizing_mode,
    )
    return BacktestEngine(cfg).run({code: candles})


def _entry_pnl_map(r: BacktestResult) -> dict:
    """진입 ts → 그 진입에서 발생한 모든 SELL pnl 합."""
    out: dict = {}
    cur_ts = None
    for t in r.trades:
        if t.side == "BUY":
            cur_ts = (t.code, t.ts)
            out[cur_ts] = 0.0
        elif cur_ts is not None:
            out[cur_ts] += float(t.pnl)
    return out


def _per_entry_analysis(r4: BacktestResult, r1: BacktestResult,
                        r2: BacktestResult) -> dict:
    """진입별 매칭: v4_q, s1_q, s2_q → ATR 승수 / 품질 승수 / 품질 점수 / s2 PNL."""
    b4 = {(t.code, t.ts): t for t in r4.trades if t.side == "BUY"}
    b1 = {(t.code, t.ts): t for t in r1.trades if t.side == "BUY"}
    b2 = {(t.code, t.ts): t for t in r2.trades if t.side == "BUY"}
    pnl2 = _entry_pnl_map(r2)
    atr_mults: list[float] = []
    qual_mults: list[float] = []
    sigs: list[float] = []
    sig_pnl_pairs: list[tuple[float, float]] = []
    for k, t4 in b4.items():
        if t4.qty <= 0:
            continue
        t1 = b1.get(k)
        t2 = b2.get(k)
        if t1 is None or t2 is None or t1.qty <= 0:
            continue
        atr_mults.append(t1.qty / t4.qty)
        qm = t2.qty / t1.qty
        qual_mults.append(qm)
        sq = qm - 0.5  # quality_multiplier = 0.5 + signal_quality 역산
        sigs.append(sq)
        sig_pnl_pairs.append((sq, pnl2.get(k, 0.0)))
    return {
        "atr_mults": atr_mults,
        "qual_mults": qual_mults,
        "sigs": sigs,
        "sig_pnl_pairs": sig_pnl_pairs,
    }


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / (dx * dy) if dx * dy else float("nan")


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
    all_atr_mults: list[float] = []
    all_qual_mults: list[float] = []
    all_sigs: list[float] = []
    all_sig_pnl: list[tuple[float, float]] = []
    for code in codes:
        candles = candle_map[code]
        name = sm._by_code.get(code, "")
        if len(candles) < MIN_BARS:
            rows.append({"code": code, "name": name, "skip": True, "bars": len(candles)})
            continue
        gi = gate_info[code]
        eligible = {code} if gi["passed"] else set()

        r4 = _run(code, candles, eligible, "fixed", "off")
        r1 = _run(code, candles, eligible, "dynamic", "off")
        r2 = _run(code, candles, eligible, "dynamic", "on")

        pa = _per_entry_analysis(r4, r1, r2)
        all_atr_mults.extend(pa["atr_mults"])
        all_qual_mults.extend(pa["qual_mults"])
        all_sigs.extend(pa["sigs"])
        all_sig_pnl.extend(pa["sig_pnl_pairs"])

        rows.append({
            "code": code, "name": name, "skip": False,
            "gate": "PASS" if gi["passed"] else "FAIL",
            "v4": _stats(r4), "s1": _stats(r1), "s2": _stats(r2), "pa": pa,
        })

    # ---- 표 ----
    print("\n" + "=" * 148)
    print(f"{'종목':<22} {'게이트':<6} "
          f"{'v4 수익':>8} {'s1 수익':>8} {'s2 수익':>8}  "
          f"{'v4 MDD':>8} {'s1 MDD':>8} {'s2 MDD':>8}  "
          f"{'거래':>4}  "
          f"{'ATR승수':>7} {'품질점수':>8} {'품질승수':>8}")
    print("-" * 148)
    for r in rows:
        if r["skip"]:
            print(f"{r['code']+' '+r['name']:<22} [데이터부족: {r['bars']}봉]")
            continue
        v4, s1, s2 = r["v4"], r["s1"], r["s2"]
        pa = r["pa"]
        am = (sum(pa["atr_mults"]) / len(pa["atr_mults"])) if pa["atr_mults"] else float("nan")
        qm = (sum(pa["qual_mults"]) / len(pa["qual_mults"])) if pa["qual_mults"] else float("nan")
        sq = (sum(pa["sigs"]) / len(pa["sigs"])) if pa["sigs"] else float("nan")
        print(f"{r['code']+' '+r['name']:<22} {r['gate']:<6} "
              f"{v4['ret_pct']:>+7.2f}% {s1['ret_pct']:>+7.2f}% {s2['ret_pct']:>+7.2f}%  "
              f"{v4['mdd_pct']:>+7.2f}% {s1['mdd_pct']:>+7.2f}% {s2['mdd_pct']:>+7.2f}%  "
              f"{v4['trades']:>4d}  "
              f"{am:>7.3f} {sq:>8.3f} {qm:>8.3f}")

    # ---- 요약 ----
    active = [r for r in rows if not r["skip"]]
    if active:
        def avg(xs): return sum(xs) / len(xs) if xs else float("nan")
        v4r = [r["v4"]["ret_pct"] for r in active]
        s1r = [r["s1"]["ret_pct"] for r in active]
        s2r = [r["s2"]["ret_pct"] for r in active]
        v4m = [r["v4"]["mdd_pct"] for r in active]
        s1m = [r["s1"]["mdd_pct"] for r in active]
        s2m = [r["s2"]["mdd_pct"] for r in active]

        s2_better_v4 = sum(1 for r in active if r["s2"]["ret_pct"] > r["v4"]["ret_pct"] + 1e-9)
        s2_same_v4 = sum(1 for r in active if abs(r["s2"]["ret_pct"] - r["v4"]["ret_pct"]) < 1e-9)
        s2_worse_v4 = len(active) - s2_better_v4 - s2_same_v4
        s2_better_s1 = sum(1 for r in active if r["s2"]["ret_pct"] > r["s1"]["ret_pct"] + 1e-9)
        s2_same_s1 = sum(1 for r in active if abs(r["s2"]["ret_pct"] - r["s1"]["ret_pct"]) < 1e-9)
        s2_worse_s1 = len(active) - s2_better_s1 - s2_same_s1

        s2_mdd_better_v4 = sum(1 for r in active if r["s2"]["mdd_pct"] > r["v4"]["mdd_pct"] + 1e-9)
        s2_mdd_same_v4 = sum(1 for r in active if abs(r["s2"]["mdd_pct"] - r["v4"]["mdd_pct"]) < 1e-9)
        s2_mdd_worse_v4 = len(active) - s2_mdd_better_v4 - s2_mdd_same_v4

        n = len(all_sigs)
        # signal_quality 분포 히스토그램 (0.2 단위)
        bins = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.0001]
        hist = [0] * (len(bins) - 1)
        for s in all_sigs:
            for i in range(len(bins) - 1):
                if bins[i] <= s < bins[i + 1]:
                    hist[i] += 1
                    break

        sigs_sorted = sorted(all_sigs)
        median = sigs_sorted[n // 2] if n else float("nan")
        clip_lo = sum(1 for m in all_qual_mults if abs(m - 0.5) < 0.01)
        clip_hi = sum(1 for m in all_qual_mults if abs(m - 1.5) < 0.01)

        if all_sig_pnl:
            xs = [p[0] for p in all_sig_pnl]
            ys = [p[1] for p in all_sig_pnl]
            r_corr = _pearson(xs, ys)
        else:
            r_corr = float("nan")

        print("-" * 148)
        print(f"[요약] N={len(active)}")
        print(f"  평균 수익률 : v4 {avg(v4r):+.2f}%  s1 {avg(s1r):+.2f}%  s2 {avg(s2r):+.2f}%")
        print(f"  평균 MDD    : v4 {avg(v4m):+.2f}%  s1 {avg(s1m):+.2f}%  s2 {avg(s2m):+.2f}%")
        print(f"  s2 vs v4 (수익) - 개선 {s2_better_v4} 동일 {s2_same_v4} 악화 {s2_worse_v4}")
        print(f"  s2 vs s1 (수익) - 개선 {s2_better_s1} 동일 {s2_same_s1} 악화 {s2_worse_s1}")
        print(f"  s2 vs v4 (MDD ) - 개선 {s2_mdd_better_v4} 동일 {s2_mdd_same_v4} 악화 {s2_mdd_worse_v4}")
        print(f"\n  [품질 점수 분포] n={n}")
        print(f"    평균 {avg(all_sigs):.3f}  중앙 {median:.3f}  "
              f"min {min(all_sigs) if all_sigs else float('nan'):.3f}  "
              f"max {max(all_sigs) if all_sigs else float('nan'):.3f}")
        print(f"    [0.0,0.2)={hist[0]}  [0.2,0.4)={hist[1]}  [0.4,0.6)={hist[2]}  "
              f"[0.6,0.8)={hist[3]}  [0.8,1.0)={hist[4]}  [1.0]={hist[5]}")
        print(f"  [품질 승수] 평균 {avg(all_qual_mults):.3f}  "
              f"하한 0.5 클립 {clip_lo}건 ({clip_lo/n*100 if n else 0:.1f}%)  "
              f"상한 1.5 클립 {clip_hi}건 ({clip_hi/n*100 if n else 0:.1f}%)")
        print(f"  [상관] signal_quality vs s2_PNL : r = {r_corr:.3f}  (n={len(all_sig_pnl)})")
        print("=" * 148)

    # ---- CSV ----
    out = Path("backtest/results/batch_v6_compare.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "code", "name", "gate",
            "v4_ret", "v4_mdd", "v4_trades",
            "s1_ret", "s1_mdd",
            "s2_ret", "s2_mdd",
            "avg_atr_mult", "avg_signal_quality", "avg_quality_mult",
            "n_buys",
        ])
        for r in rows:
            if r["skip"]:
                w.writerow([r["code"], r["name"], ""] + [""] * 11)
                continue
            v4, s1, s2 = r["v4"], r["s1"], r["s2"]
            pa = r["pa"]
            am = (sum(pa["atr_mults"]) / len(pa["atr_mults"])) if pa["atr_mults"] else ""
            qm = (sum(pa["qual_mults"]) / len(pa["qual_mults"])) if pa["qual_mults"] else ""
            sq = (sum(pa["sigs"]) / len(pa["sigs"])) if pa["sigs"] else ""
            w.writerow([
                r["code"], r["name"], r["gate"],
                f"{v4['ret_pct']:.3f}", f"{v4['mdd_pct']:.3f}", v4["trades"],
                f"{s1['ret_pct']:.3f}", f"{s1['mdd_pct']:.3f}",
                f"{s2['ret_pct']:.3f}", f"{s2['mdd_pct']:.3f}",
                f"{am:.3f}" if am != "" else "",
                f"{sq:.3f}" if sq != "" else "",
                f"{qm:.3f}" if qm != "" else "",
                len(pa["atr_mults"]),
            ])
    print(f"\n저장: {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python -m backtest.run_batch_v6 100790,440110,...")
        sys.exit(1)
    codes = _parse_codes_arg(sys.argv[1])
    asyncio.run(main(codes))
