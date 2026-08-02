from datetime import date, timedelta

import pytest

from backtest.run_gm_regime_v1 import (
    DayResult,
    UniverseMember,
    _build_point_in_time_gate,
    _constant_exposure_baseline,
    _load_snapshot,
    _market_coverage_by_day,
    _peak_exposure_benchmark_proxy,
    _peak_exposure,
    _regime_for_day,
)
from strategy.gm_v3.models import DailyBar
from strategy.gm_regime_v1 import RegimeSizingConfig, size_portfolio_day


def _selected():
    return [
        {
            "code": "A",
            "weight": 0.2,
            "pnl": 0.002,
            "ret_net": 0.01,
        },
        {
            "code": "B",
            "weight": 0.2,
            "pnl": -0.001,
            "ret_net": -0.005,
        },
    ]


@pytest.mark.parametrize(
    ("regime", "factor"),
    (
        ("risk_on", 1.0),
        ("neutral", 0.5),
        ("risk_off", 0.2),
        ("unknown", 1.0),
    ),
)
def test_regime_sizing_scales_return_weight_and_pnl(regime, factor):
    source = _selected()
    day_ret, rows = size_portfolio_day(0.001, source, regime)

    assert day_ret == pytest.approx(0.001 * factor)
    assert [row["weight"] for row in rows] == pytest.approx(
        [0.2 * factor, 0.2 * factor]
    )
    assert [row["pnl"] for row in rows] == pytest.approx(
        [0.002 * factor, -0.001 * factor]
    )
    assert all(row["exposure_factor"] == factor for row in rows)
    assert source == _selected()


def test_risk_off_reduces_but_does_not_block_observation():
    day_ret, rows = size_portfolio_day(-0.02, _selected(), "risk_off")
    assert day_ret == pytest.approx(-0.004)
    assert len(rows) == 2
    assert sum(row["weight"] for row in rows) == pytest.approx(0.08)


@pytest.mark.parametrize(
    "cfg",
    (
        RegimeSizingConfig(risk_on=1.1),
        RegimeSizingConfig(risk_on=1.0, neutral=0.1, risk_off=0.2),
    ),
)
def test_invalid_regime_sizing_config_fails_closed(cfg):
    with pytest.raises(ValueError):
        cfg.validated()


def test_unknown_regime_is_rejected():
    with pytest.raises(ValueError, match="unknown market regime"):
        size_portfolio_day(0.01, _selected(), "panic")


def test_snapshot_loader_flattens_sector_membership(tmp_path):
    path = tmp_path / "universe.json"
    path.write_text(
        """
        {"sectors": [
          {"sector_name": "AI", "pick_date": "2026-07-01", "stocks": [
            {"code": "000001", "name": "첫째"},
            {"code": "", "name": "제외"}
          ]},
          {"sector_name": "반도체", "pick_date": "2026-07-02", "stocks": [
            {"code": "000002", "name": "둘째"}
          ]}
        ]}
        """,
        encoding="utf-8",
    )
    assert _load_snapshot(path) == [
        UniverseMember(
            "000001", "첫째", "AI", date(2026, 7, 1), date(2026, 7, 8)
        ),
        UniverseMember(
            "000002", "둘째", "반도체", date(2026, 7, 2), date(2026, 7, 9)
        ),
    ]


def test_peak_exposure_counts_same_timestamp_as_overlap():
    rows = [
        {
            "entry_time": "2026-07-06T09:30:00+09:00",
            "exit_time": "2026-07-06T10:00:00+09:00",
            "weight": 0.2,
        },
        {
            "entry_time": "2026-07-06T10:00:00+09:00",
            "exit_time": "2026-07-06T11:00:00+09:00",
            "weight": 0.2,
        },
        {
            "entry_time": "2026-07-06T10:03:00+09:00",
            "exit_time": "2026-07-06T11:30:00+09:00",
            "weight": 0.2,
        },
    ]
    assert _peak_exposure(rows) == pytest.approx(0.4)


def _daily_series(code: str) -> tuple[str, list[DailyBar]]:
    start = date(2026, 1, 1)
    return code, [
        DailyBar(
            day=start + timedelta(days=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1000,
        )
        for i in range(35)
    ]


def test_point_in_time_gate_excludes_future_member():
    daily = dict([_daily_series("A"), _daily_series("B"), _daily_series("FUTURE")])
    decision_day = date(2026, 2, 3)
    members = [
        UniverseMember("A", "A", "S", date(2026, 1, 1)),
        UniverseMember("B", "B", "S", date(2026, 1, 1)),
        UniverseMember("FUTURE", "F", "S", date(2026, 2, 4)),
    ]
    gate = _build_point_in_time_gate(daily, members, [decision_day])
    assert gate.decision("A", decision_day) is not None
    assert gate.decision("FUTURE", decision_day) is None


def test_point_in_time_gate_excludes_stale_member_on_first_requested_day():
    daily = dict([_daily_series("A"), _daily_series("B"), _daily_series("STALE")])
    decision_day = date(2026, 2, 4)
    daily["STALE"] = [
        bar for bar in daily["STALE"] if bar.day < date(2026, 2, 3)
    ]
    members = [
        UniverseMember(code, code, "S", date(2026, 1, 1))
        for code in ("A", "B", "STALE")
    ]
    gate = _build_point_in_time_gate(daily, members, [decision_day])
    assert gate.decision("A", decision_day) is not None
    assert gate.decision("STALE", decision_day) is None


def test_peak_exposure_proxy_uses_model_daily_peak_exposure():
    model = [
        DayResult(date(2026, 7, 1), 0.01, "risk_on", 0.4, 2),
        DayResult(date(2026, 7, 2), -0.01, "neutral", 0.2, 1),
    ]
    benchmark = [
        DayResult(date(2026, 7, 1), 0.05, "benchmark", 1.0, 1),
        DayResult(date(2026, 7, 2), -0.10, "benchmark", 1.0, 1),
    ]
    matched = _peak_exposure_benchmark_proxy(model, benchmark)
    assert [item.ret for item in matched] == pytest.approx([0.02, -0.02])
    assert [item.exposure for item in matched] == pytest.approx([0.4, 0.2])


def test_reentry_benchmark_does_not_bridge_inactive_gap():
    from backtest.run_gm_regime_v1 import _benchmark_days

    members = [
        UniverseMember("A", "A", "S", date(2026, 1, 1), date(2026, 1, 2)),
        UniverseMember("A", "A", "S", date(2026, 1, 10), None),
    ]
    daily = {
        "A": [
            DailyBar(date(2026, 1, 2), 100, 100, 100, 100, 1),
            DailyBar(date(2026, 1, 10), 200, 200, 200, 200, 1),
        ]
    }
    result = _benchmark_days(daily, members, [date(2026, 1, 10)])
    assert result[0].ret == pytest.approx(0.0)


def test_warmup_shortfall_is_unknown_not_risk_off():
    daily = dict([_daily_series("A"), _daily_series("B")])
    members = [
        UniverseMember(code, code, "S", date(2026, 1, 1))
        for code in ("A", "B")
    ]
    early = date(2026, 1, 10)
    gate = _build_point_in_time_gate(daily, members, [early])
    coverage = _market_coverage_by_day(daily, members, [early])[early]
    assert coverage < 0.8
    assert _regime_for_day(gate, ["A", "B"], early, coverage) == "unknown"


def test_constant_baseline_matches_target_average_exposure():
    base = [
        DayResult(date(2026, 7, 1), 0.02, "risk_on", 0.4, 2),
        DayResult(date(2026, 7, 2), -0.01, "risk_off", 0.2, 1),
    ]
    target = [
        DayResult(date(2026, 7, 1), 0.01, "risk_on", 0.2, 2),
        DayResult(date(2026, 7, 2), -0.005, "risk_off", 0.1, 1),
    ]
    constant, factor = _constant_exposure_baseline(base, target)
    assert factor == pytest.approx(0.5)
    assert [item.ret for item in constant] == pytest.approx([0.01, -0.005])
    assert [item.exposure for item in constant] == pytest.approx([0.2, 0.1])
