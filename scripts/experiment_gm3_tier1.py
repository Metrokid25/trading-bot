"""gm_v3 TIER 1 (R13~R16) A/B 백테스트 — OFF 벤치 / 각 룰 단독 ON / 전체 ON.

사양: docs/gm_v3_tier1_spec.md. 데이터 = 토스 캐시 일봉 합성 + KIS 페이징 백필.
목적: 신규 룰이 수익(툴1·2 계열)과 MDD(툴3·4 계열)를 실제로 개선하는지 분리 측정.

사용:
  ./.venv/Scripts/python.exe scripts/experiment_gm3_tier1.py 2026-01-02 2026-07-10 \
      [--codes 005930,042700] [--kis-backfill 220] [--fill next_open]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.run_gm_v3 import _stock_universe  # noqa: E402
from strategy.gm_v3 import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import (  # noqa: E402
    kis_backfill_daily, load_daily_from_toss,
)
from strategy.gm_v3.models import SignalType  # noqa: E402
from strategy.gm_v3.paper import PaperTrade, simulate  # noqa: E402

VARIANTS: list[tuple[str, dict]] = [
    ("BASE (현행 gm_v3)", {}),
    ("+R13 지지레벨매수", {"r13_enabled": True}),
    ("+R14 목표격자익절", {"r14_enabled": True}),
    ("+R15 반전캔들청산", {"r15_enabled": True}),
    ("+R16 구조손절", {"r16_enabled": True}),
    ("ALL (R13~R16)", {"r13_enabled": True, "r14_enabled": True,
                       "r15_enabled": True, "r16_enabled": True}),
]


def metrics(trades: list[PaperTrade]) -> dict:
    if not trades:
        return {"n": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t.realized > 0)
    avg = sum(t.realized for t in trades) / n
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda t: t.closed_on):
        eq *= 1 + t.realized
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {"n": n, "win": wins / n, "avg": avg, "cum": eq - 1, "mdd": mdd}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--codes", help="종목코드 CSV (미지정 시 active 전체)")
    ap.add_argument("--kis-backfill", type=int, default=220)
    ap.add_argument("--fill", choices=["next_open", "close"], default="next_open")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else None)
    universe = _stock_universe(codes)
    print(f"[tier1-ab] {len(universe)}종목 | {start}~{end} | fill={args.fill} "
          f"| kis-backfill={args.kis_backfill}")

    # 일봉은 변형 간 공유 — 한 번만 적재
    bars_by_code: dict[str, list] = {}
    skipped: list[str] = []
    for idx, (code, _name) in enumerate(universe, 1):
        bars = load_daily_from_toss(code)
        if not bars:
            skipped.append(code)
            continue
        if args.kis_backfill > 0:
            pre = asyncio.run(kis_backfill_daily(code, bars[0].day,
                                                 args.kis_backfill))
            if pre:
                bars = pre + bars
        bars_by_code[code] = bars
        if idx % 20 == 0:
            print(f"  적재 {idx}/{len(universe)}")
    print(f"적재 완료 {len(bars_by_code)}종목"
          + (f" | 토스캐시 없음 스킵 {len(skipped)}: {skipped[:6]}…" if skipped else ""))

    print(f"\n{'변형':<22}{'건수':>5}{'승률':>8}{'평균':>8}{'누적(직렬)':>11}{'MDD':>8}  신규룰 발동")
    print("-" * 88)
    for label, flags in VARIANTS:
        cfg = replace(GmV3Config(), **flags).validated()
        trades: list[PaperTrade] = []
        rule_counts: dict[str, int] = {}
        for code, bars in bars_by_code.items():
            t, sigs = simulate(code, bars, cfg, fill_mode=args.fill,
                               act_from=start, act_to=end)
            trades.extend(t)
            for s in sigs:
                # 정보성 MARK(R15a 윗꼬리 경고 등)는 제외 — 실제 매매 신호만 집계
                if (s.rule in ("R13", "R14", "R15", "R16")
                        and s.type in (SignalType.BUY, SignalType.SELL)):
                    rule_counts[s.rule] = rule_counts.get(s.rule, 0) + 1
        m = metrics(trades)
        fired = ", ".join(f"{k} {v}" for k, v in sorted(rule_counts.items())) or "-"
        if m["n"] == 0:
            print(f"{label:<22}{0:>5}  (트레이드 없음)")
            continue
        print(f"{label:<22}{m['n']:>5}{m['win']*100:>7.1f}%{m['avg']*100:>+7.2f}%"
              f"{m['cum']*100:>+10.1f}%{m['mdd']*100:>7.1f}%  {fired}")
    print("-" * 88)
    print("※ 누적/MDD = 청산일 순 직렬 복리 참고치 (동시 보유 미반영, 기존 리포트 관례)"
          " · 비용 미반영 (변형 간 상대 비교 목적)")


if __name__ == "__main__":
    main()
