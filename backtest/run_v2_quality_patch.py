"""현재 웹앱 등록 종목으로 v2 구조품질 랭킹을 비교한다.

진입 규칙과 청산 규칙은 원본 v2 그대로다. 진입 당시 확인 가능한 눌림 거래량,
돌파 거래량, 높은 저점, 다지기 길이, 과열, 하락 거래량과 전일 업종 상대강도로
점수를 만든 뒤 같은 날 신호의 우선순위에만 사용한다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.run_premarket_pullback import (  # noqa: E402
    Trade, _cache_conn, backtest_symbol, v2_volume_quality_score,
)
from strategy.gm_v3.data_source import load_daily_from_toss  # noqa: E402
from strategy.paper_runner import load_universe, v2_portfolio_day  # noqa: E402
from strategy.topdown import TopDownGate  # noqa: E402

V2 = dict(
    pre_surge=0.05,
    pullback_min=0.03,
    support_tol=0.005,
    tp_levels=(0.05, 0.10, 0.15, 0.20, 0.25),
    stop_pct=0.04,
    consol_bars=3,
)
ROUND_TRIP_COST = 0.005


def rank_quality(
        trades: list[Trade], limit: int = 3, min_score: int | None = None,
        ) -> list[Trade]:
    by_day: dict[date, list[Trade]] = defaultdict(list)
    for trade in trades:
        if min_score is None or trade.quality_score >= min_score:
            by_day[trade.day].append(trade)
    out: list[Trade] = []
    for day in sorted(by_day):
        ranked = sorted(
            by_day[day],
            key=lambda t: (
                -t.quality_score,
                t.entry_time,
                t.symbol,
            ),
        )
        out.extend(ranked[:limit] if limit > 0 else ranked)
    return out


def rank_surge(trades: list[Trade], limit: int = 3) -> list[Trade]:
    by_day: dict[date, list[Trade]] = defaultdict(list)
    for trade in trades:
        by_day[trade.day].append(trade)
    out: list[Trade] = []
    for day in sorted(by_day):
        ranked = sorted(
            by_day[day],
            key=lambda t: (
                -((t.pre_high - t.prev_close) / t.prev_close),
                t.symbol,
            ),
        )
        out.extend(ranked[:limit])
    return out


def rank_volume_quality(
        trades: list[Trade], limit: int = 5, min_score: int = 0,
        ) -> list[Trade]:
    by_day: dict[date, list[Trade]] = defaultdict(list)
    for trade in trades:
        if v2_volume_quality_score(trade.quality_tags) >= min_score:
            by_day[trade.day].append(trade)
    out: list[Trade] = []
    for day in sorted(by_day):
        ranked = sorted(
            by_day[day],
            key=lambda t: (
                -v2_volume_quality_score(t.quality_tags),
                t.entry_time,
                t.symbol,
            ),
        )
        out.extend(ranked[:limit] if limit > 0 else ranked)
    return out


def _returns(
        trades: list[Trade], cost: float = ROUND_TRIP_COST,
        ) -> list[tuple[date, float]]:
    return [(t.day, t.ret - cost) for t in trades]


def _metrics(
        trades: list[Trade], cost: float = ROUND_TRIP_COST,
        ) -> dict[str, float | int]:
    returns = _returns(trades, cost)
    by_day: dict[date, list[float]] = defaultdict(list)
    for day, ret in returns:
        by_day[day].append(ret)
    eq = peak = 1.0
    mdd = 0.0
    for day in sorted(by_day):
        for ret in by_day[day]:
            eq *= 1 + ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    n = len(returns)
    return {
        "trades": n,
        "win": sum(ret > 0 for _day, ret in returns) / n if n else 0.0,
        "avg": sum(ret for _day, ret in returns) / n if n else 0.0,
        "equity": eq - 1,
        "mdd": mdd,
    }


def _period(trades: list[Trade], start: date, end: date) -> list[Trade]:
    return [t for t in trades if start <= t.day <= end]


def _print_table(rows: list[tuple[str, list[Trade]]]) -> None:
    print("전략                 거래   승률    평균     누적      MDD")
    print("-" * 65)
    for name, trades in rows:
        m = _metrics(trades)
        print(
            f"{name:<20}{m['trades']:>5} "
            f"{m['win']*100:>6.1f}% "
            f"{m['avg']*100:>+7.2f}% "
            f"{m['equity']*100:>+8.1f}% "
            f"{m['mdd']*100:>7.1f}%"
        )


def _print_score_buckets(trades: list[Trade]) -> None:
    buckets: dict[int, list[Trade]] = defaultdict(list)
    for trade in trades:
        buckets[trade.quality_score].append(trade)
    print("\n점수별 실제 성과")
    print("점수   거래   승률    평균")
    print("-" * 32)
    for score in sorted(buckets, reverse=True):
        m = _metrics(buckets[score])
        print(
            f"{score:>4} {m['trades']:>6} "
            f"{m['win']*100:>6.1f}% {m['avg']*100:>+7.2f}%"
        )


def _print_tag_effects(trades: list[Trade]) -> None:
    tags = sorted({tag for trade in trades for tag in trade.quality_tags})
    print("\n항목별 유무 성과")
    print("항목                  있음n  있음평균  없음n  없음평균    차이")
    print("-" * 68)
    for tag in tags:
        present = [t for t in trades if tag in t.quality_tags]
        absent = [t for t in trades if tag not in t.quality_tags]
        p = _metrics(present)
        a = _metrics(absent)
        delta = float(p["avg"]) - float(a["avg"])
        print(
            f"{tag:<20}{p['trades']:>6} {p['avg']*100:>+8.2f}% "
            f"{a['trades']:>6} {a['avg']*100:>+8.2f}% "
            f"{delta*100:>+7.2f}%p"
        )


def _print_candidate_diagnostics(trades: list[Trade]) -> None:
    candidate = rank_volume_quality(trades, limit=5, min_score=3)
    print("\n[v2_qv 월별]")
    print("월       거래   승률    평균")
    print("-" * 34)
    months = sorted({(t.day.year, t.day.month) for t in candidate})
    for year, month in months:
        items = [
            t for t in candidate
            if (t.day.year, t.day.month) == (year, month)
        ]
        m = _metrics(items)
        print(
            f"{year}-{month:02d} {m['trades']:>6} "
            f"{m['win']*100:>6.1f}% {m['avg']*100:>+7.2f}%"
        )

    print("\n[v2_qv 비용 민감도]")
    print("왕복비용   평균     누적      MDD")
    print("-" * 38)
    for cost in (0.005, 0.008, 0.010):
        m = _metrics(candidate, cost)
        print(
            f"{cost*100:>7.1f}% "
            f"{m['avg']*100:>+7.2f}% "
            f"{m['equity']*100:>+8.1f}% "
            f"{m['mdd']*100:>7.1f}%"
        )


def _portfolio_metrics(
        trades: list[Trade], sectors: dict[str, list[str]],
        ) -> dict[str, float | int]:
    by_day: dict[date, list[dict]] = defaultdict(list)
    for trade in trades:
        sector = sorted(sectors.get(trade.symbol, [""]))[0]
        by_day[trade.day].append({
            "code": trade.symbol,
            "name": trade.name,
            "sector": sector,
            "signal_strength": (
                (trade.pre_high - trade.prev_close) / trade.prev_close
                if trade.prev_close else 0.0
            ),
            "ret_net": trade.ret - ROUND_TRIP_COST,
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
        })

    eq = peak = 1.0
    mdd = 0.0
    selected_n = skipped_n = 0
    active_days = 0
    for day in sorted(by_day):
        day_ret, selected, skipped = v2_portfolio_day(by_day[day])
        eq *= 1 + day_ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        selected_n += len(selected)
        skipped_n += skipped
        active_days += bool(selected)
    return {
        "equity": eq - 1,
        "mdd": mdd,
        "selected": selected_n,
        "skipped": skipped_n,
        "active_days": active_days,
    }


def _print_portfolio_nav(trades: list[Trade], sectors: dict[str, list[str]]) -> None:
    qv = [
        trade for trade in trades
        if v2_volume_quality_score(trade.quality_tags) == 3
    ]
    print("\n[공유현금 NAV: 종목당 20%·최대 5종목·업종당 2종목]")
    print("전략             선택  스킵  거래일    누적      MDD")
    print("-" * 58)
    for name, items in (("v2 원본", trades), ("v2_qv", qv)):
        m = _portfolio_metrics(items, sectors)
        print(
            f"{name:<15}{m['selected']:>5}{m['skipped']:>6}"
            f"{m['active_days']:>7} "
            f"{m['equity']*100:>+8.2f}% "
            f"{m['mdd']*100:>7.2f}%"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-01-02")
    ap.add_argument("--end", default="2026-07-24")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    raw = load_universe()
    names: dict[str, str] = {}
    sectors: dict[str, list[str]] = defaultdict(list)
    for code, name, sector in raw:
        names.setdefault(code, name)
        sectors[code].append(sector)
    codes = sorted(names)

    daily = {code: load_daily_from_toss(code) for code in codes}
    days = sorted({
        bar.day for bars in daily.values() for bar in bars
        if start <= bar.day <= end
    })
    sector_gate = TopDownGate.build(daily, sectors, days)

    cache = _cache_conn()
    trades: list[Trade] = []
    for code in codes:
        items = backtest_symbol(
            cache, code, names[code], start, end, mode="v2", **V2)
        for trade in items:
            decision = sector_gate.decision(code, trade.day)
            if decision and decision.sector_pass:
                trade = replace(
                    trade,
                    quality_score=trade.quality_score + 1,
                    quality_tags=trade.quality_tags + ("sector_relative",),
                )
            trades.append(trade)
    cache.close()

    variants = [
        ("v2 원본", trades),
        (f"프리상승 상위{args.limit}", rank_surge(trades, args.limit)),
        (f"품질점수 상위{args.limit}", rank_quality(trades, args.limit)),
        ("품질점수 >=2", rank_quality(trades, args.limit, 2)),
        ("품질점수 >=3", rank_quality(trades, args.limit, 3)),
        ("품질점수 >=4", rank_quality(trades, args.limit, 4)),
        ("거래량점수 >=2", rank_volume_quality(trades, args.limit, 2)),
        ("거래량점수 =3", rank_volume_quality(trades, args.limit, 3)),
    ]
    print(
        f"[v2 quality] 현재 웹앱 {len(codes)}종목 | "
        f"{start}~{end} | 왕복비용 0.5%"
    )
    _print_table(variants)
    _print_score_buckets(trades)
    _print_tag_effects(trades)
    _print_candidate_diagnostics(trades)
    _print_portfolio_nav(trades, sectors)

    for label, p_start, p_end in (
        ("1~3월", date(2026, 1, 2), date(2026, 3, 31)),
        ("4~7월", date(2026, 4, 1), date(2026, 7, 24)),
        ("최근 forward", date(2026, 7, 6), date(2026, 7, 24)),
    ):
        print(f"\n[{label}]")
        _print_table([
            (name, _period(items, p_start, p_end))
            for name, items in variants
        ])


if __name__ == "__main__":
    main()
