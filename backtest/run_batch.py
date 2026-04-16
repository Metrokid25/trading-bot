"""30종목 일괄 백테스트 — v1 vs v4 비교.

사용:
    python -m backtest.run_batch 100790,440110,028300,...
    python -m backtest.run_batch codes.txt   # 줄바꿈 구분

흐름:
  1) tvDatafeed 로 3분봉 5000봉 수집 (DB 에 이미 있으면 재사용)
  2) KIS 로 일봉 MA + 5일 수급 게이트 체크
  3) 종목별 v1(게이트 없음) / v4(게이트 있음) 각각 백테스트
  4) 비교표 + 요약통계 + CSV 저장
"""
from __future__ import annotations

import asyncio
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from backtest.collect_tv import collect as tv_collect
from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.run_v3 import gate_check
from core.kis_api import KISClient
from data.candle_store import CandleStore
from data.models import Candle
from data.stock_master import StockMaster

MIN_BARS_REQUIRED = 500   # 이 미만이면 재수집
TV_FETCH_N_BARS = 5000


# -------------------- 수집 --------------------
async def ensure_candles(code: str, store: CandleStore) -> list[Candle]:
    """DB 에 충분히 있으면 재사용, 부족하면 tvDatafeed 에서 5000봉 수집."""
    start = datetime(2000, 1, 1)
    end = datetime.now() + timedelta(days=1)
    existing = await store.load(code, start, end)
    if len(existing) >= MIN_BARS_REQUIRED:
        return existing
    logger.info(f"[BATCH] {code}: DB {len(existing)}봉 → tv 재수집")
    await store.close()
    try:
        await tv_collect(code, symbol=code, n_bars=TV_FETCH_N_BARS)
    finally:
        await store.open()
    return await store.load(code, start, end)


# -------------------- 백테스트 헬퍼 --------------------
def run_backtest(code: str, candles: list[Candle], use_gate: bool,
                 gate_pass: bool) -> BacktestResult:
    data = {code: candles}
    if use_gate:
        eligible = {code} if gate_pass else set()
    else:
        eligible = None
    cfg = BacktestConfig(eligible_codes=eligible)
    return BacktestEngine(cfg).run(data)


def _stats(r: BacktestResult) -> dict:
    sells = [t for t in r.trades if t.side == "SELL"]
    wins = [t for t in sells if t.pnl > 0]
    losses = [t for t in sells if t.pnl <= 0]
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)
    return {
        "ret_pct": r.total_return_pct,
        "pnl": sum(t.pnl for t in sells),
        "trades": len(sells),
        "win_rate": r.win_rate,
        "rr": rr,
        "mdd_pct": r.mdd_pct,
    }


# -------------------- 메인 --------------------
async def main(codes: list[str]) -> None:
    sm = StockMaster()
    await sm._ensure_loaded()

    # 1) 수집
    store = CandleStore()
    await store.open()
    candle_map: dict[str, list[Candle]] = {}
    try:
        for code in codes:
            candle_map[code] = await ensure_candles(code, store)
            await asyncio.sleep(1.0)
    finally:
        await store.close()

    # 2) 게이트
    kis = KISClient()
    gate_info: dict[str, dict] = {}
    try:
        for code in codes:
            passed, info = await gate_check(kis, code)
            gate_info[code] = {**info, "passed": passed}
            await asyncio.sleep(0.3)
    finally:
        await kis.close()

    # 3) 종목별 v1 / v4
    rows: list[dict] = []
    for code in codes:
        candles = candle_map[code]
        name = sm._by_code.get(code, "")
        if len(candles) < MIN_BARS_REQUIRED:
            logger.warning(f"[BATCH] {code}: 데이터 부족 ({len(candles)}봉) — skip")
            rows.append({"code": code, "name": name, "bars": len(candles), "skip": True,
                         "gate": "-", "gate_f": 0, "gate_i": 0})
            continue
        gi = gate_info[code]
        gate_pass = gi["passed"]
        r1 = run_backtest(code, candles, use_gate=False, gate_pass=gate_pass)
        r4 = run_backtest(code, candles, use_gate=True, gate_pass=gate_pass)
        s1 = _stats(r1)
        s4 = _stats(r4)
        rows.append({
            "code": code, "name": name, "bars": len(candles), "skip": False,
            "gate": "PASS" if gate_pass else "FAIL",
            "gate_f": gi["foreign_mwon"], "gate_i": gi["institution_mwon"],
            "v1": s1, "v4": s4,
        })

    # 4) 표 출력
    print("\n" + "=" * 120)
    print(f"{'종목':<20} {'게이트':<6} {'외인':>8} {'기관':>8}  "
          f"{'v1 수익':>8} {'v4 수익':>8}  {'v1 승률':>7} {'v4 승률':>7}  "
          f"{'v1 MDD':>8} {'v4 MDD':>8}  {'v1 거래':>6} {'v4 거래':>6}")
    print("-" * 120)
    for row in rows:
        if row["skip"]:
            print(f"{row['code']+' '+row['name']:<20} [데이터부족: {row['bars']}봉]")
            continue
        s1, s4 = row["v1"], row["v4"]
        print(f"{row['code']+' '+row['name']:<20} {row['gate']:<6} "
              f"{row['gate_f']:>+8,} {row['gate_i']:>+8,}  "
              f"{s1['ret_pct']:>+7.2f}% {s4['ret_pct']:>+7.2f}%  "
              f"{s1['win_rate']:>6.1f}% {s4['win_rate']:>6.1f}%  "
              f"{s1['mdd_pct']:>+7.2f}% {s4['mdd_pct']:>+7.2f}%  "
              f"{s1['trades']:>6d} {s4['trades']:>6d}")

    # 5) 요약
    active = [r for r in rows if not r["skip"]]
    if active:
        v1_rets = [r["v1"]["ret_pct"] for r in active]
        v4_rets = [r["v4"]["ret_pct"] for r in active]
        v1_mdd = [r["v1"]["mdd_pct"] for r in active]
        v4_mdd = [r["v4"]["mdd_pct"] for r in active]
        pass_cnt = sum(1 for r in active if r["gate"] == "PASS")
        better_cnt = sum(1 for r in active if r["v4"]["ret_pct"] > r["v1"]["ret_pct"])
        same_cnt = sum(1 for r in active
                       if abs(r["v4"]["ret_pct"] - r["v1"]["ret_pct"]) < 1e-9)
        worse_cnt = len(active) - better_cnt - same_cnt
        print("-" * 120)
        print(f"[요약]  종목수 {len(active)}개 / 게이트 PASS {pass_cnt} / FAIL {len(active)-pass_cnt}")
        print(f"        평균 수익률 — v1 {sum(v1_rets)/len(v1_rets):+.2f}%  "
              f"v4 {sum(v4_rets)/len(v4_rets):+.2f}%")
        print(f"        평균 MDD    — v1 {sum(v1_mdd)/len(v1_mdd):+.2f}%  "
              f"v4 {sum(v4_mdd)/len(v4_mdd):+.2f}%")
        print(f"        v4 > v1  : {better_cnt}  /  동일 : {same_cnt}  /  v4 < v1 : {worse_cnt}")
        print("=" * 120)

    # 6) CSV 저장
    Path("backtest/results").mkdir(parents=True, exist_ok=True)
    out_path = Path("backtest/results/batch_summary.csv")
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "code", "name", "bars", "gate", "foreign_mwon", "institution_mwon",
            "v1_ret_pct", "v1_pnl", "v1_trades", "v1_win_rate", "v1_rr", "v1_mdd_pct",
            "v4_ret_pct", "v4_pnl", "v4_trades", "v4_win_rate", "v4_rr", "v4_mdd_pct",
        ])
        for r in rows:
            if r["skip"]:
                w.writerow([r["code"], r["name"], r["bars"], "-", 0, 0] + [""] * 12)
                continue
            s1, s4 = r["v1"], r["v4"]
            w.writerow([
                r["code"], r["name"], r["bars"], r["gate"], r["gate_f"], r["gate_i"],
                f"{s1['ret_pct']:.3f}", f"{s1['pnl']:.0f}", s1["trades"],
                f"{s1['win_rate']:.1f}", f"{s1['rr']:.2f}", f"{s1['mdd_pct']:.3f}",
                f"{s4['ret_pct']:.3f}", f"{s4['pnl']:.0f}", s4["trades"],
                f"{s4['win_rate']:.1f}", f"{s4['rr']:.2f}", f"{s4['mdd_pct']:.3f}",
            ])
    print(f"\n저장: {out_path}")


def _parse_codes_arg(arg: str) -> list[str]:
    p = Path(arg)
    if p.exists() and p.is_file():
        return [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c.strip() for c in arg.split(",") if c.strip()]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python -m backtest.run_batch 100790,440110,028300,...")
        print("     python -m backtest.run_batch codes.txt")
        sys.exit(1)
    codes = _parse_codes_arg(sys.argv[1])
    asyncio.run(main(codes))
