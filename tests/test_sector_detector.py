"""SectorDetector 단위 테스트.

- pick_thresholds / is_blocked_window: 순수 함수 검증
- evaluate_stock: KIS API AsyncMock 으로 다양한 케이스 주입
- _scan_sector: M-of-N 집계 및 알림 발화 여부
- should_alert: 실제 aiosqlite in-memory DB
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from agents.sector_detector import SectorDetector
from config import constants as C
from data.sector_models import SectorStock
from data.sector_store import SectorStore


# ---------- 헬퍼 ----------
def _make_1m_bars(cur_open, cur_close, cur_vol, past_vol, n_past=C.VOLUME_LOOKBACK):
    """KIS 1분봉 응답 모의 (output2 형식). 최신봉 [0], 직전 N봉 이어서."""
    bars = [{"stck_oprc": str(cur_open), "stck_prpr": str(cur_close), "cntg_vol": str(cur_vol)}]
    for _ in range(n_past):
        bars.append({"stck_oprc": "100", "stck_prpr": "100", "cntg_vol": str(past_vol)})
    return bars


def _make_daily(day_open):
    today = datetime.now().strftime("%Y%m%d")
    return [{"stck_bsop_date": today, "stck_oprc": str(day_open), "stck_clpr": "100"}]


def _detector():
    kis = MagicMock()
    kis.get_minute_candles = AsyncMock()
    kis.get_daily_candles = AsyncMock()
    store = MagicMock()
    store.should_alert = AsyncMock(return_value=True)
    store.insert_alert = AsyncMock(return_value=1)
    tg = MagicMock()
    tg.notify = AsyncMock()
    return SectorDetector(kis, store, tg), kis, store, tg


# ---------- 순수 함수: 임계값 선택 ----------
def test_pick_thresholds_early():
    d, *_ = _detector()
    out = d.pick_thresholds(datetime(2026, 4, 22, 9, 15))
    assert out["vol_mult"] == C.SECTOR_A_VOL_MULT_EARLY
    assert out["return"] == C.SECTOR_A_RETURN_EARLY


def test_pick_thresholds_default():
    d, *_ = _detector()
    out = d.pick_thresholds(datetime(2026, 4, 22, 11, 0))
    assert out["vol_mult"] == C.SECTOR_A_VOL_MULT_DEFAULT
    assert out["return"] == C.SECTOR_A_RETURN_DEFAULT


def test_pick_thresholds_late():
    d, *_ = _detector()
    out = d.pick_thresholds(datetime(2026, 4, 22, 14, 45))
    assert out["vol_mult"] == C.SECTOR_A_VOL_MULT_LATE
    # 상승률 임계는 LATE 구간 조정 안 함 — DEFAULT 유지
    assert out["return"] == C.SECTOR_A_RETURN_DEFAULT


# ---------- 순수 함수: 장외/차단 판정 ----------
def test_is_blocked_pre_market():
    d, *_ = _detector()
    assert d.is_blocked_window(datetime(2026, 4, 22, 8, 30)) is True


def test_is_blocked_post_close():
    d, *_ = _detector()
    assert d.is_blocked_window(datetime(2026, 4, 22, 15, 30)) is True
    assert d.is_blocked_window(datetime(2026, 4, 22, 16, 0)) is True


def test_is_blocked_closing_auction():
    # 15:20 ~ 15:30 동시호가 차단
    d, *_ = _detector()
    assert d.is_blocked_window(datetime(2026, 4, 22, 15, 25)) is True


def test_is_blocked_normal_session():
    d, *_ = _detector()
    assert d.is_blocked_window(datetime(2026, 4, 22, 11, 0)) is False
    # 장 초반도 스캔은 허용 (임계값만 강화)
    assert d.is_blocked_window(datetime(2026, 4, 22, 9, 15)) is False


# ---------- evaluate_stock: 조건 A ----------
@pytest.mark.asyncio
async def test_evaluate_stock_all_pass():
    d, kis, *_ = _detector()
    # 거래량 500/100 = 5배, 수익률 (103-100)/100 = +3%, 양봉 (close>open)
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=102, cur_close=103, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is True
    assert m["vol_ratio"] == 5.0
    assert m["return"] == 0.03
    assert m["bullish"] is True


@pytest.mark.asyncio
async def test_evaluate_stock_volume_fail():
    d, kis, *_ = _detector()
    # 거래량 200/100 = 2배 (<3배 미달), 나머지는 OK
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=102, cur_close=103, cur_vol=200, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is False
    assert m["vol_ratio"] == 2.0


@pytest.mark.asyncio
async def test_evaluate_stock_return_fail():
    d, kis, *_ = _detector()
    # 거래량 OK, 수익률 +1% (<2% 미달), 양봉
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=100.5, cur_close=101, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is False
    assert m["return"] == 0.01


@pytest.mark.asyncio
async def test_evaluate_stock_bearish():
    d, kis, *_ = _detector()
    # 음봉: close(103) < open(104)
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=104, cur_close=103, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is False
    assert m["bullish"] is False


@pytest.mark.asyncio
async def test_evaluate_stock_insufficient_bars():
    d, kis, *_ = _detector()
    # 과거 N봉 부족 (LOOKBACK+1 미만)
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=102, cur_close=103, cur_vol=500, past_vol=100, n_past=5,
    )
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is False
    assert m["reason"] == "insufficient_bars"


@pytest.mark.asyncio
async def test_evaluate_stock_zero_open():
    d, kis, *_ = _detector()
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=102, cur_close=103, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=0)
    ok, m = await d.evaluate_stock("000001", {"vol_mult": 3.0, "return": 0.02})
    assert ok is False
    assert m["reason"] == "no_day_open"


# ---------- _scan_sector: M-of-N + 알림 발화 ----------
@pytest.mark.asyncio
async def test_scan_sector_triggers_alert():
    d, kis, store, tg = _detector()
    # 모든 종목 통과 케이스 → 3종목 ≥ SECTOR_B_MIN_PASSED(=3)
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=102, cur_close=103, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    stocks = [
        SectorStock(pick_id=1, sector_name="AI", stock_code=f"00000{i}",
                    stock_name=f"종목{i}", added_order=i)
        for i in range(3)
    ]
    now = datetime(2026, 4, 22, 10, 30)
    await d._scan_sector("AI", stocks, {"vol_mult": 3.0, "return": 0.02}, now)
    store.insert_alert.assert_awaited_once()
    tg.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_sector_below_threshold():
    d, kis, store, tg = _detector()
    # 전체 수익률 미달 → passed=0 → 알림 발화 없음
    kis.get_minute_candles.return_value = _make_1m_bars(
        cur_open=100.5, cur_close=101, cur_vol=500, past_vol=100,
    )
    kis.get_daily_candles.return_value = _make_daily(day_open=100)
    stocks = [
        SectorStock(pick_id=1, sector_name="AI", stock_code=f"00000{i}",
                    stock_name=f"종목{i}", added_order=i)
        for i in range(5)
    ]
    now = datetime(2026, 4, 22, 10, 30)
    await d._scan_sector("AI", stocks, {"vol_mult": 3.0, "return": 0.02}, now)
    store.insert_alert.assert_not_awaited()
    tg.notify.assert_not_awaited()


# ---------- should_alert: 실제 SQLite in-memory ----------
@pytest_asyncio.fixture
async def real_store():
    store = SectorStore(db_path=":memory:")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_should_alert_empty_history(real_store):
    # 이력이 전혀 없으면 True
    assert await real_store.should_alert("AI", 1, cooldown_min=5) is True


@pytest.mark.asyncio
async def test_should_alert_inside_cooldown(real_store):
    now = datetime.now()
    await real_store.insert_alert(
        sector_name="AI", stage=1, triggered_at=now,
        passed_stocks=[], metrics={}, threshold_used={},
    )
    # 방금 기록 → cooldown=5분 이내 → False
    assert await real_store.should_alert("AI", 1, cooldown_min=5) is False


@pytest.mark.asyncio
async def test_should_alert_outside_cooldown(real_store):
    past = datetime.now() - timedelta(minutes=10)
    await real_store.insert_alert(
        sector_name="AI", stage=1, triggered_at=past,
        passed_stocks=[], metrics={}, threshold_used={},
    )
    # 10분 전 기록 → cooldown=5분 경과 → True
    assert await real_store.should_alert("AI", 1, cooldown_min=5) is True


@pytest.mark.asyncio
async def test_should_alert_stage_independent(real_store):
    now = datetime.now()
    await real_store.insert_alert(
        sector_name="AI", stage=1, triggered_at=now,
        passed_stocks=[], metrics={}, threshold_used={},
    )
    # Stage 1 쿨다운 중이어도 Stage 2는 독립적으로 허용
    assert await real_store.should_alert("AI", 2, cooldown_min=5) is True
