"""매수 시그널 — PULLBACK (v1) + BREAKOUT (v5) 듀얼 채널.

evaluate_buy(..., allow_breakout: bool) 가 외부 진입점.
  1) 먼저 PULLBACK 평가 (v1 원형)
  2) PULLBACK 미발화 AND allow_breakout=True → BREAKOUT 평가
  3) 둘 다 실패면 None

엔진은 `BacktestConfig.allow_breakout` 로 채널 토글.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from config.constants import (
    ATR_PERIOD,
    BREAKOUT_HIGH_LOOKBACK,
    BREAKOUT_VOLUME_MULT,
    BREAKOUT_VOL_LOOKBACK_BARS,
    EMA_SHORT,
    HTF_MULTIPLIER,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    NO_NEW_BUY_AFTER,
    NO_TRADE_END,
    NO_TRADE_START,
    SignalType,
    VOLUME_LOOKBACK,
    VOLUME_SURGE_MULT,
)
from data.candle_store import CandleBuffer
from data.models import Signal
from strategy.indicators import atr_wilder, atr_wilder_series, ema, macd_hist_series, vwap

MACD_FLIP_LOOKBACK = 5


# -------------------- PULLBACK (v1) --------------------
def _evaluate_pullback(code: str, buf: CandleBuffer, ts: datetime) -> Signal | None:
    t = ts.time()
    if NO_TRADE_START <= t < NO_TRADE_END:
        return None
    if t >= NO_NEW_BUY_AFTER:
        return None

    candles = buf.candles()
    if len(candles) < max(MACD_SLOW + MACD_SIGNAL, VOLUME_LOOKBACK + 2, ATR_PERIOD + 2):
        return None

    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    price = closes[-1]

    today = candles[-1].ts.date()
    sess_idx = next((i for i, c in enumerate(candles) if c.ts.date() == today), len(candles) - 1)
    vwap3 = vwap(highs[sess_idx:], lows[sess_idx:], closes[sess_idx:], vols[sess_idx:])
    hist_series = macd_hist_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if len(hist_series) < 2:
        return None
    hist_now = hist_series[-1]
    hist_prev = hist_series[-2]
    atr_val = atr_wilder(highs, lows, closes, ATR_PERIOD)
    ema9 = ema(closes, EMA_SHORT)

    cond_vwap = (lows[-1] <= vwap3 * 1.002) and (closes[-1] >= vwap3) and (closes[-1] > opens[-1])
    vol_window = vols[-(VOLUME_LOOKBACK + 1):-1]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
    cond_volume = avg_vol > 0 and vols[-1] >= avg_vol * VOLUME_SURGE_MULT
    recent_flip = any(
        hist_series[i - 1] < 0 <= hist_series[i]
        for i in range(max(1, len(hist_series) - MACD_FLIP_LOOKBACK), len(hist_series))
    )
    cond_macd = hist_now > 0 and recent_flip

    if not (cond_vwap and cond_volume and cond_macd):
        return None

    htf = buf.resample(HTF_MULTIPLIER)
    if len(htf) < MACD_SLOW + MACD_SIGNAL:
        return None
    h_h = [c.high for c in htf]
    h_l = [c.low for c in htf]
    h_c = [c.close for c in htf]
    h_v = [c.volume for c in htf]
    h_sess = next((i for i, c in enumerate(htf) if c.ts.date() == today), len(htf) - 1)
    vwap15 = vwap(h_h[h_sess:], h_l[h_sess:], h_c[h_sess:], h_v[h_sess:])
    h_hist = macd_hist_series(h_c, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if not h_hist:
        return None
    if not (h_c[-1] > vwap15 and h_hist[-1] > 0):
        return None

    score = 0
    if ema9 == ema9 and price > ema9:
        score += 1

    # v6 step1: 동적 사이징용 — 최근 20개 봉의 ATR 평균. 데이터 부족 시 None.
    atr_series = atr_wilder_series(highs, lows, closes, ATR_PERIOD)
    atr_avg20: float | None = float(np.mean(atr_series[-20:])) if atr_series else None

    # v6 step2: 시그널 품질 점수 (각 0~1, 평균 → signal_quality).
    # 임계 튜닝(0.530→분산 확대): vol 2.0~4.0배, macd hist ≥ price×0.05%.
    try:
        vwap_score = max(0.0, min(1.0, (price - vwap3) / vwap3 / 0.005))
        vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0.0
        vol_score = max(0.0, min(1.0, (vol_ratio - 2.0) / 2.0))
        macd_score = (
            max(0.0, min(1.0, hist_now / (price * 0.001))) if price > 0 else 0.0
        )
        signal_quality: float | None = (vwap_score + vol_score + macd_score) / 3.0
    except Exception:
        vwap_score = vol_score = macd_score = 0.0
        signal_quality = None

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
            "atr_avg20": atr_avg20,
            "signal_quality": signal_quality,
            "vwap_score": vwap_score,
            "vol_score": vol_score,
            "macd_score": macd_score,
            "vwap3": vwap3,
            "vwap15": vwap15,
            "ema9": ema9,
            "hist": hist_now,
            "score": score,
        },
    )


# -------------------- BREAKOUT (v5) --------------------
def _evaluate_breakout(code: str, buf: CandleBuffer, ts: datetime) -> Signal | None:
    t = ts.time()
    if NO_TRADE_START <= t < NO_TRADE_END:
        return None
    if t >= NO_NEW_BUY_AFTER:
        return None

    candles = buf.candles()
    need = max(BREAKOUT_HIGH_LOOKBACK + 2, ATR_PERIOD + 2, 30)
    if len(candles) < need:
        return None

    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]

    # 1) 거래량 폭발 — 직전 5일(per-bar) 평균 × 3
    lookback_n = min(BREAKOUT_VOL_LOOKBACK_BARS, len(vols) - 1)
    if lookback_n <= 0:
        return None
    vol_hist = vols[-(lookback_n + 1):-1]
    avg_vol = float(np.mean(vol_hist)) if vol_hist else 0.0
    cond_vol = avg_vol > 0 and vols[-1] >= avg_vol * BREAKOUT_VOLUME_MULT

    # 2) 신고가 돌파 — 직전 60봉 최고가 초과
    prior_highs = highs[-(BREAKOUT_HIGH_LOOKBACK + 1):-1]
    prior_max = max(prior_highs) if prior_highs else 0.0
    cond_high = prior_max > 0 and highs[-1] > prior_max

    # 3) 양봉 마감
    cond_green = closes[-1] > opens[-1]

    if not (cond_vol and cond_high and cond_green):
        return None

    atr_val = atr_wilder(highs, lows, closes, ATR_PERIOD)
    if atr_val != atr_val or atr_val <= 0:
        return None

    price = closes[-1]
    return Signal(
        code=code,
        type=SignalType.BUY,
        price=price,
        ts=ts,
        reason=(
            f"HIGH>{prior_max:.0f}(60봉) vol×{vols[-1]/max(avg_vol,1):.2f} "
            f"(5d avg={avg_vol:.0f}) ATR={atr_val:.1f}"
        ),
        meta={
            "atr": atr_val,
            "prior_max_high": prior_max,
            "vol_mult": vols[-1] / max(avg_vol, 1),
        },
    )


# -------------------- 디스패처 --------------------
def evaluate_buy(
    code: str, buf: CandleBuffer, ts: datetime,
    allow_breakout: bool = False,
) -> Signal | None:
    sig = _evaluate_pullback(code, buf, ts)
    if sig:
        sig.meta["kind"] = "PULLBACK"
        return sig
    if allow_breakout:
        sig = _evaluate_breakout(code, buf, ts)
        if sig:
            sig.meta["kind"] = "BREAKOUT"
            return sig
    return None
