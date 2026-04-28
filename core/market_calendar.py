"""KRX (Korea Exchange) trading calendar utilities.

Uses pandas_market_calendars for accurate Korean market holidays
(Lunar New Year, Chuseok, public holidays, special closures).
"""
from datetime import date, timedelta
import pandas_market_calendars as mcal

_KRX = mcal.get_calendar("XKRX")  # ISO MIC code for Korea Exchange


def add_trading_days(start: date, n: int) -> date:
    """Return the date that is `n` trading days after `start`.

    Args:
        start: anchor date (the pick date itself)
        n: number of trading days to add (e.g., 20 for D+20)

    Returns:
        date object representing the n-th trading day after `start`.
        `start` itself is NOT included in the count.
        Example: if start=Mon and n=1, returns Tue (assuming both trading days).
    """
    end_window = start + timedelta(days=n * 2 + 14)
    schedule = _KRX.schedule(start_date=start, end_date=end_window)

    trading_days = schedule.index.date
    future_days = [d for d in trading_days if d > start]

    if len(future_days) < n:
        raise ValueError(
            f"Not enough trading days in window: requested {n}, found {len(future_days)}"
        )
    return future_days[n - 1]


def is_trading_day(d: date) -> bool:
    """Check if the given date is a KRX trading day."""
    schedule = _KRX.schedule(start_date=d, end_date=d)
    return len(schedule) > 0
