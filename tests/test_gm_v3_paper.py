"""gm_v3 페이퍼 시뮬레이터·시그널 로깅 테스트 — 체결 가정과 멱등 적재 고정."""
from __future__ import annotations

import sqlite3

from strategy.gm_v3 import GmV3Config
from strategy.gm_v3.paper import simulate
from strategy.gm_v3.signal_log import log_signals
from strategy.gm_v3.synth import make_bars

CFG = GmV3Config().validated()

# R1 발화 시나리오 (test_gm_v3_rules.BASE 동일) + 체결/청산용 후속 봉
BASE = [
    (10000, 10200, 9900, 10100, 100),
    (10100, 10400, 10050, 10300, 100),
    (10300, 10700, 10250, 10600, 100),
    (10600, 11000, 10500, 10800, 100),
    (10800, 10850, 10400, 10500, 100),
    (10500, 10600, 10200, 10300, 100),
    (10300, 10400, 10000, 10100, 100),
    (10100, 10600, 10050, 10500, 100),
    (10500, 11150, 10450, 11100, 100),   # idx8: R1 신호 (종가 11100)
]


def test_fill_next_open_uses_next_bar_open():
    rows = BASE + [(11150, 11300, 11100, 11250, 100),   # idx9: 시가 11150 체결
                   (11250, 11350, 11200, 11300, 100)]
    trades, sigs = simulate("X", make_bars(rows), CFG)
    assert any(s.rule == "R1" for s in sigs)
    assert len(trades) == 1
    t = trades[0]
    assert t.entry_avg == 11150          # 신호 다음날 시가
    assert t.forced_eor and t.exit_rules == ["EOR"]


def test_fill_close_uses_signal_bar_close():
    rows = BASE + [(11150, 11300, 11100, 11250, 100)]
    trades, _ = simulate("X", make_bars(rows), CFG, fill_mode="close")
    assert len(trades) == 1
    assert trades[0].entry_avg == 11100  # 신호 당일 종가 (낙관 모드)


def test_r10_stop_fills_same_day_at_trigger():
    rows = BASE + [(11150, 11300, 11100, 11250, 100),   # idx9: 시가 11150 매수
                   (11000, 11050, 10600, 10700, 100)]   # idx10: 저가 10600 < 스탑
    trades, _ = simulate("X", make_bars(rows), CFG)
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_rules == ["R10"] and not t.forced_eor
    stop = 11150 * (1 - CFG.r10_stop_pct)
    expect = (stop / 11150 - 1) * CFG.r6_scout_weight
    assert abs(t.realized - expect) < 1e-9


def test_r10_gap_down_fills_at_open_not_stop():
    """갭하락으로 시가가 스탑 아래면 시가 체결 — 스탑 가격 낙관 체결 금지."""
    rows = BASE + [(11150, 11300, 11100, 11250, 100),   # 시가 11150 매수
                   (10200, 10250, 10000, 10100, 100)]   # 시가 10200 < 스탑 10704
    trades, _ = simulate("X", make_bars(rows), CFG)
    assert len(trades) == 1
    expect = (10200 / 11150 - 1) * CFG.r6_scout_weight  # 시가 기준 -1.70%
    assert abs(trades[0].realized - expect) < 1e-9


def test_apply_buy_harmonic_average_accounting():
    """자본 비중 매수의 평단은 조화평균 — 0.2@100 + 0.2@200 전량 200 매도 = +20%."""
    from datetime import date as Date
    from strategy.gm_v3.models import StockState
    st = StockState(code="X")
    st.apply_buy(100.0, 0.2, Date(2026, 1, 5))
    st.apply_buy(200.0, 0.2, Date(2026, 1, 6))
    pos = st.position
    realized = (200.0 / pos.entry_avg - 1) * pos.invested
    assert abs(realized - 0.2) < 1e-9                   # 산술평균이면 +13.3%로 왜곡


def test_act_window_excludes_warmup_signals():
    rows = BASE + [(11150, 11300, 11100, 11250, 100)]
    bars = make_bars(rows)
    cut = bars[-1].day                       # 마지막 봉만 액션 윈도우
    trades, sigs = simulate("X", bars, CFG, act_from=cut)
    assert not sigs and not trades           # R1 신호일(idx8)은 워밍업 구간


def test_signal_logging_idempotent(tmp_path):
    from scripts.migrations import m010_gm_v3_signals
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    m010_gm_v3_signals.up(con)
    con.commit()
    con.close()

    rows = BASE + [(11150, 11300, 11100, 11250, 100)]
    _trades, sigs = simulate("X", make_bars(rows), CFG)
    assert sigs
    n1 = log_signals(db, sigs, run_id="testrun")
    n2 = log_signals(db, sigs, run_id="testrun")     # 재실행 → 중복 없음
    assert n1 == len(sigs) and n2 == 0
    con = sqlite3.connect(db)
    total, = con.execute("SELECT COUNT(*) FROM gm_v3_signals").fetchone()
    rule, price = con.execute(
        "SELECT rule, price FROM gm_v3_signals WHERE signal_type='BUY'"
    ).fetchone()
    con.close()
    assert total == len(sigs) and rule == "R1" and price == 11100
