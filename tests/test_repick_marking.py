"""재픽업 마킹 단위 테스트.

in-memory SQLite 사용. 외부 의존 없음.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from core.market_calendar import add_trading_days
from core.time_utils import to_db_iso
from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore

_KST = ZoneInfo("Asia/Seoul")


# ---------- fixture ----------

@pytest_asyncio.fixture
async def store():
    s = SectorStore(db_path=":memory:")
    await s.open()
    yield s
    await s.close()


# ---------- 헬퍼 ----------

def _pick(pick_date: str = "2025-04-28", offset_days: int = 0) -> SectorPick:
    base = datetime(2025, 4, 28, 9, 0, 0, tzinfo=_KST) + timedelta(days=offset_days)
    return SectorPick(
        pick_date=pick_date,
        created_at=base,
        expires_at=base + timedelta(days=7),
    )


def _stock(sector: str, code: str, name: str) -> SectorStock:
    return SectorStock(pick_id=0, sector_name=sector, stock_code=code,
                       stock_name=name, added_order=1)


# ---------- 시나리오 (a): 첫 픽 ----------

@pytest.mark.asyncio
async def test_first_pick_marking(store: SectorStore):
    """첫 픽: is_repick=0, prev_pick_id=None, days_since=None, total_pick_count=1."""
    r = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    rows = await store.get_stocks_by_sector(r.pick_id, "반도체")
    s = rows[0]
    assert s.is_repick == 0
    assert s.prev_pick_id is None
    assert s.days_since_last_pick is None
    assert s.total_pick_count == 1


# ---------- 시나리오 (b): 두 번째 픽 ----------

@pytest.mark.asyncio
async def test_second_pick_repick_metadata(store: SectorStore):
    """같은 stock_code 두 번째 픽(다른 섹터): is_repick=1, prev_pick_id 정확, days_since=5, total=2."""
    r1 = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick("2025-04-28", offset_days=0))
    prev_ss_id = (await store.get_stocks_by_sector(r1.pick_id, "반도체"))[0].id

    r2 = await store.upsert_sector("AI", [_stock("AI", "005930", "삼성전자")], _pick("2025-05-03", offset_days=5))
    rows2 = await store.get_stocks_by_sector(r2.pick_id, "AI")
    s2 = rows2[0]

    assert s2.is_repick == 1
    assert s2.prev_pick_id == prev_ss_id
    assert s2.days_since_last_pick == 5
    assert s2.total_pick_count == 2


# ---------- 시나리오 (c): 세 번째 픽 ----------

@pytest.mark.asyncio
async def test_third_pick_total_count(store: SectorStore):
    """세 번째 픽: total_pick_count=3, prev_pick_id는 두 번째 픽의 ss.id."""
    await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick("2025-04-28", offset_days=0))
    r2 = await store.upsert_sector("AI", [_stock("AI", "005930", "삼성전자")], _pick("2025-05-05", offset_days=7))
    prev_ss_id_2 = (await store.get_stocks_by_sector(r2.pick_id, "AI"))[0].id

    r3 = await store.upsert_sector("반도체2", [_stock("반도체2", "005930", "삼성전자")], _pick("2025-05-12", offset_days=14))
    s3 = (await store.get_stocks_by_sector(r3.pick_id, "반도체2"))[0]

    assert s3.total_pick_count == 3
    assert s3.prev_pick_id == prev_ss_id_2
    assert s3.is_repick == 1


# ---------- 시나리오 (d): tracking_start_date ----------

@pytest.mark.asyncio
async def test_tracking_start_date_matches_created_at(store: SectorStore):
    """tracking_start_date == sector_picks.created_at (to_db_iso 기준)."""
    pick = _pick("2025-04-28")
    r = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], pick)
    s = (await store.get_stocks_by_sector(r.pick_id, "반도체"))[0]
    assert s.tracking_start_date == to_db_iso(pick.created_at)


# ---------- 시나리오 (e): tracking_end_date ----------

@pytest.mark.asyncio
async def test_tracking_end_date_is_d20(store: SectorStore):
    """tracking_end_date = 거래일 D+20."""
    pick = _pick("2025-04-28")
    r = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], pick)
    s = (await store.get_stocks_by_sector(r.pick_id, "반도체"))[0]
    expected = add_trading_days(pick.created_at.date(), 20).isoformat()
    assert s.tracking_end_date == expected


# ---------- 시나리오 (f): tracking_status ----------

@pytest.mark.asyncio
async def test_tracking_status_active(store: SectorStore):
    """tracking_status = 'active'."""
    r = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    s = (await store.get_stocks_by_sector(r.pick_id, "반도체"))[0]
    assert s.tracking_status == "active"


# ---------- 시나리오 (g): 같은 세션 내 중복 ----------

@pytest.mark.asyncio
async def test_same_session_duplicate_deduped(store: SectorStore):
    """같은 upsert_sector 호출에 동일 stock_code 두 번 → DB엔 1개, added_count=1."""
    stocks = [
        _stock("반도체", "005930", "삼성전자"),
        _stock("반도체", "005930", "삼성전자_중복"),
    ]
    r = await store.upsert_sector("반도체", stocks, _pick())
    rows = await store.get_stocks_by_sector(r.pick_id, "반도체")
    codes = [s.stock_code for s in rows]
    assert codes.count("005930") == 1
    assert r.added_count == 1


# ---------- 시나리오 (h): cross-sector 중복 보존 ----------

@pytest.mark.asyncio
async def test_cross_sector_both_preserved(store: SectorStore):
    """다른 섹터에서 같은 종목 → 둘 다 INSERT / 같은 섹터 내 중복 → 두 번째 skip."""
    # 다른 섹터: 반도체 005930, AI 005930 → 둘 다 저장
    r1 = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick("2025-04-28"))
    r2 = await store.upsert_sector("AI", [_stock("AI", "005930", "삼성전자")], _pick("2025-04-28"))

    rows_semicon = await store.get_stocks_by_sector(r1.pick_id, "반도체")
    rows_ai = await store.get_stocks_by_sector(r2.pick_id, "AI")
    assert len(rows_semicon) == 1
    assert len(rows_ai) == 1
    assert rows_ai[0].is_repick == 1
    assert rows_ai[0].prev_pick_id == rows_semicon[0].id

    # 같은 섹터 내 중복은 여전히 dedup
    r3 = await store.upsert_sector("반도체2", [
        _stock("반도체2", "006400", "삼성SDI"),
        _stock("반도체2", "006400", "삼성SDI_dup"),
    ], _pick("2025-04-28"))
    assert r3.added_count == 1


# ---------- 시나리오 (i): pick_date 기준 정렬 ----------

@pytest.mark.asyncio
async def test_repick_uses_pick_date_not_created_at(store: SectorStore):
    """pick_date 기준 정렬: created_at이 늦어도 pick_date가 이른 픽은 prev로 선택 안 됨."""
    # Pick A: pick_date 늦음(2025-05-10), created_at 이름
    # Pick B: pick_date 이름(2025-04-28), created_at 늦음
    # → created_at 기준이면 Pick B가 prev, pick_date 기준이면 Pick A가 prev
    early_created = datetime(2025, 4, 28, 9, 0, 0, tzinfo=_KST)
    late_created = datetime(2025, 4, 29, 9, 0, 0, tzinfo=_KST)

    pick_a = SectorPick(
        pick_date="2025-05-10",
        created_at=early_created,
        expires_at=early_created + timedelta(days=7),
    )
    pick_b = SectorPick(
        pick_date="2025-04-28",
        created_at=late_created,
        expires_at=late_created + timedelta(days=7),
    )

    id_a = await store.insert_pick(pick_a, [_stock("반도체", "005930", "삼성전자")])
    await store.insert_pick(pick_b, [_stock("AI", "005930", "삼성전자")])

    ss_a_id = (await store.get_stocks_by_pick(id_a))[0].id

    pick_c = SectorPick(
        pick_date="2025-05-15",
        created_at=datetime(2025, 4, 30, 9, 0, 0, tzinfo=_KST),
        expires_at=datetime(2025, 5, 7, 9, 0, 0, tzinfo=_KST),
    )
    r_c = await store.upsert_sector("반도체2", [_stock("반도체2", "005930", "삼성전자")], pick_c)
    s_c = (await store.get_stocks_by_sector(r_c.pick_id, "반도체2"))[0]

    assert s_c.is_repick == 1
    assert s_c.prev_pick_id == ss_a_id  # pick_date 기준: pick_a(2025-05-10)이 prev
    assert s_c.days_since_last_pick == 5  # 2025-05-15 - 2025-05-10 = 5일
