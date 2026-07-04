"""gm_v3 룰 엔진 백테스트 러너 — 토스 캐시 일봉 + 페이퍼 체결 + 시그널 로깅.

데이터: db/toss_candles.db 1분봉 → 정규장 일봉 합성 (실데이터).
       --kis-backfill N 으로 KIS 과거 일봉 보충(60일선 워밍업), 실패/부족 시
       --synth-pad N 합성 패딩(더미 — 리포트에 명시).
체결: 기본 '다음날 시가' (R10 손절만 당일 스탑 체결). --fill close 전환 가능.
로깅: --log 시 gm_v3_signals(m010) 테이블 적재 (run_id 멱등).

사용:
  ./.venv/Scripts/python.exe backtest/run_gm_v3.py --start 2026-04-01 --end 2026-06-27
  ... --r2 --fill close --log --codes 005930,042700
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from strategy.gm_v3 import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import (  # noqa: E402
    kis_backfill_daily, load_daily_from_toss, synth_pad,
)
from strategy.gm_v3.paper import PaperTrade, simulate  # noqa: E402
from strategy.gm_v3.signal_log import log_signals  # noqa: E402


def _stock_universe(codes: list[str] | None) -> list[tuple[str, str]]:
    import sqlite3
    con = sqlite3.connect(settings.DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT stock_code, stock_name FROM sector_stocks "
        "WHERE tracking_status='active' ORDER BY stock_code").fetchall()
    con.close()
    uni = [(r[0], r[1]) for r in rows]
    if codes:
        want = set(codes)
        picked = [(c, n) for c, n in uni if c in want]
        known = {c for c, _ in picked}
        picked += [(c, c) for c in codes if c not in known]
        return picked
    return uni


def _report(trades: list[PaperTrade], names: dict[str, str],
            gaps: dict[str, list[str]]) -> None:
    print(f"\n{'개시':<11}{'청산':<11}{'종목':<12}{'평단':>9}{'실현':>8}"
          f"{'최대투입':>7}  청산룰")
    print("-" * 78)
    for t in sorted(trades, key=lambda t: (t.opened_on, t.code)):
        print(f"{t.opened_on.isoformat():<11}{t.closed_on.isoformat():<11}"
              f"{names.get(t.code, t.code)[:10]:<12}{t.entry_avg:>9.0f}"
              f"{t.realized*100:>7.2f}%{t.max_invested:>7.2f}  "
              f"{'>'.join(t.exit_rules)}")
    print("-" * 78)
    if trades:
        n = len(trades)
        wins = [t for t in trades if t.realized > 0]
        avg = sum(t.realized for t in trades) / n
        eq, peak, mdd = 1.0, 1.0, 0.0
        for t in sorted(trades, key=lambda t: t.closed_on):
            eq *= 1 + t.realized
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        print(f"트레이드 {n}건 | 승률 {len(wins)/n*100:.1f}% "
              f"| 평균실현 {avg*100:+.2f}% | 누적(복리) {(eq-1)*100:+.1f}% "
              f"| MDD {mdd*100:.1f}%")
    else:
        print("완결 트레이드 0건")
    for kind, items in gaps.items():
        if items:
            print(f"[데이터 갭] {kind}: {len(items)}종목 — {', '.join(items[:8])}"
                  + (" ..." if len(items) > 8 else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-04-01")
    ap.add_argument("--end", default="2026-06-27")
    ap.add_argument("--codes", help="종목코드 CSV (미지정 시 active 전체)")
    ap.add_argument("--fill", choices=["next_open", "close"],
                    default="next_open", help="체결 가정 (기본 다음날 시가)")
    ap.add_argument("--r2", action="store_true",
                    help="R2 보수 필터(정배열 5>20>60) 켜기")
    ap.add_argument("--kis-backfill", type=int, default=0,
                    help="KIS 과거 일봉 N거래일 보충(60일선 워밍업). 0=끔")
    ap.add_argument("--synth-pad", type=int, default=0,
                    help="[더미] 부족 이력 합성 패딩 봉 수. 0=끔 (리포트 명시)")
    ap.add_argument("--log", action="store_true",
                    help="gm_v3_signals 테이블에 시그널 적재 (m010 필요)")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    cfg = replace(GmV3Config(), r2_trend_filter_enabled=args.r2).validated()
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else None)
    universe = _stock_universe(codes)
    if not universe:
        raise SystemExit("active sector_stocks 없음 — 웹앱/스크립트로 등록 후 실행")
    run_id = args.run_id or f"gm3_{args.start}_{args.end}_{uuid.uuid4().hex[:6]}"
    print(f"[gm_v3] {len(universe)}종목 | {start}~{end} | fill={args.fill} "
          f"| R2={'on' if args.r2 else 'off'} | run_id={run_id}")

    names = dict(universe)
    all_trades: list[PaperTrade] = []
    all_signals = []
    gaps: dict[str, list[str]] = {"토스캐시 없음(스킵)": [],
                                  "KIS 보충 실패": [], "합성 패딩 사용(더미)": []}
    for code, _name in universe:
        bars = load_daily_from_toss(code)
        if not bars:
            gaps["토스캐시 없음(스킵)"].append(code)
            continue
        if args.kis_backfill > 0:
            pre = asyncio.run(kis_backfill_daily(code, bars[0].day,
                                                 args.kis_backfill))
            if pre:
                bars = pre + bars
            else:
                gaps["KIS 보충 실패"].append(code)
        if args.synth_pad > 0 and len(bars) < args.synth_pad + 60:
            bars = synth_pad(bars, args.synth_pad, seed=hash(code) % 10 ** 6)
            gaps["합성 패딩 사용(더미)"].append(code)
        trades, sigs = simulate(code, bars, cfg, fill_mode=args.fill,
                                act_from=start, act_to=end)
        all_trades.extend(trades)
        all_signals.extend(sigs)

    rule_counts: dict[str, int] = {}
    for s in all_signals:
        rule_counts[s.rule] = rule_counts.get(s.rule, 0) + 1
    print("시그널 발동: " + (", ".join(
        f"{k} {v}" for k, v in sorted(rule_counts.items())) or "없음"))

    _report(all_trades, names, gaps)

    if args.log:
        n = log_signals(settings.DB_PATH, all_signals, run_id=run_id)
        print(f"[로그] gm_v3_signals 신규 {n}행 (run_id={run_id})")


if __name__ == "__main__":
    main()
