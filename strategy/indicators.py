"""기술적 지표: RSI / BB / MA (레거시) + VWAP / MACD / ATR / EMA."""
from __future__ import annotations

import numpy as np


# --- 레거시 (백테스트/테스트 호환용 유지) -----------------------------------
def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    arr = np.asarray(closes, dtype=float)
    delta = np.diff(arr[-(period + 1):])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(closes: list[float], period: int = 20, std: float = 2.0) -> tuple[float, float, float]:
    if len(closes) < period:
        nan = float("nan")
        return nan, nan, nan
    arr = np.asarray(closes[-period:], dtype=float)
    mid = arr.mean()
    sd = arr.std(ddof=0)
    return mid + std * sd, mid, mid - std * sd


def ma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return float("nan")
    return float(np.mean(closes[-period:]))


# --- 신규 지표 --------------------------------------------------------------
def ema(closes: list[float], period: int) -> float:
    """단일값 EMA (마지막 값)."""
    if len(closes) < period:
        return float("nan")
    arr = np.asarray(closes, dtype=float)
    k = 2.0 / (period + 1.0)
    e = float(arr[:period].mean())
    for v in arr[period:]:
        e = (float(v) - e) * k + e
    return e


def ema_series(closes: list[float], period: int) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period:
        return np.array([], dtype=float)
    k = 2.0 / (period + 1.0)
    out = np.empty(len(arr) - period + 1, dtype=float)
    out[0] = arr[:period].mean()
    for i, v in enumerate(arr[period:], start=1):
        out[i] = (v - out[i - 1]) * k + out[i - 1]
    return out


def vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[int]) -> float:
    """세션 누적 VWAP. 거래량이 모두 0이면 단순평균(close) 로 폴백."""
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n == 0:
        return float("nan")
    h = np.asarray(highs[:n], dtype=float)
    l = np.asarray(lows[:n], dtype=float)
    c = np.asarray(closes[:n], dtype=float)
    v = np.asarray(volumes[:n], dtype=float)
    tp = (h + l + c) / 3.0
    vol_sum = v.sum()
    if vol_sum <= 0:
        return float(tp.mean())
    return float((tp * v).sum() / vol_sum)


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """반환: (macd, signal, histogram). 데이터 부족 시 NaN."""
    if len(closes) < slow + signal:
        nan = float("nan")
        return nan, nan, nan
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    # 길이 맞추기
    m = min(len(ef), len(es))
    macd_line = ef[-m:] - es[-m:]
    if len(macd_line) < signal:
        nan = float("nan")
        return nan, nan, nan
    # signal = EMA of macd_line
    k = 2.0 / (signal + 1.0)
    sig = float(macd_line[:signal].mean())
    for v in macd_line[signal:]:
        sig = (float(v) - sig) * k + sig
    macd_val = float(macd_line[-1])
    return macd_val, sig, macd_val - sig


def macd_hist_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> list[float]:
    """히스토그램 시계열 (끝에서 두 값만 봐도 충분하지만 편의상 제공)."""
    if len(closes) < slow + signal:
        return []
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    m = min(len(ef), len(es))
    macd_line = ef[-m:] - es[-m:]
    if len(macd_line) < signal:
        return []
    k = 2.0 / (signal + 1.0)
    sig_vals = np.empty(len(macd_line) - signal + 1, dtype=float)
    sig_vals[0] = macd_line[:signal].mean()
    for i, v in enumerate(macd_line[signal:], start=1):
        sig_vals[i] = (v - sig_vals[i - 1]) * k + sig_vals[i - 1]
    hist = macd_line[-len(sig_vals):] - sig_vals
    return hist.tolist()


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Wilder ATR. 데이터 부족 시 NaN."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return float("nan")
    h = np.asarray(highs[-(period + 1):], dtype=float)
    l = np.asarray(lows[-(period + 1):], dtype=float)
    c = np.asarray(closes[-(period + 1):], dtype=float)
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    # Wilder smoothing: 최초 값이 단순평균
    a = float(tr.mean())
    # (전체 시계열로 더 정교하게 계산하려면 별도 함수)
    return a


def atr_wilder(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """전체 시계열에 Wilder 재귀 평활 적용한 ATR (마지막 값)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return float("nan")
    h = np.asarray(highs[:n], dtype=float)
    l = np.asarray(lows[:n], dtype=float)
    c = np.asarray(closes[:n], dtype=float)
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    if len(tr) < period:
        return float("nan")
    a = float(tr[:period].mean())
    for v in tr[period:]:
        a = (a * (period - 1) + float(v)) / period
    return a
