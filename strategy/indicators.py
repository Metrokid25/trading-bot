"""RSI / 볼린저밴드 / 이동평균 지표."""
from __future__ import annotations

import numpy as np


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
    """returns (upper, middle, lower)."""
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
