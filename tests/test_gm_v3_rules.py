"""gm_v3 룰 엔진 단위 테스트 — 룰별 발생/미발생 케이스 (합성 일봉).

시나리오 공통: 피벗 k=3, lookback 20, 임계값은 GmV3Config 기본값.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date as Date

from strategy.gm_v3 import (
    DailyBar, GmV3Config, Signal, SignalType, StockState,
    evaluate_day, liquidation_order,
)
from strategy.gm_v3.models import Position
from strategy.gm_v3.synth import make_bars, make_random_walk

CFG = GmV3Config().validated()


def feed(bars: list[DailyBar], cfg: GmV3Config = CFG,
         position: Position | None = None) -> tuple[list[Signal], StockState]:
    st = StockState(code="000001")
    st.position = position
    sigs: list[Signal] = []
    for b in bars:
        sigs.extend(evaluate_day(st, b, cfg))
    return sigs, st


def only(sigs: list[Signal], rule: str) -> list[Signal]:
    return [s for s in sigs if s.rule == rule]


def pos(entry: float, invested: float = 1.0, *, peak: float = 0.0,
        r8_done: bool = True) -> Position:
    return Position(entry_avg=entry, invested=invested,
                    opened_on=Date(2026, 1, 2), peak=peak, r8_done=r8_done)


# 공통 시나리오: 상승 → 피벗(고가 11000, idx3) → 하락 3봉(확정) → 반등 → 돌파
BASE = [
    (10000, 10200, 9900, 10100, 100),
    (10100, 10400, 10050, 10300, 100),
    (10300, 10700, 10250, 10600, 100),
    (10600, 11000, 10500, 10800, 100),   # idx3: 피벗 고점 11000
    (10800, 10850, 10400, 10500, 100),
    (10500, 10600, 10200, 10300, 100),
    (10300, 10400, 10000, 10100, 100),   # idx6: 피벗 확정(k=3)
    (10100, 10600, 10050, 10500, 100),   # 반등 시작
    (10500, 11150, 10450, 11100, 100),   # idx8: 종가 11100 > 11000 첫 돌파
]


# -------------------- R1 무릎 매수 --------------------
def test_r1_fires_on_close_break_of_confirmed_pivot():
    sigs, _ = feed(make_bars(BASE))
    buys = only(sigs, "R1")
    assert len(buys) == 1
    assert buys[0].type == SignalType.BUY
    assert buys[0].reason["pivot_high"] == 11000
    assert buys[0].weight == CFG.r6_scout_weight     # R6: 선발대 20%


def test_r1_no_fire_when_close_below_pivot():
    rows = BASE[:8] + [(10500, 10950, 10450, 10900, 100)]   # 종가 10900 < 11000
    sigs, _ = feed(make_bars(rows))
    assert not [s for s in sigs if s.type == SignalType.BUY]


def test_r1_pivot_not_reused_after_fire():
    rows = BASE + [
        (11100, 11150, 10700, 10800, 100),   # 피벗 아래로 되밀림
        (10800, 11400, 10750, 11300, 100),   # 재돌파 — 소비된 피벗, 재발화 금지
    ]
    sigs, _ = feed(make_bars(rows))
    assert len([s for s in sigs if s.type == SignalType.BUY]) == 1


# -------------------- R2 보수적 확인 필터 --------------------
def test_r2_enabled_blocks_without_ma_alignment_data():
    cfg = replace(CFG, r2_trend_filter_enabled=True)   # 60일선 데이터 부족 → 차단
    sigs, _ = feed(make_bars(BASE), cfg)
    assert not [s for s in sigs if s.type == SignalType.BUY]


def test_r2_disabled_allows():
    sigs, _ = feed(make_bars(BASE), replace(CFG, r2_trend_filter_enabled=False))
    assert only(sigs, "R1")


# -------------------- R3 추격매수 금지 --------------------
SURGE8 = BASE[:8] + [(10500, 11700, 10450, 11600, 100)]   # idx8: +10.5% 급등


def test_r3_blocks_chase_and_registers_watch():
    sigs, st = feed(make_bars(SURGE8))
    # 종가 11600 > 피벗 11000 이지만 급등일 → R1 차단, WATCH 등록
    assert not [s for s in sigs if s.type == SignalType.BUY]
    assert only(sigs, "R3") and only(sigs, "R3")[0].type == SignalType.WATCH
    assert st.watch is not None and st.watch.watermark == 11700


# -------------------- R4 눌림목 재진입 --------------------
def _r4_rows(pullback_vols=(100, 100, 100, 100), rebreak_vol=250,
             decline_vol=300):
    rows = [(o, h, l, c, decline_vol) for o, h, l, c, _v in BASE[:8]]
    rows += [(10500, 11700, 10450, 11600, decline_vol)]       # 급등 → watch
    pb = [(11500, 11550, 11200, 11250), (11250, 11300, 11100, 11150),
          (11150, 11250, 11050, 11100), (11100, 11200, 11000, 11050)]
    rows += [(o, h, l, c, v) for (o, h, l, c), v in zip(pb, pullback_vols)]
    rows += [(11050, 11400, 11040, 11350, rebreak_vol)]       # 전일 고가 재돌파
    return rows


def test_r4_fires_on_zone_pullback_volume_rebreak():
    sigs, _ = feed(make_bars(_r4_rows()))
    buys = only(sigs, "R4")
    assert len(buys) == 1 and buys[0].type == SignalType.BUY
    assert buys[0].reason["vol_ratio"] >= CFG.r4_vol_mult


def test_r4_no_fire_without_volume():
    sigs, _ = feed(make_bars(_r4_rows(rebreak_vol=150)))      # 2배 미달
    assert not [s for s in sigs if s.type == SignalType.BUY]


def test_r4_watch_dropped_on_too_deep_pullback():
    rows = [(o, h, l, c, 300) for o, h, l, c, _v in SURGE8]
    rows += [(11400, 11450, 10650, 10700, 100),    # -8.5% — 존 이탈, watch 해제
             (10700, 10800, 10650, 10750, 100),
             (10750, 10950, 10740, 10900, 250)]    # 전일 고가 돌파해도 무효
    sigs, st = feed(make_bars(rows))
    assert st.watch is None
    assert not [s for s in sigs if s.type == SignalType.BUY]


# -------------------- R5 하락 중 거래량 필터 --------------------
def test_r5_rising_volume_downtrend_blocks_entry():
    rows = _r4_rows(pullback_vols=(200, 240, 290, 350), rebreak_vol=800,
                    decline_vol=100)               # 하락 중 거래량 증가 추세
    sigs, _ = feed(make_bars(rows))
    assert not [s for s in sigs if s.type == SignalType.BUY]
    sigs2, _ = feed(make_bars(rows), replace(CFG, r5_enabled=False))
    assert only(sigs2, "R4")                       # 필터 끄면 동일 봉에서 발화


def test_r5_drying_volume_marks_dip_candidate():
    rows = [(10000 - 50 * i + 20, 10000 - 50 * i + 60,
             10000 - 50 * i - 60, 10000 - 50 * i,
             200 if i < 6 else 100) for i in range(12)]
    sigs, _ = feed(make_bars(rows))
    marks = only(sigs, "R5")
    assert marks and all(s.type == SignalType.MARK for s in marks)
    assert not [s for s in sigs if s.type == SignalType.BUY]


# -------------------- R6 분할매수 비중 --------------------
def test_r6_scout_then_add_weights():
    cfg = replace(CFG, r7_enabled=False, r8_enabled=False, r9_enabled=False)
    st = StockState(code="000001")
    sigs: list[Signal] = []
    for b in make_bars(BASE):
        sigs.extend(evaluate_day(st, b, cfg))
    assert only(sigs, "R1")[0].weight == 0.2
    st.apply_buy(11100, 0.2, Date(2026, 1, 2))     # 선발대 체결
    more = [(11100, 12300, 11050, 12250, 100),     # +10.4% 급등 → watch
            (12200, 12250, 11850, 11900, 100),     # -3.3% 존 도달
            (11900, 11950, 11800, 11850, 100),
            (11850, 12200, 11840, 12150, 250)]     # 재돌파 → R4 추가매수
    sigs2: list[Signal] = []
    for b in make_bars(more, start=Date(2026, 2, 2)):
        sigs2.extend(evaluate_day(st, b, cfg))
    adds = only(sigs2, "R4")
    assert len(adds) == 1 and adds[0].weight == cfg.r6_add_weight


def test_r6_no_buy_on_price_drop_alone():
    bars = make_bars([(11000, 11050, 10900, 10950, 100),
                      (10950, 10980, 10800, 10850, 100),
                      (10850, 10900, 10700, 10750, 100)])
    sigs, _ = feed(bars, position=pos(11000, 0.2, peak=11050))
    assert not [s for s in sigs if s.type == SignalType.BUY]   # 물타기 금지


# -------------------- R7 어깨 매도 --------------------
def test_r7_fires_on_trailing_drawdown_bearish():
    bars = make_bars([(11000, 11500, 10900, 11400, 100),
                      (11400, 12100, 11300, 11350, 100)])   # 고점 대비 -6.2% 음봉
    sigs, _ = feed(bars, position=pos(10000))
    assert only(sigs, "R7")


def test_r7_suppressed_while_bullish_advance():
    bars = make_bars([(11000, 11500, 10900, 11400, 100),
                      (11390, 12100, 11300, 11450, 100)])   # -5.4% 지만 상승 양봉
    sigs, _ = feed(bars, position=pos(10000))
    assert not only(sigs, "R7")


# -------------------- R8 목표가 분할매도 --------------------
def test_r8_partial_take_profit_once():
    bars = make_bars([(10800, 11050, 10750, 11000, 100),    # +10% 도달
                      (11100, 11600, 11050, 11500, 100)])   # 재도달해도 1회만
    sigs, _ = feed(bars, position=pos(10000, r8_done=False))
    r8 = only(sigs, "R8")
    assert len(r8) == 1 and r8[0].weight == CFG.r8_sell_frac


# -------------------- R9 위험 신호 즉시 청산 --------------------
def test_r9a_fires_on_big_bear_after_surge():
    bars = make_bars([(9950, 10050, 9900, 10000, 100),
                      (10150, 11250, 10100, 11200, 100),    # +12% 폭등 양봉
                      (11150, 11200, 9880, 9900, 100)])     # 전일 몸통보다 큰 음봉
    sigs, _ = feed(bars, position=pos(9000))
    assert any(s.reason.get("variant") == "a" for s in only(sigs, "R9"))


def test_r9a_no_fire_on_smaller_bear_body():
    bars = make_bars([(9950, 10050, 9900, 10000, 100),
                      (10150, 11250, 10100, 11200, 100),
                      (11150, 11200, 10100, 10250, 100)])   # 몸통 900 < 1050
    sigs, _ = feed(bars, position=pos(9000))
    assert not only(sigs, "R9")


def test_r9b_fires_on_surge_wick_volume():
    pre = [(10000, 10060, 9950, 10000, 100)] * 5
    bars = make_bars(pre + [(10100, 10700, 9800, 10150, 250)])
    sigs, _ = feed(bars, position=pos(9000))
    assert any(s.reason.get("variant") == "b" for s in only(sigs, "R9"))


def test_r9b_no_fire_without_volume_spike():
    pre = [(10000, 10060, 9950, 10000, 100)] * 5
    bars = make_bars(pre + [(10100, 10700, 9800, 10150, 150)])
    sigs, _ = feed(bars, position=pos(9000))
    assert not only(sigs, "R9")


# -------------------- R10 손절 --------------------
def test_r10_fires_on_stop_touch_and_short_circuits():
    bars = make_bars([(9900, 9950, 9540, 9800, 100)])       # 저가 9540 ≤ 9600
    sigs, _ = feed(bars, position=pos(10000))
    assert [s.rule for s in sigs] == ["R10"]
    assert sigs[0].price == 10000 * (1 - CFG.r10_stop_pct)


def test_r10_no_fire_above_stop():
    bars = make_bars([(9900, 9950, 9640, 9800, 100)])
    sigs, _ = feed(bars, position=pos(10000))
    assert not only(sigs, "R10")


# -------------------- R11 홀딩 예외 --------------------
def _r11_rows():
    rows = [(9000 + 200 * i, 9050 + 200 * i, 8950 + 200 * i,
             9000 + 200 * i, 200) for i in range(5)]        # 상승, 거래량 200
    rows += [(9850, 10000, 9800, 9900, 100),                # 고점 10000
             (9900, 9980, 9850, 9950, 100),
             (9950, 9990, 9880, 9920, 100),
             (9920, 9960, 9850, 9900, 100),
             (9900, 9950, 9800, 9850, 100),                 # 마름 + -1.5% 버팀 → HOLD
             (9500, 9550, 9400, 9450, 100)]                 # -5.5% 음봉 (유예 구간)
    return rows


def test_r11_hold_flag_suspends_r7():
    sigs, st = feed(make_bars(_r11_rows()), position=pos(8000))
    assert only(sigs, "R11")
    assert not only(sigs, "R7")                             # 트레일링 유예
    assert st.hold_until >= 0


def test_r11_disabled_lets_r7_fire():
    cfg = replace(CFG, r11_enabled=False)
    sigs, _ = feed(make_bars(_r11_rows()), cfg, position=pos(8000))
    assert only(sigs, "R7")


# -------------------- R12 손실 종목 우선 정리 --------------------
def test_r12_liquidation_order_sorts_by_loss():
    order = liquidation_order([("A", 100.0, 110.0), ("B", 100.0, 90.0),
                               ("C", 100.0, 95.0)])
    assert [c for c, _r in order] == ["B", "C", "A"]


# -------------------- 합성 데이터 유틸 --------------------
def test_synth_random_walk_is_reproducible_and_valid():
    a = make_random_walk(seed=7, n=30)
    b = make_random_walk(seed=7, n=30)
    assert a == b and len(a) == 30
    assert all(bar.low <= min(bar.open, bar.close)
               and bar.high >= max(bar.open, bar.close) for bar in a)
