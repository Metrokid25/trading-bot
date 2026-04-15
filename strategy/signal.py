"""멀티 타임프레임 매수 시그널.

[상위 TF 필터 - 15분봉]
 - close > VWAP15
 - MACD15 히스토그램 > 0

[진입 시그널 - 3분봉 (3개 모두 충족)]
 1. VWAP 지지 반등 양봉: low ≤ VWAP3 ≤ close, close > open
 2. 거래량 급증: vol[-1] ≥ avg(vol[-21:-1]) × 1.5
 3. MACD 히스토그램 양전환: hist[-2] < 0 ≤ hist[-1]

[가점 - 선택적]
 - close > EMA9 → score +1
"""
from __future__ import annotations

from datetime import datetime

from config.constants import (
    ATR_PERIOD,
    EMA_SHORT,
    HTF_MULTIPLIER,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    SignalType,
    VOLUME_LOOKBACK,
    VOLUME_SURGE_MULT,
)
from data.candle_store import CandleBuffer
from data.models import Signal
from strategy.indicators import atr_wilder, ema, macd_hist_series, vwap


def evaluate_buy(code: str, buf: CandleBuffer, ts: datetime) -> Signal | None:
    candles = buf.candles()
    if len(candles) < max(MACD_SLOW + MACD_SIGNAL, VOLUME_LOOKBACK + 2, ATR_PERIOD + 2):
        return None

    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]

    price = closes[-1]

    # --- 3분봉 지표 ---
    vwap3 = vwap(highs, lows, closes, vols)
    hist_series = macd_hist_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if len(hist_series) < 2:
        return None
    hist_now = hist_series[-1]
    hist_prev = hist_series[-2]
    atr_val = atr_wilder(highs, lows, closes, ATR_PERIOD)
    ema9 = ema(closes, EMA_SHORT)

    # --- 진입 3조건 ---
    cond_vwap = (lows[-1] <= vwap3 <= closes[-1]) and (closes[-1] > opens[-1])

    vol_window = vols[-(VOLUME_LOOKBACK + 1):-1]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
    cond_volume = avg_vol > 0 and vols[-1] >= avg_vol * VOLUME_SURGE_MULT

    cond_macd = hist_prev < 0 <= hist_now

    if not (cond_vwap and cond_volume and cond_macd):
        return None

    # --- 상위 TF(15분) 필터 ---
    htf = buf.resample(HTF_MULTIPLIER)
    if len(htf) < MACD_SLOW + MACD_SIGNAL:
        return None
    h_h = [c.high for c in htf]
    h_l = [c.low for c in htf]
    h_c = [c.close for c in htf]
    h_v = [c.volume for c in htf]
    vwap15 = vwap(h_h, h_l, h_c, h_v)
    h_hist = macd_hist_series(h_c, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if not h_hist:
        return None
    if not (h_c[-1] > vwap15 and h_hist[-1] > 0):
        return None

    score = 0
    if ema9 == ema9 and price > ema9:  # NaN 체크
        score += 1

    return Signal(
        code=code,
        type=SignalType.BUY,
        price=price,
        ts=ts,
        reason=(
            f"VWAP3={vwap3:.0f} vol×{vols[-1]/max(avg_vol,1):.2f} "
            f"hist {hist_prev:+.3f}→{hist_now:+.3f} ATR={atr_val:.1f} score={score}"
        ),
        meta={
            "atr": atr_val,
            "vwap3": vwap3,
            "vwap15": vwap15,
            "ema9": ema9,
            "hist": hist_now,
            "score": score,
        },
    )
