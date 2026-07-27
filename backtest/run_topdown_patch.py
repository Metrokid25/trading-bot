"""현재 웹앱 등록 유니버스로 원본 전략과 top-down 패치 후보를 비교한다.

패치 후보는 기존 전략을 변경하지 않는다. 전일까지 확정된 등록 유니버스
시장 상태, 업종 상대강도, 종목 MA20 추세로 신규 진입만 허가한다.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.run_premarket_pullback import (  # noqa: E402
    _cache_conn, _select_top_n, backtest_symbol,
)
from strategy.gm_v3.config import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import load_daily_from_toss  # noqa: E402
from strategy.gm_v3.paper import PaperTrade, simulate  # noqa: E402
from strategy.paper_runner import load_universe  # noqa: E402
from strategy.topdown import TopDownGate  # noqa: E402

V2 = dict(
    pre_surge=0.05,
    pullback_min=0.03,
    support_tol=0.005,
    tp_levels=(0.05, 0.10, 0.15, 0.20, 0.25),
    stop_pct=0.04,
    consol_bars=3,
)
V2_PATCH = dict(
    **V2,
    vol_dryup_max=0.80,
    vol_confirm_ratio=1.50,
)
V4R = dict(**V2, max_entries=4, use_after=False, winner_gate=True)
V4R_PATCH = dict(
    **V2,
    max_entries=1,
    use_after=False,
    winner_gate=True,
)
ROUND_TRIP_COST = 0.005


def _metrics(returns: list[tuple[date, float]]) -> dict[str, float | int]:
    by_day: dict[date, list[float]] = {}
    for day, ret in returns:
        by_day.setdefault(day, []).append(ret)
    eq = peak = 1.0
    mdd = 0.0
    wins = 0
    for day in sorted(by_day):
        day_factor = 1.0
        for ret in by_day[day]:
            day_factor *= 1 + ret
            wins += ret > 0
        eq *= day_factor
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    n = len(returns)
    return {
        "trades": n,
        "win_rate": wins / n if n else 0.0,
        "avg": sum(r for _d, r in returns) / n if n else 0.0,
        "equity": eq - 1,
        "mdd": mdd,
    }


def _v2_returns(trades) -> list[tuple[date, float]]:
    return [(t.day, t.ret - ROUND_TRIP_COST) for t in trades]


def _gm_returns(trades: list[PaperTrade]) -> list[tuple[date, float]]:
    out = []
    for t in trades:
        sides = 1 if t.forced_eor else 2
        cost = 0.0025 * sides * min(t.max_invested, 1.0)
        out.append((t.closed_on, t.realized - cost))
    return out


def _print_table(rows: list[tuple[str, dict]]) -> None:
    print("\n전략                 거래   승률    평균     누적      MDD")
    print("-" * 65)
    for name, m in rows:
        print(
            f"{name:<20}{m['trades']:>5} "
            f"{m['win_rate']*100:>6.1f}% "
            f"{m['avg']*100:>+7.2f}% "
            f"{m['equity']*100:>+8.1f}% "
            f"{m['mdd']*100:>7.1f}%"
        )


def _in_period(
        returns: list[tuple[date, float]], start: date, end: date,
        ) -> list[tuple[date, float]]:
    return [(day, ret) for day, ret in returns if start <= day <= end]


def _print_periods(series: dict[str, list[tuple[date, float]]]) -> None:
    periods = (
        ("1~3월", date(2026, 1, 2), date(2026, 3, 31)),
        ("4~7월", date(2026, 4, 1), date(2026, 7, 24)),
    )
    for label, start, end in periods:
        print(f"\n[{label}]")
        _print_table([
            (name, _metrics(_in_period(returns, start, end)))
            for name, returns in series.items()
        ])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-01-02")
    ap.add_argument("--end", default="2026-07-24")
    ap.add_argument("--top-n", type=int, default=3,
                    help="v2 패치의 일별 최대 진입 수(프리장 강도순)")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    raw_universe = load_universe()
    sectors: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for code, name, sector in raw_universe:
        names.setdefault(code, name)
        sectors.setdefault(code, []).append(sector)
    codes = sorted(names)
    if not codes:
        raise SystemExit("현재 웹앱 활성 등록 종목이 없습니다.")

    bars_by_code = {code: load_daily_from_toss(code) for code in codes}
    missing = [code for code, bars in bars_by_code.items() if not bars]
    bars_by_code = {code: bars for code, bars in bars_by_code.items() if bars}
    days = sorted({
        bar.day for bars in bars_by_code.values() for bar in bars
        if start <= bar.day <= end
    })
    gate = TopDownGate.build(bars_by_code, sectors, days)
    print(
        f"[topdown patch] 현재 웹앱 등록 {len(codes)}종목 / "
        f"실데이터 {len(bars_by_code)}종목 / {start}~{end}"
    )
    if missing:
        print(f"[데이터 없음] {len(missing)}종목: {','.join(missing)}")

    cache = _cache_conn()
    v2_base = []
    v2_volume_candidates = []
    v4r_base = []
    v4r_patch = []
    for code in bars_by_code:
        name = names[code]
        v2_base += backtest_symbol(
            cache, code, name, start, end, mode="v2", **V2)
        v2_volume_candidates += backtest_symbol(
            cache, code, name, start, end, mode="v2", **V2_PATCH)
        v4r_base += backtest_symbol(
            cache, code, name, start, end, mode="v4r", **V4R)
        v4r_patch += backtest_symbol(
            cache, code, name, start, end, mode="v4r",
            entry_gate=gate.allows, **V4R_PATCH)
    cache.close()
    v2_top = _select_top_n(v2_base, args.top_n)
    v2_market = _select_top_n([
        t for t in v2_base
        if (gate.decision(t.symbol, t.day)
            and gate.decision(t.symbol, t.day).market_score >= 2)
    ], args.top_n)
    v2_gate = _select_top_n([
        t for t in v2_base if gate.allows(t.symbol, t.day)
    ], args.top_n)
    v2_volume = _select_top_n(v2_volume_candidates, args.top_n)
    v2_patch = _select_top_n([
        t for t in v2_volume_candidates if gate.allows(t.symbol, t.day)
    ], args.top_n)

    gm_cfg = GmV3Config().validated()
    gm_r2_cfg = replace(
        gm_cfg, r2_trend_filter_enabled=True).validated()
    r13_cfg = replace(gm_cfg, r13_enabled=True).validated()
    gm_base: list[PaperTrade] = []
    gm_r2: list[PaperTrade] = []
    gm_gate: list[PaperTrade] = []
    r13_base: list[PaperTrade] = []
    r13_gate: list[PaperTrade] = []
    for code, bars in bars_by_code.items():
        base, _ = simulate(
            code, bars, gm_cfg, fill_mode="next_open",
            act_from=start, act_to=end)
        r2_only, _ = simulate(
            code, bars, gm_r2_cfg, fill_mode="next_open",
            act_from=start, act_to=end)
        gate_only, _ = simulate(
            code, bars, gm_cfg, fill_mode="next_open",
            act_from=start, act_to=end, entry_gate=gate.allows_next_session)
        r13, _ = simulate(
            code, bars, r13_cfg, fill_mode="next_open",
            act_from=start, act_to=end)
        r13_gate_only, _ = simulate(
            code, bars, r13_cfg, fill_mode="next_open",
            act_from=start, act_to=end, entry_gate=gate.allows_next_session)
        gm_base += base
        gm_r2 += r2_only
        gm_gate += gate_only
        r13_base += r13
        r13_gate += r13_gate_only

    series = {
        "v2 원본": _v2_returns(v2_base),
        "v2 상위3": _v2_returns(v2_top),
        "v2 시장+상위3": _v2_returns(v2_market),
        "v2 전체게이트+상위3": _v2_returns(v2_gate),
        "v2 거래량+상위3": _v2_returns(v2_volume),
        "v2_td 실험": _v2_returns(v2_patch),
        "gm_v3 원본": _gm_returns(gm_base),
        "gm_v3 R2만": _gm_returns(gm_r2),
        "gm_v3 게이트만": _gm_returns(gm_gate),
        "gm_v3_td 최종": _gm_returns(gm_gate),
        "R13 원본": _gm_returns(r13_base),
        "R13 게이트만": _gm_returns(r13_gate),
        "R13_td 최종": _gm_returns(r13_gate),
        "v4r 원본": _v2_returns(v4r_base),
        "v4r_td 최종": _v2_returns(v4r_patch),
    }
    _print_table([(name, _metrics(returns))
                  for name, returns in series.items()])
    _print_periods(series)

    decisions = list(gate.decisions.values())
    risk_on = sum(d.regime == "risk_on" for d in decisions)
    neutral = sum(d.regime == "neutral" for d in decisions)
    risk_off = sum(d.regime == "risk_off" for d in decisions)
    allowed = sum(d.allowed for d in decisions)
    print(
        f"\n허가표: 허가 {allowed}/{len(decisions)} "
        f"| risk_on {risk_on} / neutral {neutral} / risk_off {risk_off}"
    )
    print("※ 현재 등록 종목을 과거 전체 기간에 고정 적용한 회고 테스트이며, "
          "실제 편입 시점 기반 forward 성과가 아닙니다.")


if __name__ == "__main__":
    main()
