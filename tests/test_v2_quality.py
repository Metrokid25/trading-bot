from dataclasses import replace
from datetime import date

import pytest

from backtest.run_premarket_pullback import Trade, v2_volume_quality_score
from backtest.run_v2_quality_patch import (
    _portfolio_metrics, rank_quality, rank_volume_quality,
)
from strategy.paper_runner import select_v2_qv


def _trade(code: str, score: int, entry_time: str = "2026-07-06T09:30:00+09:00"):
    return Trade(
        symbol=code, name=code, day=date(2026, 7, 6),
        prev_close=100, pre_high=110, entry=105, exit=110,
        reason="1TP/BE", ret=0.01, entry_time=entry_time,
        exit_time="2026-07-06T10:30:00+09:00",
        quality_score=score,
    )


def test_quality_rank_prefers_score_not_premarket_surge():
    low = replace(_trade("LOW", 1), pre_high=130)
    high = replace(_trade("HIGH", 5), pre_high=108)
    picked = rank_quality([low, high], limit=1)
    assert [t.symbol for t in picked] == ["HIGH"]


def test_quality_rank_applies_threshold_and_daily_limit():
    trades = [_trade("A", 5), _trade("B", 4), _trade("C", 3), _trade("D", 1)]
    picked = rank_quality(trades, limit=2, min_score=3)
    assert [t.symbol for t in picked] == ["A", "B"]


def test_volume_quality_keeps_only_supported_volume_features():
    dry = replace(_trade("DRY", 0), quality_tags=("dryup",))
    both = replace(
        _trade("BOTH", 0), quality_tags=("dryup", "breakout_volume"))
    structural = replace(
        _trade("STRUCT", 9), quality_tags=("higher_low", "long_base"))
    assert v2_volume_quality_score(both.quality_tags) == 3
    assert v2_volume_quality_score(dry.quality_tags) == 2
    assert v2_volume_quality_score(structural.quality_tags) == 0
    assert [t.symbol for t in rank_volume_quality(
        [dry, both, structural], limit=2, min_score=2)] == ["BOTH", "DRY"]


def test_paper_qv_axis_requires_both_volume_features():
    rows = [
        {"code": "BOTH", "volume_quality_score": 3},
        {"code": "DRY", "volume_quality_score": 2},
        {"code": "BREAK", "volume_quality_score": 1},
        {"code": "OLD"},
    ]
    assert [row["code"] for row in select_v2_qv(rows)] == ["BOTH"]


def test_shared_cash_metrics_use_twenty_percent_slots():
    trades = [_trade("A", 3), _trade("B", 3)]
    metrics = _portfolio_metrics(trades, {"A": ["S1"], "B": ["S2"]})
    # 각 거래 순수익 0.5%(gross 1% - cost 0.5%) × 슬롯 20% × 2개
    assert metrics["equity"] == pytest.approx(0.002)
    assert metrics["selected"] == 2
