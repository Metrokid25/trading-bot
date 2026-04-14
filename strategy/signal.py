"""매수 시그널 판정 로직.

조건 (모두 충족 시 BUY):
 - RSI(14) < 30 (과매도) 이후 반등 (이전봉 대비 상승)
 - 현재가가 볼린저밴드 하단 근접 또는 터치
 - MA5 가 MA20 을 상향 돌파 (골든크로스) 또는 MA5 상승 전환
"""
from __future__ import annotations

from datetime import datetime

from config.constants import (
    BB_PERIOD,
    BB_STD,
    MA_LONG,
    MA_MID,
    MA_SHORT,
    RSI_BUY_THRESHOLD,
    RSI_PERIOD,
    SignalType,
)
from data.models import Signal
from strategy.indicators import bollinger, ma, rsi


def evaluate_buy(code: str, closes: list[float], ts: datetime) -> Signal | None:
    if len(closes) < max(RSI_PERIOD + 1, BB_PERIOD, MA_MID) + 2:
        return None

    price = closes[-1]
    prev = closes[-2]

    cur_rsi = rsi(closes, RSI_PERIOD)
    prev_rsi = rsi(closes[:-1], RSI_PERIOD)
    _, _, bb_low = bollinger(closes, BB_PERIOD, BB_STD)

    ma5 = ma(closes, MA_SHORT)
    ma20 = ma(closes, MA_MID)
    ma5_prev = ma(closes[:-1], MA_SHORT)
    ma20_prev = ma(closes[:-1], MA_MID)

    cond_rsi = prev_rsi < RSI_BUY_THRESHOLD and cur_rsi > prev_rsi
    cond_bb = price <= bb_low * 1.005  # 하단 0.5% 이내 터치
    cond_ma_cross = ma5_prev <= ma20_prev and ma5 > ma20
    cond_ma_up = ma5 > ma5_prev

    if cond_rsi and cond_bb and (cond_ma_cross or cond_ma_up):
        return Signal(
            code=code,
            type=SignalType.BUY,
            price=price,
            ts=ts,
            reason=f"RSI {prev_rsi:.1f}→{cur_rsi:.1f}, BBlow={bb_low:.0f}, MA5={ma5:.0f}/MA20={ma20:.0f}",
            meta={"rsi": cur_rsi, "bb_low": bb_low, "ma5": ma5, "ma20": ma20},
        )
    return None
