"""evaluate_day_acc(저점 분할매집, --mode acc) 단위 테스트 — 합성 1분봉으로 핵심 로직 고정.

검증: 프리장 급등 게이트 / 눌림 미발생 / 다지기 확인 후 다음 봉부터 체결(룩어헤드
      금지) / 분할 매집 평단 / 재돌파 익절 / 다지기의 떨어지는칼날 회피 / 트레일링.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backtest.run_premarket_pullback import _resample_3m, evaluate_day_acc
from backtest.toss_client import Bar

KST = timezone(timedelta(hours=9))


def _b(hh: int, mm: int, o: int, h: int, l: int, c: int, v: int = 100) -> Bar:
    return Bar(ts=datetime(2026, 6, 1, hh, mm, tzinfo=KST),
               open=o, high=h, low=l, close=c, volume=v)


def _pre_surge(prev_close: int, surge: float) -> list[Bar]:
    """프리장(08:00~08:02) 급등 봉. 고가 = prev_close*(1+surge)."""
    hi = int(prev_close * (1 + surge))
    return [_b(8, 0, prev_close, hi, prev_close, hi),
            _b(8, 1, hi, hi, prev_close, prev_close),
            _b(8, 2, prev_close, prev_close, prev_close - 1, prev_close)]


# 공통 룰 파라미터(소수 비율, entry_bands 는 %)
CFG = dict(pre_surge=0.05, pullback_min=0.03, support_tol=0.005,
           entry_bands=(1.0, 0.0, -1.0), stop_pct=0.04,
           tp_levels=(0.05, 0.10, 0.15), trail_pct=0.05)


# -------------------- _resample_3m --------------------
def test_resample_3m_clock_buckets():
    bars = [_b(9, m, 100 + m, 110 + m, 90 + m, 100 + m) for m in range(6)]
    out = _resample_3m(bars)
    assert len(out) == 2                     # 09:00~02, 09:03~05
    assert out[0].open == 100                # 첫 봉 시가
    assert out[0].high == 112                # max(110,111,112)
    assert out[0].low == 90                  # min(90,91,92)
    assert out[0].close == 102               # 09:02 종가
    assert out[0].volume == 300              # 3봉 합


# -------------------- 게이트 / 눌림 --------------------
def test_gate_fail_no_pre_surge():
    pc = 10000
    bars = [_b(8, 0, 10000, 10100, 9950, 10050)]            # +1% 뿐(게이트 5% 미달)
    bars += [_b(9, m, 10000, 10100, 9900, 10000) for m in range(30)]
    assert evaluate_day_acc("X", "x", bars, pc, consol_bars=3, **CFG) is None


def test_no_pullback_returns_none():
    pc = 10000
    bars = _pre_surge(pc, 0.10)                              # +10% 급등(게이트 통과)
    bars += [_b(9, m, 11000, 11050, 10980, 11020) for m in range(30)]  # 눌림 없이 횡보
    assert evaluate_day_acc("X", "x", bars, pc, consol_bars=3, **CFG) is None


# -------------------- 해피패스(다지기→다음봉 매집→재돌파→익절) --------------------
def _scenario_entry() -> tuple[int, list[Bar]]:
    """급등 → 눌림(저가 10600) → 다지기 3봉(레벨 세팅) → 다음 봉 매집 → 재돌파.

    3분봉 타임라인:
      09:00 고점권(day_high=11000) / 09:03 눌림 저가 10600 등록
      09:06~14 다지기 3봉(저가 10600·10590·10580, 지지 유지) → 09:12 봉 종료 시
               consol=3, 지지 10580 기준 레벨 [10685.8, 10580, 10474.2] 세팅
      09:15 저가 10550 → 위 두 레벨만 체결(평단 10632.9). 셋째(10474.2)는 미체결
      09:18 종가 11050 > 아침고점 11000 재돌파 → 매집 종료
    """
    pc = 10000
    bars = _pre_surge(pc, 0.10)                              # 고가 11000
    # 09:00~02 고점권
    bars += [_b(9, 0, 10900, 11000, 10850, 10950),
             _b(9, 1, 10950, 10980, 10850, 10900),
             _b(9, 2, 10900, 10950, 10800, 10850)]
    # 09:03~05 눌림: 저가 10600 (-3.6% from 11000)
    bars += [_b(9, 3, 10850, 10880, 10600, 10650),
             _b(9, 4, 10650, 10700, 10610, 10680),
             _b(9, 5, 10680, 10720, 10620, 10700)]
    # 09:06~14 다지기 3봉: 저가 10600 → 10590 → 10580 (지지 유지, 이탈 없음)
    for base, lo in ((6, 10600), (9, 10590), (12, 10580)):
        bars += [_b(9, base, 10700, 10760, lo + 20, 10680),
                 _b(9, base + 1, 10680, 10740, lo + 10, 10650),
                 _b(9, base + 2, 10650, 10720, lo, 10660)]
    # 09:15~17 매집 봉: 저가 10550 → 레벨 10685.8/10580 체결(10474.2 미체결)
    bars += [_b(9, 15, 10660, 10700, 10600, 10650),
             _b(9, 16, 10650, 10680, 10550, 10620),
             _b(9, 17, 10620, 10690, 10580, 10660)]
    # 09:18~20 재돌파: 종가 11050 > 11000
    bars += [_b(9, 18, 10660, 10900, 10650, 10850),
             _b(9, 19, 10850, 11000, 10800, 10950),
             _b(9, 20, 10950, 11080, 10900, 11050)]
    return pc, bars


def test_acc_happy_path_profits():
    pc, bars = _scenario_entry()
    # 09:21~26 급등: TP 5/10/15% (평단 10632.9 기준 11164.5/11696.2/12227.8) 전부 체결
    bars += [_b(9, 21, 11050, 11500, 11000, 11450),
             _b(9, 22, 11450, 11800, 11400, 11750),
             _b(9, 23, 11750, 11800, 11700, 11780),
             _b(9, 24, 11780, 12300, 11750, 12250),
             _b(9, 25, 12250, 12400, 12200, 12350),
             _b(9, 26, 12350, 12400, 12300, 12380)]
    bars += [_b(9, m, 12300, 12350, 12250, 12300) for m in range(27, 40)]
    t = evaluate_day_acc("005930", "삼성전자", bars, pc, consol_bars=3, **CFG)
    assert t is not None
    assert t.entry == 10633          # (10685.8+10580)/2 — 두 레벨만 체결된 평단
    assert t.ret > 0
    assert t.reason == "3TP"         # 3구간 전부 익절


def test_acc_orders_not_filled_on_setup_bar():
    """레벨을 세팅한 봉(09:12)의 저가로는 체결 불가 — 다음 봉부터 유효(룩어헤드 금지)."""
    pc, bars = _scenario_entry()
    # 매집 봉(09:15~) 이후를 잘라내고 레벨 위 횡보로 종료
    bars = [b for b in bars if not (b.ts.time().hour == 9 and b.ts.time().minute >= 15)]
    bars += [_b(9, m, 10700, 10760, 10690, 10720) for m in range(15, 40)]
    t = evaluate_day_acc("X", "x", bars, pc, consol_bars=3, **CFG)
    # 다지기 봉들 저가(10580~10600)는 첫 레벨(10685.8)보다 낮지만 세팅 이전이므로
    # 체결로 치지 않는다. 이후 저가 10690 은 어떤 레벨에도 닿지 않음 → 미진입.
    assert t is None


def test_acc_trailing_stop_locks_profit():
    """첫 익절 후 하락 시 트레일링(고점 -5%)이 잔량을 평단 위에서 청산(사유 TR)."""
    pc, bars = _scenario_entry()
    # 09:21~23: TP1(11164.5)만 체결(고가 11250) → 트레일 스탑 = 11250*0.95 = 10687.5
    bars += [_b(9, 21, 11050, 11250, 11000, 11200),
             _b(9, 22, 11200, 11240, 11150, 11180),
             _b(9, 23, 11180, 11220, 11160, 11170)]
    # 09:24~: 하락 → 저가 10600 ≤ 10687.5 → 잔량 트레일 청산
    bars += [_b(9, 24, 11170, 11180, 10900, 10950),
             _b(9, 25, 10950, 10980, 10600, 10650)]
    bars += [_b(9, m, 10650, 10700, 10600, 10650) for m in range(26, 40)]
    t = evaluate_day_acc("X", "x", bars, pc, consol_bars=3, **CFG)
    assert t is not None
    assert t.reason == "1TP/TR"
    assert t.ret > 0                 # 트레일 덕에 잔량도 평단(10632.9) 위에서 정리


# -------------------- 다지기의 떨어지는 칼날 회피 --------------------
def _scenario_knife() -> tuple[int, list[Bar]]:
    """급등 → 눌림 후 저점이 계속 깨지는(다지기 실패) 하락."""
    pc = 10000
    bars = _pre_surge(pc, 0.10)
    bars += [_b(9, 0, 10900, 11000, 10850, 10950),
             _b(9, 1, 10950, 10980, 10850, 10900),
             _b(9, 2, 10900, 10950, 10800, 10850)]
    # 매 3분봉마다 저점을 계속 낮춤(다지기 못 만듦)
    lows = [10600, 10400, 10200, 10000, 9800, 9600, 9400, 9200]
    m = 3
    for lo in lows:
        bars += [_b(9, m, lo + 200, lo + 220, lo, lo + 50),
                 _b(9, m + 1, lo + 50, lo + 80, lo - 30, lo + 10),
                 _b(9, m + 2, lo + 10, lo + 40, lo - 60, lo - 20)]
        m += 3
    bars += [_b(9, mm, 9200, 9250, 9150, 9200) for mm in range(m, m + 6)]
    return pc, bars


def test_acc_consolidation_avoids_knife():
    pc, bars = _scenario_knife()
    on = evaluate_day_acc("X", "x", bars, pc, consol_bars=3, **CFG)
    off = evaluate_day_acc("X", "x", bars, pc, consol_bars=0, **CFG)
    # 다지기 OFF: 첫 저점에 즉시 매집했다 더 빠져 손절
    assert off is not None and off.reason == "SL"
    # 다지기 ON: 저점이 계속 깨져 매집을 미뤄 손실을 피함(미진입) 또는 손실이 더 작음
    assert on is None or on.ret >= off.ret
