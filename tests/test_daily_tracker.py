"""fetch_daily_candles_for_pick 단위 테스트.

KISClient는 AsyncMock으로 대체. 실제 HTTP 호출 없음.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.daily_tracker import DailyOHLCV, fetch_daily_candles_for_pick


# ---------- 헬퍼 ----------

def _make_kis_daily_row(date_str, oprc, hgpr, lwpr, clpr, vol, value):
    return {
        "stck_bsop_date": date_str,  # 'YYYYMMDD'
        "stck_oprc": str(oprc),
        "stck_hgpr": str(hgpr),
        "stck_lwpr": str(lwpr),
        "stck_clpr": str(clpr),
        "acml_vol": str(vol),
        "acml_tr_pbmn": str(value),
    }


def _mock_client(rows):
    client = MagicMock()
    client.get_daily_candles = AsyncMock(return_value=rows)
    return client


# ---------- 테스트 케이스 ----------

@pytest.mark.asyncio
async def test_fetch_returns_dailyohlcv_list():
    """KIS mock 응답 3행 → DailyOHLCV 3개 반환, 필드 매핑 검증."""
    rows = [
        _make_kis_daily_row("20260506", 10000, 11000, 9500, 10500, 1_000_000, 10_500_000_000),
        _make_kis_daily_row("20260507", 10500, 12000, 10200, 11500, 1_200_000, 13_800_000_000),
        _make_kis_daily_row("20260508", 11500, 12500, 11000, 12000, 900_000, 10_800_000_000),
    ]
    client = _mock_client(rows)

    result = await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=20)

    assert len(result) == 3
    assert all(isinstance(r, DailyOHLCV) for r in result)
    r0 = result[0]
    assert r0.trade_date == "2026-05-06"
    assert r0.open == 10000
    assert r0.high == 11000
    assert r0.low == 9500
    assert r0.close == 10500
    assert r0.volume == 1_000_000
    assert r0.value == 10_500_000_000


@pytest.mark.asyncio
async def test_fetch_sorts_ascending():
    """KIS가 내림차순으로 반환해도 출력은 trade_date 오름차순."""
    rows = [
        _make_kis_daily_row("20260508", 11500, 12500, 11000, 12000, 900_000, 10_800_000_000),
        _make_kis_daily_row("20260507", 10500, 12000, 10200, 11500, 1_200_000, 13_800_000_000),
        _make_kis_daily_row("20260506", 10000, 11000, 9500, 10500, 1_000_000, 10_500_000_000),
    ]
    client = _mock_client(rows)

    result = await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=20)

    dates = [r.trade_date for r in result]
    assert dates == sorted(dates)
    assert dates[0] == "2026-05-06"
    assert dates[-1] == "2026-05-08"


@pytest.mark.asyncio
async def test_fetch_empty_response():
    """KIS가 빈 list 반환 → 빈 list 반환 (raise 안 함)."""
    client = _mock_client([])

    result = await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=20)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_date", ["2026-13-99", "20260506", "", "2026/05/06", "abc"])
async def test_fetch_invalid_pick_date_format(bad_date):
    """pick_date 형식 불량 → ValueError."""
    client = _mock_client([])

    with pytest.raises(ValueError):
        await fetch_daily_candles_for_pick(client, "005930", bad_date)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ticker", ["", "12345", "ABCDEF", "1234567", "123 45"])
async def test_fetch_invalid_ticker(bad_ticker):
    """ticker 형식 불량 → ValueError."""
    client = _mock_client([])

    with pytest.raises(ValueError):
        await fetch_daily_candles_for_pick(client, bad_ticker, "2026-05-06")


@pytest.mark.asyncio
async def test_fetch_negative_lookback():
    """lookback_days < 0 → ValueError."""
    client = _mock_client([])

    with pytest.raises(ValueError):
        await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=-1)


@pytest.mark.asyncio
async def test_fetch_skips_malformed_row():
    """5행 중 stck_oprc 누락 1행 → 4개만 반환 + 경고 로그 1회."""
    rows = [
        _make_kis_daily_row("20260506", 10000, 11000, 9500, 10500, 1_000_000, 10_500_000_000),
        _make_kis_daily_row("20260507", 10500, 12000, 10200, 11500, 1_200_000, 13_800_000_000),
        {   # stck_oprc 누락 — 말폼 행
            "stck_bsop_date": "20260508",
            "stck_hgpr": "12500",
            "stck_lwpr": "11000",
            "stck_clpr": "12000",
            "acml_vol": "900000",
            "acml_tr_pbmn": "10800000000",
        },
        _make_kis_daily_row("20260509", 12000, 13000, 11800, 12800, 800_000, 10_240_000_000),
        _make_kis_daily_row("20260512", 12800, 14000, 12500, 13500, 750_000, 10_125_000_000),
    ]
    client = _mock_client(rows)

    with patch("core.daily_tracker.logger") as mock_logger:
        result = await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=20)

    assert len(result) == 4
    assert "2026-05-08" not in [r.trade_date for r in result]
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_calls_kis_with_correct_range():
    """pick_date='2026-05-06', lookback_days=20 → KIS 호출 인자 검증."""
    client = _mock_client([])

    await fetch_daily_candles_for_pick(client, "005930", "2026-05-06", lookback_days=20)

    client.get_daily_candles.assert_called_once_with(
        "005930", "20260506", "20260526", period="D"
    )
