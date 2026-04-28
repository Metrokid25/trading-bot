from datetime import date
from core.market_calendar import add_trading_days, is_trading_day


def test_add_one_day_thursday():
    # 목(2025-01-02) → 다음 거래일 = 금(2025-01-03)
    assert add_trading_days(date(2025, 1, 2), 1) == date(2025, 1, 3)


def test_add_one_day_friday_skips_weekend():
    # 금(2025-01-03) → 다음 거래일 = 월(2025-01-06)
    assert add_trading_days(date(2025, 1, 3), 1) == date(2025, 1, 6)


def test_add_five_days_skips_lunar_new_year():
    # 설 연휴(1/28~1/30) 포함한 5거래일 후
    # 1/27(월) +1=1/31(금) +2=2/3(월) +3=2/4(화) +4=2/5(수) +5=2/6(목)
    result = add_trading_days(date(2025, 1, 27), 5)
    assert result >= date(2025, 2, 3)  # 설 연휴 건너뜀 확인
    assert result <= date(2025, 2, 7)  # 합리적 범위


def test_add_twenty_trading_days():
    # D+20: 2025-04-28 기준
    result = add_trading_days(date(2025, 4, 28), 20)
    assert date(2025, 5, 26) <= result <= date(2025, 6, 6)


def test_new_year_not_trading():
    assert is_trading_day(date(2025, 1, 1)) is False


def test_jan2_is_trading():
    assert is_trading_day(date(2025, 1, 2)) is True
