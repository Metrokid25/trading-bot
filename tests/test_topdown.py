from datetime import date, timedelta

from strategy.gm_v3.models import DailyBar
from strategy.topdown import TopDownGate


def _series(
        code: str, rising: bool = True, step: float = 1.0,
        ) -> tuple[str, list[DailyBar]]:
    start = date(2026, 1, 1)
    bars = []
    for i in range(35):
        px = 100 + step * i if rising else 150 - step * i
        bars.append(DailyBar(
            day=start + timedelta(days=i),
            open=px, high=px + 1, low=px - 1, close=px, volume=1000))
    return code, bars


def test_topdown_uses_only_bars_before_decision_day():
    bars = dict([_series("A"), _series("B"), _series("C")])
    sectors = {"A": ["강세"], "B": ["강세"], "C": ["강세"]}
    day = date(2026, 2, 4)
    before = TopDownGate.build(bars, sectors, [day]).decision("A", day)

    # 당일 봉을 폭락으로 바꿔도 당일 장 시작 전에 계산된 결정은 같아야 한다.
    changed = {code: list(items) for code, items in bars.items()}
    changed["A"][-1] = DailyBar(day, 1, 1, 1, 1, 999999)
    after = TopDownGate.build(changed, sectors, [day]).decision("A", day)
    assert before == after


def test_next_session_gate_uses_signal_day_close():
    bars = dict([_series("A"), _series("B"), _series("C", step=0.1)])
    sectors = {"A": ["주도"], "B": ["주도"], "C": ["비주도"]}
    signal_day = date(2026, 2, 3)
    next_day = date(2026, 2, 4)
    gate = TopDownGate.build(bars, sectors, [signal_day, next_day])
    assert gate.allows_next_session("A", signal_day) == gate.allows("A", next_day)


def test_topdown_blocks_falling_market_and_allows_strong_sector_stock():
    rising = dict([_series("A"), _series("B"), _series("C", step=0.1)])
    sectors = {"A": ["주도"], "B": ["주도"], "C": ["비주도"]}
    day = date(2026, 2, 4)
    gate = TopDownGate.build(rising, sectors, [day])
    assert gate.allows("A", day)

    falling = dict([_series("A", False), _series("B", False),
                    _series("C", False)])
    gate = TopDownGate.build(falling, sectors, [day])
    assert not gate.allows("A", day)
