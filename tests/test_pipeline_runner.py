"""통합 파이프라인(core/pipeline_runner) 배선 테스트.

개별 수집 모듈은 각자 테스트가 있으므로, 여기서는 "글루"만 검증한다:
 - 빠져 있던 핵심 연결고리: 활성 이벤트 → ensure_tracking_rows 가 실제로 호출돼
   pick_daily_tracking 행이 생기는가.
 - run_full_pipeline 이 각 단계를 best-effort 로 돌고 summary 를 채우는가.

KIS는 빈 응답 fake — 데이터 0이어도 파이프라인이 깨지지 않아야 한다.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from core.pipeline_runner import run_full_pipeline
from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore
from scripts.migrations import (
    m001_phase25_tracking,
    m002_sector_repick_trading_days,
    m003_sector_pick_event_pick_date,
    m004_phase25_daily_tracking_status,
    m005_phase25_event_scoped_uniqueness,
    m006_phase25_event_pick_id,
    m007_phase25_minute_raw_rebuild,
    m008_phase25_minute_agg,
    m009_phase25_breakout_marks,
)
from scripts.migrations.migration_runner import run_migrations

_MIGRATIONS = [
    m001_phase25_tracking,
    m002_sector_repick_trading_days,
    m003_sector_pick_event_pick_date,
    m004_phase25_daily_tracking_status,
    m005_phase25_event_scoped_uniqueness,
    m006_phase25_event_pick_id,
    m007_phase25_minute_raw_rebuild,
    m008_phase25_minute_agg,
    m009_phase25_breakout_marks,
]

_TODAY = date(2026, 5, 10)


@pytest_asyncio.fixture
async def db_path(tmp_path) -> str:
    path = str(tmp_path / "pipe.db")
    # 1) 기본 스키마 (sector_picks/sector_stocks/alert_history)
    store = SectorStore(path)
    await store.open()
    await store.close()
    # 2) Phase 2.5 스키마
    run_migrations(path, _MIGRATIONS, backup=False)
    return path


async def _register_pick(path: str) -> None:
    store = SectorStore(path)
    await store.open()
    pick = SectorPick.create(_TODAY.isoformat(), raw_input="[test]", expires_days=7)
    stocks = [
        SectorStock(
            pick_id=0, sector_name="반도체", stock_code="005930",
            stock_name="삼성전자", added_order=1,
        )
    ]
    await store.upsert_sector("반도체", stocks, pick, record_pick_event=True)
    await store.close()


def _fake_kis() -> MagicMock:
    kis = MagicMock()
    kis.get_daily_candles = AsyncMock(return_value=[])
    kis.get_minute_candles_at = AsyncMock(return_value=[])
    return kis


@pytest.mark.asyncio
async def test_pipeline_creates_tracking_rows(db_path):
    """핵심 연결고리: 픽 이벤트 → ensure_tracking_rows → pick_daily_tracking 21행."""
    await _register_pick(db_path)

    summary = await run_full_pipeline(
        db_path, _fake_kis(), today=_TODAY, include_nxt=True
    )

    # 종목 1개 × (D+0~D+20) = 21행 생성
    assert summary["tracking_rows_created"] == 21
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM pick_daily_tracking").fetchone()[0]
    conn.close()
    assert n == 21


@pytest.mark.asyncio
async def test_pipeline_runs_all_stages_best_effort(db_path):
    """데이터 0이어도 모든 단계가 돌고 summary 키가 채워진다."""
    await _register_pick(db_path)

    summary = await run_full_pipeline(
        db_path, _fake_kis(), today=_TODAY, include_nxt=True
    )

    for key in ["daily", "minute_raw", "minute_agg", "breakout", "pullback"]:
        assert key in summary, f"{key} 단계 누락"
        assert summary[key] is not None, f"{key} 단계가 None — 예외가 삼켜졌다"
    assert summary["trading_day"] == _TODAY.isoformat()
    assert summary["include_nxt"] is True


@pytest.mark.asyncio
async def test_ensure_tracking_idempotent(db_path):
    """파이프라인 2회 실행해도 추적행은 중복 생성되지 않는다(멱등)."""
    await _register_pick(db_path)

    first = await run_full_pipeline(db_path, _fake_kis(), today=_TODAY)
    second = await run_full_pipeline(db_path, _fake_kis(), today=_TODAY)

    assert first["tracking_rows_created"] == 21
    assert second["tracking_rows_created"] == 0  # 이미 존재 → 0 생성
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM pick_daily_tracking").fetchone()[0]
    conn.close()
    assert n == 21
