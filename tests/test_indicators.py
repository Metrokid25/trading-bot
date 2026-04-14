from strategy.indicators import bollinger, ma, rsi


def test_rsi_bounds():
    up = list(range(1, 50))
    assert 70 <= rsi(up) <= 100

    down = list(range(50, 0, -1))
    assert 0 <= rsi(down) <= 30


def test_bollinger_order():
    closes = [100 + i * 0.5 for i in range(30)]
    upper, mid, lower = bollinger(closes)
    assert lower < mid < upper


def test_ma():
    assert ma([1, 2, 3, 4, 5], 5) == 3.0
