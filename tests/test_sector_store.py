"""SectorStore UPSERT / merge 단위 테스트.

in-memory SQLite 사용. 외부 의존 없음.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from data.sector_models import PickStatus, SectorPick, SectorStock
from data.sector_store import SectorStore


# ---------- fixture ----------

@pytest_asyncio.fixture
async def store():
    s = SectorStore(db_path=":memory:")
    await s.open()
    yield s
    await s.close()


# ---------- 헬퍼 ----------

def _pick(raw: str = "") -> SectorPick:
    return SectorPick.create("2026-04-22", raw_input=raw, expires_days=7)


def _stock(sector: str, code: str, name: str, order: int = 1) -> SectorStock:
    return SectorStock(pick_id=0, sector_name=sector, stock_code=code,
                       stock_name=name, added_order=order)


# ---------- UPSERT ----------

@pytest.mark.asyncio
async def test_upsert_same_sector_merges(store: SectorStore):
    """같은 sector_name 두 번 호출 → 한 pick_id에 병합."""
    stocks_1 = [_stock("반도체", "005930", "삼성전자"), _stock("반도체", "000660", "SK하이닉스")]
    r1 = await store.upsert_sector("반도체", stocks_1, _pick())

    stocks_2 = [_stock("반도체", "042700", "한미반도체")]
    r2 = await store.upsert_sector("반도체", stocks_2, _pick())

    assert r1.is_new_pick is True
    assert r2.is_new_pick is False
    assert r2.pick_id == r1.pick_id
    assert r2.added_count == 1
    assert r2.total_count == 3


@pytest.mark.asyncio
async def test_upsert_deduplicates_stocks(store: SectorStore):
    """중복 stock_code 재입력 시 skipped_stocks에 담기고 DB엔 1개만."""
    stocks_1 = [_stock("반도체", "005930", "삼성전자"), _stock("반도체", "000660", "SK하이닉스")]
    await store.upsert_sector("반도체", stocks_1, _pick())

    stocks_2 = [_stock("반도체", "000660", "SK하이닉스"), _stock("반도체", "042700", "한미반도체")]
    r2 = await store.upsert_sector("반도체", stocks_2, _pick())

    assert r2.added_count == 1
    assert r2.total_count == 3
    assert len(r2.skipped_stocks) == 1
    assert r2.skipped_stocks[0].stock_code == "000660"

    rows = await store.get_stocks_by_sector(r2.pick_id, "반도체")
    codes = [s.stock_code for s in rows]
    assert codes.count("000660") == 1


@pytest.mark.asyncio
async def test_upsert_different_sector_creates_new(store: SectorStore):
    """다른 sector_name → 각각 새 pick_id 생성."""
    r1 = await store.upsert_sector("반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    r2 = await store.upsert_sector("2차전지", [_stock("2차전지", "006400", "삼성SDI")], _pick())

    assert r1.is_new_pick is True
    assert r2.is_new_pick is True
    assert r1.pick_id != r2.pick_id


# ---------- merge ----------

@pytest.mark.asyncio
async def test_merge_duplicate_sectors(store: SectorStore):
    """insert_pick으로 강제 중복 생성 → merge 후 oldest pick에 통합."""
    # Pick 1: 중동재건 (종목 3개)
    pick_a = _pick("pick_a")
    stocks_a = [
        SectorStock(0, "중동재건", "001234", "종목A", 1),
        SectorStock(0, "중동재건", "002345", "종목B", 2),
        SectorStock(0, "중동재건", "003456", "종목C", 3),
    ]
    id_a = await store.insert_pick(pick_a, stocks_a)

    # Pick 2: 중동재건 (종목 2개, 종목B 중복 + 신규 종목D)
    pick_b = _pick("pick_b")
    stocks_b = [
        SectorStock(0, "중동재건", "002345", "종목B", 1),  # 중복
        SectorStock(0, "중동재건", "004567", "종목D", 2),
    ]
    id_b = await store.insert_pick(pick_b, stocks_b)

    assert id_a < id_b  # oldest = id_a

    results = await store.merge_duplicate_sectors()

    assert "중동재건" in results
    info = results["중동재건"]
    assert info["target_id"] == id_a
    assert id_b in info["merged_ids"]
    assert info["total_stocks"] == 4  # A, B, C, D (B 중복 제거)

    # oldest pick: active, merged pick: archived
    active = await store.get_active_picks()
    active_ids = [p.id for p in active]
    assert id_a in active_ids
    assert id_b not in active_ids

    # added_order가 섹터 스코프로 연속 (1~4)
    merged_stocks = await store.get_stocks_by_sector(id_a, "중동재건")
    orders = sorted(s.added_order for s in merged_stocks)
    assert orders == list(range(1, 5))


@pytest.mark.asyncio
async def test_find_duplicate_sectors(store: SectorStore):
    """중복 섹터만 반환, 단독 섹터 제외, pick_ids ASC 순 보장."""
    # 반도체: Pick 2개 (중복)
    t0 = datetime.now()
    pick_1 = SectorPick(pick_date="2026-04-22", created_at=t0,
                        expires_at=t0 + timedelta(days=7))
    pick_2 = SectorPick(pick_date="2026-04-22", created_at=t0 + timedelta(seconds=1),
                        expires_at=t0 + timedelta(days=7))
    id1 = await store.insert_pick(pick_1, [SectorStock(0, "반도체", "005930", "삼성전자", 1)])
    id2 = await store.insert_pick(pick_2, [SectorStock(0, "반도체", "000660", "SK하이닉스", 1)])

    # 2차전지: Pick 1개 (단독)
    pick_3 = SectorPick(pick_date="2026-04-22", created_at=t0 + timedelta(seconds=2),
                        expires_at=t0 + timedelta(days=7))
    await store.insert_pick(pick_3, [SectorStock(0, "2차전지", "006400", "삼성SDI", 1)])

    result = await store.find_duplicate_sectors()

    assert "2차전지" not in result
    assert "반도체" in result
    assert result["반도체"]["pick_ids"] == [id1, id2]  # ASC 순
    assert result["반도체"]["stock_counts"] == [1, 1]
