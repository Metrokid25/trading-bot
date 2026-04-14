from datetime import datetime

from config.constants import TradeWindow
from risk.risk_manager import RiskManager


def test_time_windows():
    r = RiskManager()
    assert r.classify_window(datetime(2025, 1, 2, 9, 5)) == TradeWindow.FORBIDDEN
    assert r.classify_window(datetime(2025, 1, 2, 10, 0)) == TradeWindow.NORMAL
    assert r.classify_window(datetime(2025, 1, 2, 15, 15)) == TradeWindow.FORCE_CLOSE
    assert r.classify_window(datetime(2025, 1, 2, 16, 0)) == TradeWindow.CLOSED


def test_daily_loss_halts():
    r = RiskManager()
    r.start_equity = 10_000_000
    r.update_equity(8_900_000)  # -11%
    assert r.trading_halted
