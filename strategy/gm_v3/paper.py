"""gm_v3 페이퍼 시뮬레이터 — 시그널 → 가상 체결 → 트레이드 기록.

체결 가정 (2026-07-04 형 결정: 기본 '다음날 시가'):
  - BUY/SELL(R7/R8/R9): 종가 신호 → 다음 봉 시가 체결. fill_mode='close' 로
    당일 종가 체결 전환 가능(낙관적 — 리포트에 명시).
  - R10 손절만 예외: 걸어둔 스탑 주문으로 보고 당일 트리거 가격 체결.
  - 같은 날 SELL 여러 개 → 우선순위(R10>R16>R9>R15>R7>R8>R14, models.SELL_PRIORITY)
    최상위 1개만 적용. 밀린 R8/R14 의 원샷 상태는 엔진이 복원한다 (rules.py).

수익률 회계: 목표 노셔널 1.0 기준. 청산 시 (체결가/평단-1)×투입비중×청산비율
을 실현손익으로 누적, 전량 청산 시 트레이드 확정.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from strategy.gm_v3.config import GmV3Config
from strategy.gm_v3.models import (
    SELL_PRIORITY, DailyBar, Signal, SignalType, StockState,
)
from strategy.gm_v3.rules import evaluate_day


@dataclass(slots=True)
class PaperTrade:
    code: str
    opened_on: Date
    closed_on: Date
    entry_avg: float
    realized: float           # 목표 노셔널 대비 실현 수익률
    max_invested: float       # 최대 투입 비중
    exit_rules: list[str] = field(default_factory=list)
    forced_eor: bool = False  # 기간 종료 강제 청산 여부


def simulate(code: str, bars: list[DailyBar], cfg: GmV3Config, *,
             fill_mode: str = "next_open",
             act_from: Date | None = None,
             act_to: Date | None = None,
             ) -> tuple[list[PaperTrade], list[Signal]]:
    """일봉 시퀀스에 룰 엔진을 돌려 (완결 트레이드, 발동 시그널) 반환.

    act_from 이전 봉은 지표 워밍업으로만 사용(체결/시그널 수집 안 함).
    """
    assert fill_mode in ("next_open", "close")
    st = StockState(code=code)
    all_signals: list[Signal] = []
    trades: list[PaperTrade] = []

    pending_buys: list[Signal] = []
    pending_sells: list[Signal] = []
    realized = 0.0
    opened_on: Date | None = None
    max_invested = 0.0
    exit_rules: list[str] = []

    def in_window(d: Date) -> bool:
        return ((act_from is None or d >= act_from)
                and (act_to is None or d <= act_to))

    def do_sell(px: float, frac: float, rule: str, day: Date) -> None:
        nonlocal realized
        pos = st.position
        if pos is None:
            return
        realized += (px / pos.entry_avg - 1) * pos.invested * frac
        exit_rules.append(rule)
        st.apply_sell(frac)
        if st.position is None:
            trades.append(PaperTrade(code, opened_on, day, pos.entry_avg,
                                     realized, max_invested,
                                     exit_rules.copy(), rule == "EOR"))
            _reset()

    def _reset() -> None:
        nonlocal realized, opened_on, max_invested
        realized = 0.0
        opened_on = None
        max_invested = 0.0
        exit_rules.clear()

    last_in_window: DailyBar | None = None
    for bar in bars:
        actionable = in_window(bar.day)
        # ---- 전일 신호 체결 (다음날 시가) ----
        if actionable:
            for s in pending_sells:
                do_sell(bar.open, s.weight, s.rule, bar.day)
            pending_sells.clear()
            for s in pending_buys:
                if st.position is None:
                    opened_on = bar.day
                st.apply_buy(bar.open, s.weight, bar.day)
                max_invested = max(max_invested, st.position.invested)
            pending_buys.clear()

        sigs = evaluate_day(st, bar, cfg)
        if not actionable:
            continue
        all_signals.extend(sigs)
        last_in_window = bar

        sells = sorted((s for s in sigs if s.type == SignalType.SELL),
                       key=lambda s: SELL_PRIORITY.get(s.rule, 9))
        if sells:
            top = sells[0]
            if top.rule == "R10":
                # 당일 스탑 체결. 갭하락으로 시가가 이미 스탑 아래면 시가 체결
                # (스탑 주문은 시가보다 좋게 체결될 수 없음 — 리뷰 HIGH 반영)
                do_sell(min(bar.open, top.price), 1.0, "R10", bar.day)
            elif fill_mode == "close":
                do_sell(bar.close, top.weight, top.rule, bar.day)
            else:
                pending_sells.append(top)
        for s in (s for s in sigs if s.type == SignalType.BUY):
            if fill_mode == "close":
                if st.position is None:
                    opened_on = bar.day
                st.apply_buy(bar.close, s.weight, bar.day)
                max_invested = max(max_invested, st.position.invested)
            else:
                pending_buys.append(s)

    # ---- 기간 종료: 잔여 포지션 강제 청산 (마지막 관측 종가) ----
    if st.position is not None and last_in_window is not None:
        do_sell(last_in_window.close, 1.0, "EOR", last_in_window.day)

    return trades, all_signals
