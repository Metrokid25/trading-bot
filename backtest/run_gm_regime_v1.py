"""굿머닝 검증판의 GM-R01/R02를 v2_qv 위에 얹은 연구 백테스트.

기존 top-down처럼 진입을 막지 않는다. 레거시 스냅샷의 섹터 pick_date로
근사한 등록 유니버스의 시장 상태(risk_on/neutral/risk_off)를 사용해 공유현금
포트폴리오 노출만 100%/50%/20%로 조절한다. 기존 전략·페이퍼 러너에는
연결하지 않는다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.run_premarket_pullback import (  # noqa: E402
    _cache_conn,
    backtest_symbol,
    v2_volume_quality_score,
)
from strategy.gm_regime_v1 import (  # noqa: E402
    RegimeSizingConfig,
    size_portfolio_day,
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
MIN_MARKET_COVERAGE = 0.80
TOPDOWN_MIN_HISTORY = 25


@dataclass(frozen=True, slots=True)
class UniverseMember:
    code: str
    name: str
    sector: str
    active_from: date
    active_until: date | None = None


@dataclass(frozen=True, slots=True)
class DayResult:
    day: date
    ret: float
    regime: str
    exposure: float
    trades: int


def _metrics(days: list[DayResult]) -> dict[str, float | int]:
    eq = peak = 1.0
    mdd = 0.0
    active = [item for item in days if item.trades]
    for item in sorted(days, key=lambda x: x.day):
        eq *= 1 + item.ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    returns = [item.ret for item in active]
    return {
        "active_days": len(active),
        "trades": sum(item.trades for item in active),
        "day_win_rate": (
            sum(value > 0 for value in returns) / len(returns) if returns else 0.0
        ),
        "avg_day": sum(returns) / len(returns) if returns else 0.0,
        "worst_day": min(returns, default=0.0),
        "equity": eq - 1,
        "mdd": mdd,
        "avg_exposure": (
            sum(item.exposure for item in active) / len(active) if active else 0.0
        ),
    }


def _period(
    days: list[DayResult], start: date, end: date
) -> list[DayResult]:
    return [item for item in days if start <= item.day <= end]


def _peak_exposure(rows: list[dict]) -> float:
    """선택된 포지션의 당일 동시 최대 투입 비중.

    같은 timestamp의 청산금은 신규 진입에 재사용하지 않는 원 포트폴리오의
    보수적 가정에 맞춰, 같은 시각이면 신규 진입을 먼저 반영한다.
    """
    events: list[tuple[datetime, int, float]] = []
    for row in rows:
        weight = float(row.get("weight", 0.0))
        if weight <= 0:
            continue
        events.append((datetime.fromisoformat(row["entry_time"]), 0, weight))
        events.append((datetime.fromisoformat(row["exit_time"]), 1, -weight))
    current = peak = 0.0
    for _ts, _kind, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


def _print_metrics(label: str, days: list[DayResult]) -> None:
    m = _metrics(days)
    print(
        f"{label:<18}{m['trades']:>5} {m['active_days']:>6} "
        f"{m['day_win_rate']*100:>6.1f}% {m['avg_day']*100:>+8.3f}% "
        f"{m['equity']*100:>+8.2f}% {m['mdd']*100:>7.2f}% "
        f"{m['worst_day']*100:>+8.3f}% {m['avg_exposure']*100:>7.1f}%"
    )


def _regime_for_day(
    gate: TopDownGate,
    codes: list[str],
    day: date,
    coverage: float,
) -> str:
    """모든 종목에 공통인 당일 시장 상태를 반환한다.

    충분한 워밍업을 갖춘 활성 종목 coverage가 80% 미만이면 장세를 추측하지
    않고 unknown으로 분리한다.
    """
    if coverage < MIN_MARKET_COVERAGE:
        return "unknown"
    for code in codes:
        decision = gate.decision(code, day)
        if decision is not None:
            return decision.regime
    return "unknown"


def _evaluate(
    rows_by_day: dict[date, list[dict]],
    gate: TopDownGate,
    codes: list[str],
    coverage_by_day: dict[date, float],
    cfg: RegimeSizingConfig,
) -> tuple[list[DayResult], list[DayResult], int]:
    base: list[DayResult] = []
    sized: list[DayResult] = []
    skipped = 0
    for day in sorted(rows_by_day):
        day_ret, selected, day_skipped = v2_portfolio_day(rows_by_day[day])
        regime = _regime_for_day(
            gate, codes, day, coverage_by_day.get(day, 0.0)
        )
        sized_ret, allocations = size_portfolio_day(
            day_ret, selected, regime, cfg
        )
        base.append(
            DayResult(
                day=day,
                ret=day_ret,
                regime=regime,
                exposure=_peak_exposure(selected),
                trades=len(selected),
            )
        )
        sized.append(
            DayResult(
                day=day,
                ret=sized_ret,
                regime=regime,
                exposure=_peak_exposure(allocations),
                trades=sum(float(row["weight"]) > 0 for row in allocations),
            )
        )
        skipped += day_skipped
    return base, sized, skipped


def _load_snapshot(path: Path) -> list[UniverseMember]:
    """스냅샷의 섹터 pick_date 근사 경계를 읽는다(쓰기 없음).

    레거시 스냅샷에는 종목별 이벤트 시각이 없으므로 같은 섹터의 나중 편입과
    장중 편입을 구분할 수 없다. 따라서 이 입력은 엄밀한 point-in-time 검증이
    아니라 pick-date approximation으로만 보고한다.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[UniverseMember] = []
    for sector in payload.get("sectors", []):
        sector_name = str(sector.get("sector_name", "")).strip()
        pick_date = date.fromisoformat(str(sector.get("pick_date", "")))
        for stock in sector.get("stocks", []):
            code = str(stock.get("code", "")).strip()
            name = str(stock.get("name", "")).strip()
            if sector_name and code and name:
                rows.append(
                    UniverseMember(
                        code,
                        name,
                        sector_name,
                        pick_date,
                        pick_date + timedelta(days=7),
                    )
                )
    return rows


def _is_active(member: UniverseMember, day: date) -> bool:
    return member.active_from <= day and (
        member.active_until is None or day < member.active_until
    )


def _active_sectors(
    members: list[UniverseMember], code: str, day: date
) -> list[str]:
    return sorted(
        {
            item.sector
            for item in members
            if item.code == code and _is_active(item, day)
        }
    )


def _build_point_in_time_gate(
    daily: dict[str, list],
    members: list[UniverseMember],
    days: list[date],
) -> TopDownGate:
    """일자별 pick-date 근사 등록 종목으로 market/sector/stock 결정을 만든다."""
    decisions = {}
    all_sessions = sorted(
        {bar.day for bars in daily.values() for bar in bars}
    )
    for day in days:
        prior_candidates = [session for session in all_sessions if session < day]
        prior_session = prior_candidates[-1] if prior_candidates else None
        active_codes = {
            item.code for item in members if _is_active(item, day)
        }
        # 직전 실제 세션 봉이 없는 종목의 낡은 종가가 breadth에 남지 않게 제외.
        if prior_session is not None:
            active_codes = {
                code
                for code in active_codes
                if any(bar.day == prior_session for bar in daily.get(code, ()))
            }
        day_daily = {
            code: daily[code] for code in active_codes if code in daily
        }
        day_sectors = {
            code: _active_sectors(members, code, day) for code in day_daily
        }
        if not day_daily:
            continue
        one_day = TopDownGate.build(day_daily, day_sectors, [day])
        decisions.update(one_day.decisions)
    return TopDownGate(decisions)


def _market_coverage_by_day(
    daily: dict[str, list],
    members: list[UniverseMember],
    days: list[date],
) -> dict[date, float]:
    """활성 종목 중 전일 freshness와 top-down 워밍업을 갖춘 비율."""
    all_sessions = sorted(
        {bar.day for bars in daily.values() for bar in bars}
    )
    result: dict[date, float] = {}
    for day in days:
        active_codes = {
            item.code for item in members if _is_active(item, day)
        }
        if not active_codes:
            result[day] = 0.0
            continue
        prior_candidates = [session for session in all_sessions if session < day]
        prior_session = prior_candidates[-1] if prior_candidates else None
        ready = 0
        for code in active_codes:
            hist = [bar for bar in daily.get(code, ()) if bar.day < day]
            if (
                prior_session is not None
                and len(hist) >= TOPDOWN_MIN_HISTORY
                and hist[-1].day == prior_session
            ):
                ready += 1
        result[day] = ready / len(active_codes)
    return result


def _benchmark_days(
    daily: dict[str, list],
    members: list[UniverseMember],
    days: list[date],
) -> list[DayResult]:
    """주어진 멤버십 근사 경계의 등록 유니버스 동일가중 일수익(무비용)."""
    by_code: dict[str, list[UniverseMember]] = defaultdict(list)
    for item in members:
        by_code[item.code].append(item)
    out: list[DayResult] = []
    for day in days:
        returns: list[float] = []
        for code, code_members in by_code.items():
            active_windows = [
                item for item in code_members if _is_active(item, day)
            ]
            if not active_windows:
                continue
            current_window_start = min(
                item.active_from for item in active_windows
            )
            bars = daily.get(code, ())
            current = next((bar for bar in bars if bar.day == day), None)
            if current is None or current.open <= 0:
                continue
            prior = [
                bar
                for bar in bars
                if current_window_start <= bar.day < day
                and bar.close > 0
                and any(_is_active(item, bar.day) for item in code_members)
            ]
            if prior:
                returns.append(current.close / prior[-1].close - 1)
            else:
                returns.append(current.close / current.open - 1)
        if returns:
            out.append(
                DayResult(
                    day=day,
                    ret=sum(returns) / len(returns),
                    regime="benchmark",
                    exposure=1.0,
                    trades=1,
                )
            )
    return out


def _peak_exposure_benchmark_proxy(
    model: list[DayResult], benchmark: list[DayResult]
) -> list[DayResult]:
    """일별 최대 동시노출을 종가 일수익에 곱한 거친 비교 proxy.

    장중 실제 보유시간을 반영하지 않으므로 동일 gross exposure 벤치가 아니다.
    """
    by_day = {item.day: item.ret for item in benchmark}
    return [
        DayResult(
            day=item.day,
            ret=by_day.get(item.day, 0.0) * item.exposure,
            regime="peak_exposure_proxy",
            exposure=item.exposure,
            trades=1 if item.exposure > 0 else 0,
        )
        for item in model
    ]


def _constant_exposure_baseline(
    base: list[DayResult], target: list[DayResult]
) -> tuple[list[DayResult], float]:
    """장세 정보 없이 목표 모델과 평균노출만 맞춘 상수비중 대조군."""
    base_avg = float(_metrics(base)["avg_exposure"])
    target_avg = float(_metrics(target)["avg_exposure"])
    factor = min(target_avg / base_avg, 1.0) if base_avg > 0 else 0.0
    return (
        [
            DayResult(
                day=item.day,
                ret=item.ret * factor,
                regime="constant",
                exposure=item.exposure * factor,
                trades=item.trades if factor > 0 else 0,
            )
            for item in base
        ],
        factor,
    )


def _print_proxy_gap(
    label: str, model: list[DayResult], benchmark: list[DayResult]
) -> None:
    model_nav = float(_metrics(model)["equity"])
    bench_nav = float(
        _metrics(_peak_exposure_benchmark_proxy(model, benchmark))["equity"]
    )
    print(
        f"{label}: 모델 {model_nav*100:+.2f}% / "
        f"피크노출×일봉 proxy {bench_nav*100:+.2f}% / "
        f"단순 격차 {((model_nav-bench_nav)*100):+.2f}%p"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-07-24")
    parser.add_argument("--neutral", type=float, default=0.50)
    parser.add_argument("--risk-off", type=float, default=0.20)
    parser.add_argument("--cost", type=float, default=ROUND_TRIP_COST)
    parser.add_argument(
        "--snapshot",
        type=Path,
        help=(
            "라이브 활성 종목 대신 지정한 universe_snapshot.json 사용 "
            "(고정 유니버스 회고 한계가 있으므로 명시 옵션만 허용)"
        ),
    )
    parser.add_argument(
        "--ignore-pick-date",
        action="store_true",
        help=(
            "스냅샷 종목을 시작일부터 고정하는 편향 탐색 모드. "
            "성과 검증에는 사용 금지"
        ),
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    cfg = RegimeSizingConfig(
        risk_on=1.0, neutral=args.neutral, risk_off=args.risk_off
    ).validated()

    if not 0 <= args.cost < 1:
        raise SystemExit("--cost는 0 이상 1 미만의 소수 비율이어야 합니다.")
    if args.snapshot:
        members = _load_snapshot(args.snapshot)
    else:
        if not args.ignore_pick_date:
            raise SystemExit(
                "현재 활성 유니버스에는 과거 편입일이 없습니다. "
                "pick_date 스냅샷을 지정하거나 편향 탐색임을 명시하는 "
                "--ignore-pick-date가 필요합니다."
            )
        members = [
            UniverseMember(code, name, sector, start, None)
            for code, name, sector in load_universe()
        ]
    if args.ignore_pick_date:
        members = [
            UniverseMember(item.code, item.name, item.sector, start, None)
            for item in members
        ]
    names: dict[str, str] = {}
    for item in members:
        names.setdefault(item.code, item.name)
    codes = sorted(names)
    if not codes:
        raise SystemExit("현재 웹앱 활성 등록 종목이 없습니다.")

    daily = {code: load_daily_from_toss(code) for code in codes}
    daily = {code: bars for code, bars in daily.items() if bars}
    days = sorted(
        {
            bar.day
            for bars in daily.values()
            for bar in bars
            if start <= bar.day <= end
        }
    )
    gate = _build_point_in_time_gate(daily, members, days)
    coverage_by_day = _market_coverage_by_day(daily, members, days)
    benchmark = _benchmark_days(daily, members, days)

    rows_by_day: dict[date, list[dict]] = defaultdict(list)
    cache = _cache_conn()
    try:
        for code in sorted(daily):
            trades = backtest_symbol(
                cache, code, names[code], start, end, mode="v2", **V2
            )
            for trade in trades:
                if v2_volume_quality_score(trade.quality_tags) != 3:
                    continue
                active_sectors = _active_sectors(members, code, trade.day)
                if not active_sectors:
                    continue
                rows_by_day[trade.day].append(
                    {
                        "code": code,
                        "name": names[code],
                        "sector": active_sectors[0],
                        "signal_strength": (
                            (trade.pre_high - trade.prev_close) / trade.prev_close
                            if trade.prev_close
                            else 0.0
                        ),
                        "ret_net": trade.ret - args.cost,
                        "entry_time": trade.entry_time,
                        "exit_time": trade.exit_time,
                    }
                )
    finally:
        cache.close()

    base, sized, skipped = _evaluate(
        rows_by_day, gate, sorted(daily), coverage_by_day, cfg
    )
    constant, constant_factor = _constant_exposure_baseline(base, sized)
    print(
        f"[gm_regime_v1] "
        f"{'편향 고정 스냅샷' if args.ignore_pick_date else 'pick-date 근사 스냅샷'} "
        f"{len(codes)}종목 / 실데이터 {len(daily)}종목 "
        f"/ {start}~{end} / 왕복비용 {args.cost*100:.1f}%"
    )
    print(
        f"노출 배수: unknown {cfg.unknown*100:.0f}% / risk_on 100% / "
        f"neutral {cfg.neutral*100:.0f}% / risk_off {cfg.risk_off*100:.0f}% "
        f"| 슬롯 경쟁 탈락 {skipped}"
    )
    print(
        "\n모델               거래 활성일 일승률   평균일손익     누적      MDD"
        "     최악일 평균노출"
    )
    print("-" * 100)
    _print_metrics("v2_qv 기준", base)
    _print_metrics(f"상수비중 {constant_factor:.2f}x", constant)
    _print_metrics("gm_regime_v1", sized)
    print("\n[벤치마크/피크노출 일봉 proxy — 동일노출 아님]")
    _print_metrics("유니버스 벤치", benchmark)
    _print_proxy_gap("v2_qv 기준", base, benchmark)
    _print_proxy_gap(f"상수비중 {constant_factor:.2f}x", constant, benchmark)
    _print_proxy_gap("gm_regime_v1", sized, benchmark)

    for label, period_start, period_end in (
        ("1~3월", date(2026, 1, 2), date(2026, 3, 31)),
        ("4월~종료", date(2026, 4, 1), end),
    ):
        print(f"\n[{label}]")
        _print_metrics("v2_qv 기준", _period(base, period_start, period_end))
        _print_metrics(
            f"상수비중 {constant_factor:.2f}x",
            _period(constant, period_start, period_end),
        )
        _print_metrics("gm_regime_v1", _period(sized, period_start, period_end))
        period_benchmark = _period(benchmark, period_start, period_end)
        _print_proxy_gap(
            "v2_qv 기준",
            _period(base, period_start, period_end),
            period_benchmark,
        )
        _print_proxy_gap(
            f"상수비중 {constant_factor:.2f}x",
            _period(constant, period_start, period_end),
            period_benchmark,
        )
        _print_proxy_gap(
            "gm_regime_v1",
            _period(sized, period_start, period_end),
            period_benchmark,
        )

    print("\n[장세별 gm_regime_v1]")
    for regime in ("unknown", "risk_on", "neutral", "risk_off"):
        regime_days = [item for item in sized if item.regime == regime]
        _print_metrics(regime, regime_days)
        _print_proxy_gap(regime, regime_days, benchmark)
    print(
        "\n※ 스냅샷 벤치는 pick_date 근사 동일가중·무비용이다. 피크노출 "
        "일봉 proxy는 장중 보유시간을 반영하지 않아 동일 gross exposure "
        "벤치도, 초과수익도 아니다. "
        "forward 검증 전에는 성과가 입증된 전략으로 간주하지 않는다."
    )


if __name__ == "__main__":
    main()
