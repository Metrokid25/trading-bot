"""SectorStore UPSERT / merge / alert 단위 테스트.

in-memory SQLite 사용. 외부 의존 없음.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import aiosqlite

from data.sector_models import PickStatus, SectorPick, SectorStock
from data.sector_store import AlertResult, SectorStore, materialize_expired_picks


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


# ---------- append-only universe membership events ----------

@pytest.mark.asyncio
async def test_membership_events_follow_code_level_active_boundary(store: SectorStore):
    """같은 코드가 여러 섹터에 있으면 최초 활성/마지막 이탈만 기록한다."""
    await store.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    await store.upsert_sector(
        "반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    await store.remove_stock_from_sector("AI", "005930")
    await store.remove_stock_from_sector("반도체", "005930")

    rows = await (await store._db.execute(
        "SELECT action,stock_code,source FROM universe_membership_events "
        "ORDER BY id")).fetchall()
    assert rows == [
        ("activate", "005930", "stock_insert"),
        ("deactivate", "005930", "stock_delete"),
    ]


@pytest.mark.asyncio
async def test_membership_events_record_pick_archive(store: SectorStore):
    result = await store.upsert_sector(
        "AI", [_stock("AI", "000660", "SK하이닉스")], _pick())
    await store.archive_pick(result.pick_id)
    # 이미 inactive인 과거 행을 정리해도 거짓 deactivate를 추가하면 안 된다.
    await store._db.execute(
        "DELETE FROM sector_stocks WHERE pick_id=?", (result.pick_id,))

    rows = await (await store._db.execute(
        "SELECT action,source FROM universe_membership_events ORDER BY id")).fetchall()
    assert rows == [
        ("activate", "stock_insert"),
        ("deactivate", "pick_off"),
    ]


@pytest.mark.asyncio
async def test_membership_bootstrap_runs_after_legacy_column_migration(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sector_picks (id INTEGER PRIMARY KEY,pick_date TEXT,"
        "created_at TEXT,expires_at TEXT,status TEXT,raw_input TEXT)")
    con.execute(
        "CREATE TABLE sector_stocks (id INTEGER PRIMARY KEY,pick_id INTEGER,"
        "sector_name TEXT,stock_code TEXT,stock_name TEXT,added_order INTEGER)")
    now = datetime.now().astimezone()
    con.execute(
        "INSERT INTO sector_picks VALUES (1,'2026-07-24',?,?, 'active','')",
        (now.isoformat(), (now + timedelta(days=7)).isoformat()))
    con.execute(
        "INSERT INTO sector_stocks VALUES (1,1,'AI','005930','삼성전자',1)")
    con.commit()
    con.close()

    legacy = SectorStore(db)
    await legacy.open()
    try:
        rows = await (await legacy._db.execute(
            "SELECT action,stock_code,source FROM universe_membership_events")
        ).fetchall()
        assert rows == [("activate", "005930", "bootstrap")]
    finally:
        await legacy.close()


@pytest.mark.asyncio
async def test_membership_events_follow_expiry_shorten_and_extend(store: SectorStore):
    result = await store.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    future = (datetime.now().astimezone() + timedelta(days=7)).isoformat()

    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?", (past, result.pick_id))
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?", (future, result.pick_id))

    rows = await (await store._db.execute(
        "SELECT action,source FROM universe_membership_events ORDER BY id")).fetchall()
    assert rows == [
        ("activate", "stock_insert"),
        ("deactivate", "expiry_shortened"),
        ("activate", "expiry_extended"),
    ]


@pytest.mark.asyncio
async def test_materialize_natural_expiry_uses_expiry_time_and_no_duplicate(tmp_path):
    db = tmp_path / "expiry.db"
    s = SectorStore(db)
    await s.open()
    result = await s.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    activated_at = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    expired_at = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    await s._db.execute(
        "UPDATE universe_membership_events SET occurred_at=?",
        (activated_at,))
    # 자연 시간 경과를 재현하려고 expires_at UPDATE 감지 트리거만 잠시 제거한다.
    await s._db.execute("DROP TRIGGER trg_universe_pick_expiry_off")
    await s._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (expired_at, result.pick_id))
    await s.close()

    assert materialize_expired_picks(db) == 1
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT occurred_at,action,source FROM universe_membership_events "
            "ORDER BY id").fetchall()
        status = con.execute(
            "SELECT status FROM sector_picks WHERE id=?",
            (result.pick_id,)).fetchone()[0]
    finally:
        con.close()
    assert rows[0][1:] == ("activate", "stock_insert")
    assert rows[1] == (expired_at, "deactivate", "pick_expired")
    assert status == "expired"


@pytest.mark.asyncio
async def test_extending_elapsed_pick_backfills_inactive_gap(store: SectorStore):
    result = await store.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    activated_at = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    expired_at = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    future = (datetime.now().astimezone() + timedelta(days=7)).isoformat()
    await store._db.execute(
        "UPDATE universe_membership_events SET occurred_at=?",
        (activated_at,))
    await store._db.execute("DROP TRIGGER trg_universe_pick_expiry_off")
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (expired_at, result.pick_id))
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (future, result.pick_id))

    rows = await (await store._db.execute(
        "SELECT occurred_at,action,source FROM universe_membership_events "
        "ORDER BY id")).fetchall()
    assert rows[0][1:] == ("activate", "stock_insert")
    assert rows[1] == (
        expired_at, "deactivate", "expiry_elapsed_before_extension")
    assert rows[2][1:] == ("activate", "expiry_extended")


@pytest.mark.asyncio
async def test_multi_pick_natural_expiry_uses_last_code_expiry(tmp_path):
    db = tmp_path / "multi-expiry.db"
    s = SectorStore(db)
    await s.open()
    first = await s.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    second = await s.upsert_sector(
        "반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    activated_at = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
    earlier = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    later = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    await s._db.execute(
        "UPDATE universe_membership_events SET occurred_at=?",
        (activated_at,))
    await s._db.execute("DROP TRIGGER trg_universe_pick_expiry_off")
    await s._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (earlier, first.pick_id))
    await s._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (later, second.pick_id))
    await s.close()

    assert materialize_expired_picks(db) == 2
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT occurred_at,action,source FROM universe_membership_events "
            "ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows[0][1:] == ("activate", "stock_insert")
    assert rows[1] == (later, "deactivate", "pick_expired")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_multi_pick_elapsed_extension_uses_last_code_expiry(store: SectorStore):
    first = await store.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    second = await store.upsert_sector(
        "반도체", [_stock("반도체", "005930", "삼성전자")], _pick())
    activated_at = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
    earlier = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    later = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    future = (datetime.now().astimezone() + timedelta(days=7)).isoformat()
    await store._db.execute(
        "UPDATE universe_membership_events SET occurred_at=?",
        (activated_at,))
    await store._db.execute("DROP TRIGGER trg_universe_pick_expiry_off")
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (earlier, first.pick_id))
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (later, second.pick_id))
    await store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?",
        (future, first.pick_id))

    rows = await (await store._db.execute(
        "SELECT occurred_at,action,source FROM universe_membership_events "
        "ORDER BY id")).fetchall()
    assert rows[0][1:] == ("activate", "stock_insert")
    assert rows[1] == (
        later, "deactivate", "expiry_elapsed_before_extension")
    assert rows[2][1:] == ("activate", "expiry_extended")


@pytest.mark.asyncio
async def test_membership_events_cover_direct_stock_identity_and_pick_delete(
        store: SectorStore):
    first = await store.upsert_sector(
        "AI", [_stock("AI", "005930", "삼성전자")], _pick())
    second = await store.upsert_sector(
        "바이오", [_stock("바이오", "000660", "SK하이닉스")], _pick())
    await store._db.execute(
        "UPDATE sector_stocks SET stock_code='035420',stock_name='NAVER' "
        "WHERE pick_id=?", (first.pick_id,))
    await store._db.execute(
        "DELETE FROM sector_picks WHERE id=?", (second.pick_id,))

    rows = await (await store._db.execute(
        "SELECT action,stock_code,source FROM universe_membership_events "
        "ORDER BY id")).fetchall()
    assert rows == [
        ("activate", "005930", "stock_insert"),
        ("activate", "000660", "stock_insert"),
        ("activate", "035420", "stock_identity_on"),
        ("deactivate", "005930", "stock_identity_off"),
        ("deactivate", "000660", "pick_delete"),
    ]


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
async def test_upsert_sector_is_case_insensitive(store: SectorStore):
    """영문 대소문자만 다른 섹터명은 기존 표기를 유지하며 같은 Pick에 합친다."""
    r1 = await store.upsert_sector(
        "ai솔루션", [_stock("ai솔루션", "005930", "삼성전자")], _pick()
    )
    r2 = await store.upsert_sector(
        "  AI솔루션  ", [_stock("AI솔루션", "000660", "SK하이닉스")], _pick()
    )

    assert r2.is_new_pick is False
    assert r2.pick_id == r1.pick_id
    rows = await store.get_stocks_by_sector(r1.pick_id, "AI솔루션")
    assert {row.stock_code for row in rows} == {"005930", "000660"}
    assert {row.sector_name for row in rows} == {"ai솔루션"}


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


@pytest.mark.asyncio
async def test_consolidate_case_insensitive_sectors_preserves_historical_tracking(
    store: SectorStore,
):
    """활성 행은 복제하되 이전 종목·이벤트·추적 연결은 원래 Pick에 보존한다."""
    now = datetime.now().astimezone()
    older = SectorPick(
        pick_date="2026-07-01",
        created_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(days=30),
    )
    newer = SectorPick(
        pick_date="2026-07-01",
        created_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=30),
    )
    older_id = await store.insert_pick(older, [
        _stock("ai솔루션", "005930", "삼성전자", 1),
        _stock("ai솔루션", "000660", "SK하이닉스", 2),
    ])
    newer_id = await store.insert_pick(newer, [
        _stock("AI솔루션", "000660", "SK하이닉스", 1),
        _stock("AI솔루션", "042700", "한미반도체", 2),
    ])
    moved_before = (await store.get_stocks_by_sector(older_id, "AI솔루션"))[0]
    await store._db.execute(
        "CREATE TABLE sector_pick_events ("
        "event_id INTEGER PRIMARY KEY, pick_id INTEGER NOT NULL, sector_name TEXT NOT NULL)"
    )
    await store._db.execute(
        "CREATE TABLE pick_daily_tracking ("
        "id INTEGER PRIMARY KEY, stock_pick_id INTEGER NOT NULL, event_id INTEGER NOT NULL)"
    )
    await store._db.execute(
        "INSERT INTO sector_pick_events(event_id, pick_id, sector_name) VALUES (1, ?, ?)",
        (older_id, "ai솔루션"),
    )
    await store._db.execute(
        "INSERT INTO pick_daily_tracking(id, stock_pick_id, event_id) VALUES (1, ?, 1)",
        (moved_before.id,),
    )

    result = await store.consolidate_case_insensitive_sectors()

    assert result["AI솔루션"]["target_id"] == newer_id
    assert result["AI솔루션"]["merged_ids"] == [older_id]
    active_ids = {pick.id for pick in await store.get_active_picks()}
    assert newer_id in active_ids
    assert older_id not in active_ids
    rows = await store.get_stocks_by_sector(newer_id, "ai솔루션")
    assert {row.stock_code for row in rows} == {"005930", "000660", "042700"}
    assert {row.sector_name for row in rows} == {"AI솔루션"}
    moved_after = next(row for row in rows if row.stock_code == "005930")
    assert moved_after.id != moved_before.id
    old_row = await store._db.execute(
        "SELECT pick_id, sector_name FROM sector_stocks WHERE id = ?",
        (moved_before.id,),
    )
    assert await old_row.fetchone() == (older_id, "ai솔루션")
    linked = await store._db.execute(
        "SELECT COUNT(*) FROM pick_daily_tracking pdt "
        "JOIN sector_pick_events spe ON spe.event_id = pdt.event_id "
        "JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id "
        "AND ss.pick_id = spe.pick_id AND ss.sector_name = spe.sector_name"
    )
    assert (await linked.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_consolidate_normalizes_whitespace_within_one_pick(store: SectorStore):
    now = datetime.now().astimezone()
    pick = SectorPick(
        pick_date="2026-07-01",
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
    pick_id = await store.insert_pick(
        pick, [_stock("  AI   솔루션  ", "005930", "삼성전자")]
    )
    await store._db.execute(
        "CREATE TABLE sector_pick_events ("
        "event_id INTEGER PRIMARY KEY, pick_id INTEGER NOT NULL, sector_name TEXT NOT NULL)"
    )
    await store._db.execute(
        "INSERT INTO sector_pick_events(event_id, pick_id, sector_name) VALUES (1, ?, ?)",
        (pick_id, "  AI   솔루션  "),
    )

    result = await store.consolidate_case_insensitive_sectors()

    assert result["AI 솔루션"]["target_id"] == pick_id
    rows = await store.get_stocks_by_sector(pick_id, "ai 솔루션")
    assert [row.sector_name for row in rows] == ["AI 솔루션"]
    event = await store._db.execute(
        "SELECT sector_name FROM sector_pick_events WHERE event_id = 1"
    )
    assert (await event.fetchone())[0] == "AI 솔루션"


@pytest.mark.asyncio
async def test_consolidate_preserves_same_spelling_repick_picks(store: SectorStore):
    """표기가 같은 여러 Pick은 정상 재픽업 이력일 수 있으므로 합치지 않는다."""
    now = datetime.now().astimezone()
    first = SectorPick(
        pick_date="2026-07-01",
        created_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(days=30),
    )
    second = SectorPick(
        pick_date="2026-07-02",
        created_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=30),
    )
    first_id = await store.insert_pick(
        first, [_stock("AI솔루션", "005930", "삼성전자")]
    )
    second_id = await store.insert_pick(
        second, [_stock("AI솔루션", "000660", "SK하이닉스")]
    )

    assert await store.consolidate_case_insensitive_sectors() == {}
    active_ids = {pick.id for pick in await store.get_active_picks()}
    assert {first_id, second_id} <= active_ids


@pytest.mark.asyncio
async def test_consolidate_skips_case_variant_mixed_with_repick_history(
    store: SectorStore,
):
    """AI, AI 정상 재픽과 ai 변형이 섞이면 모호하므로 모두 보존한다."""
    now = datetime.now().astimezone()
    ids = []
    for minutes, name, code in [
        (3, "AI", "005930"),
        (2, "AI", "000660"),
        (1, "ai", "042700"),
    ]:
        pick = SectorPick(
            pick_date="2026-07-01",
            created_at=now - timedelta(minutes=minutes),
            expires_at=now + timedelta(days=30),
        )
        ids.append(await store.insert_pick(pick, [_stock(name, code, code)]))

    assert await store.consolidate_case_insensitive_sectors() == {}
    active_ids = {pick.id for pick in await store.get_active_picks()}
    assert set(ids) <= active_ids


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


# ---------- archive_sector / remove_stock ----------

@pytest.mark.asyncio
async def test_archive_sector(store: SectorStore):
    """archive_sector: 여러 Pick에 분산된 섹터 제거. 빈 Pick만 auto-archive."""
    # Pick A: [중동재건] 3종목 + [다른섹터] 2종목
    pick_a = _pick()
    stocks_a = [
        SectorStock(0, "중동재건", "001000", "종목A1", 1),
        SectorStock(0, "중동재건", "001001", "종목A2", 2),
        SectorStock(0, "중동재건", "001002", "종목A3", 3),
        SectorStock(0, "다른섹터", "002000", "종목B1", 4),
        SectorStock(0, "다른섹터", "002001", "종목B2", 5),
    ]
    id_a = await store.insert_pick(pick_a, stocks_a)

    # Pick B: [중동재건] 2종목만 (다른 섹터 없음)
    pick_b = _pick()
    stocks_b = [
        SectorStock(0, "중동재건", "001010", "종목C1", 1),
        SectorStock(0, "중동재건", "001011", "종목C2", 2),
    ]
    id_b = await store.insert_pick(pick_b, stocks_b)

    result = await store.archive_sector("중동재건")

    assert set(result["affected_picks"]) == {id_a, id_b}
    assert result["auto_archived_picks"] == [id_b]  # Pick B만 빈 Pick

    # Pick A: active 유지 (다른섹터 2종목 남음)
    active = await store.get_active_picks()
    active_ids = [p.id for p in active]
    assert id_a in active_ids
    assert id_b not in active_ids

    # 중동재건 종목 전부 삭제됨
    assert await store.get_stocks_by_sector(id_a, "중동재건") == []
    assert await store.get_stocks_by_sector(id_b, "중동재건") == []

    # 다른섹터 종목 보존됨
    other = await store.get_stocks_by_sector(id_a, "다른섹터")
    assert len(other) == 2


@pytest.mark.asyncio
async def test_archive_sector_preserves_other_sectors(store: SectorStore):
    """archive_sector: 같은 Pick의 다른 섹터 종목은 보존."""
    pick = _pick()
    stocks = [
        SectorStock(0, "A섹터", "003000", "A종목1", 1),
        SectorStock(0, "A섹터", "003001", "A종목2", 2),
        SectorStock(0, "B섹터", "004000", "B종목1", 3),
    ]
    pick_id = await store.insert_pick(pick, stocks)

    result = await store.archive_sector("A섹터")

    assert result["affected_picks"] == [pick_id]
    assert result["auto_archived_picks"] == []  # B섹터 남아서 Pick 유지

    # Pick active 상태 유지
    active = await store.get_active_picks()
    assert any(p.id == pick_id for p in active)

    # A섹터 종목 0개
    assert await store.get_stocks_by_sector(pick_id, "A섹터") == []

    # B섹터 종목 1개 유지
    b_stocks = await store.get_stocks_by_sector(pick_id, "B섹터")
    assert len(b_stocks) == 1
    assert b_stocks[0].stock_code == "004000"


@pytest.mark.asyncio
async def test_remove_stock_from_single_pick(store: SectorStore):
    """remove_stock_from_sector: 단일 Pick에서 종목 제거, Pick은 유지."""
    pick_id = await store.insert_pick(_pick(), [
        SectorStock(0, "반도체", "005930", "삼성전자", 1),
        SectorStock(0, "반도체", "000660", "SK하이닉스", 2),
    ])

    result = await store.remove_stock_from_sector("반도체", "005930")

    assert result["removed_from_picks"] == [pick_id]
    assert result["auto_archived_picks"] == []

    # Pick active 유지
    active = await store.get_active_picks()
    assert any(p.id == pick_id for p in active)

    # SK하이닉스만 남음
    remaining = await store.get_stocks_by_sector(pick_id, "반도체")
    assert len(remaining) == 1
    assert remaining[0].stock_code == "000660"


@pytest.mark.asyncio
async def test_remove_stock_auto_archives_empty_pick(store: SectorStore):
    """remove_stock_from_sector: 마지막 종목 제거 시 Pick 자동 archive."""
    pick_id = await store.insert_pick(_pick(), [
        SectorStock(0, "반도체", "005930", "삼성전자", 1),
    ])

    result = await store.remove_stock_from_sector("반도체", "005930")

    assert result["removed_from_picks"] == [pick_id]
    assert result["auto_archived_picks"] == [pick_id]

    # Pick archived 상태
    active = await store.get_active_picks()
    assert not any(p.id == pick_id for p in active)


@pytest.mark.asyncio
async def test_remove_stock_nonexistent(store: SectorStore):
    """remove_stock_from_sector: 없는 종목코드는 에러 없이 빈 결과 반환."""
    pick_id = await store.insert_pick(_pick(), [
        SectorStock(0, "반도체", "005930", "삼성전자", 1),
    ])

    result = await store.remove_stock_from_sector("반도체", "999999")

    assert result["removed_from_picks"] == []
    assert result["auto_archived_picks"] == []

    # 기존 데이터 그대로 유지
    remaining = await store.get_stocks_by_sector(pick_id, "반도체")
    assert len(remaining) == 1
    assert remaining[0].stock_code == "005930"


# ---------- try_insert_alert_with_cooldown: 원자 쿨다운 ----------
_ALERT_KWARGS = dict(
    sector_name="AI",
    stage=1,
    cooldown_min=5,
    passed_stocks=[],
    metrics={},
    threshold_used={},
)


@pytest.mark.asyncio
async def test_try_insert_first_call_returns_inserted(store: SectorStore):
    """이력 없을 때 첫 호출은 INSERTED, row_id는 양수."""
    now = datetime.now()
    result, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert result is AlertResult.INSERTED
    assert isinstance(row_id, int) and row_id > 0


@pytest.mark.asyncio
async def test_try_insert_second_call_within_cooldown_returns_active(store: SectorStore):
    """쿨다운 기간 내 두 번째 호출은 COOLDOWN_ACTIVE."""
    now = datetime.now()
    first, _ = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    second, row_id2 = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert first is AlertResult.INSERTED
    assert second is AlertResult.COOLDOWN_ACTIVE
    assert row_id2 is None


@pytest.mark.asyncio
async def test_try_insert_after_cooldown_returns_inserted(store: SectorStore):
    """쿨다운 기간이 지난 triggered_at으로 호출하면 다시 INSERTED."""
    past = datetime.now() - timedelta(minutes=10)
    recent = datetime.now()
    # 10분 전 기록 삽입 (cooldown=5분이므로 만료)
    first, _ = await store.try_insert_alert_with_cooldown(triggered_at=past, **_ALERT_KWARGS)
    # 현재 시각 기준으로는 쿨다운 경과 → INSERTED
    second, row_id2 = await store.try_insert_alert_with_cooldown(triggered_at=recent, **_ALERT_KWARGS)
    assert first is AlertResult.INSERTED
    assert second is AlertResult.INSERTED
    assert isinstance(row_id2, int) and row_id2 > 0


@pytest.mark.asyncio
async def test_try_insert_concurrent_calls_only_one_inserts(store: SectorStore):
    """asyncio.gather로 동일 sector/stage 동시 호출 → 1건 INSERTED, 1건 COOLDOWN_ACTIVE."""
    now = datetime.now()
    pairs = await asyncio.gather(
        store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS),
        store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS),
    )
    statuses = [r[0] for r in pairs]
    inserted = [s for s in statuses if s is AlertResult.INSERTED]
    skipped = [s for s in statuses if s is AlertResult.COOLDOWN_ACTIVE]
    assert len(inserted) == 1
    assert len(skipped) == 1


# ---------- try_insert: DB 잠금 재시도 ----------

@pytest.mark.asyncio
async def test_insert_retries_on_database_locked_succeeds_second_attempt(store: SectorStore):
    """첫 번째 시도에서 locked 오류 → 재시도 후 INSERTED."""
    now = datetime.now()
    call_count = 0
    original_execute = store._db.execute

    async def patched_execute(sql, params=None):
        nonlocal call_count
        if params is not None and "INSERT INTO alert_history" in sql:
            call_count += 1
            if call_count == 1:
                raise sqlite3.OperationalError("database is locked")
        if params is not None:
            return await original_execute(sql, params)
        return await original_execute(sql)

    store._db.execute = patched_execute
    result, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert result is AlertResult.INSERTED
    assert row_id is not None
    assert call_count == 2


@pytest.mark.asyncio
async def test_insert_retries_exhausted_returns_insert_failed(store: SectorStore):
    """모든 재시도 소진 시 INSERT_FAILED 반환."""
    now = datetime.now()

    async def always_locked(sql, params=None):
        if params is not None and "INSERT INTO alert_history" in sql:
            raise sqlite3.OperationalError("database is locked")
        if params is not None:
            return await store._db._execute(sql, params)
        return await store._db._execute(sql)

    store._db.execute = always_locked
    result, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert result is AlertResult.INSERT_FAILED
    assert row_id is None


# ---------- update_delivery_status ----------

@pytest.mark.asyncio
async def test_update_delivery_status_sent(store: SectorStore):
    """insert 후 delivery_status를 'sent'로 갱신."""
    now = datetime.now()
    _, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert row_id is not None
    await store.update_delivery_status(row_id, 'sent')
    cur = await store._db.execute(
        "SELECT delivery_status FROM alert_history WHERE id = ?", (row_id,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 'sent'


@pytest.mark.asyncio
async def test_update_delivery_status_disabled(store: SectorStore):
    """insert 후 delivery_status를 'disabled'로 갱신."""
    now = datetime.now()
    _, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    assert row_id is not None
    await store.update_delivery_status(row_id, 'disabled')
    cur = await store._db.execute(
        "SELECT delivery_status FROM alert_history WHERE id = ?", (row_id,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 'disabled'


@pytest.mark.asyncio
async def test_initial_status_pending_after_insert(store: SectorStore):
    """try_insert_alert_with_cooldown 직후 delivery_status는 'pending'."""
    now = datetime.now()
    _, row_id = await store.try_insert_alert_with_cooldown(triggered_at=now, **_ALERT_KWARGS)
    cur = await store._db.execute(
        "SELECT delivery_status FROM alert_history WHERE id = ?", (row_id,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 'pending'


# ---------- 마이그레이션 ----------

@pytest.mark.asyncio
async def test_migration_idempotent():
    """SectorStore를 두 번 열어도 오류 없음 (멱등 마이그레이션)."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        s = SectorStore(db_path=db_path)
        await s.open()
        await s.close()
        s2 = SectorStore(db_path=db_path)
        await s2.open()
        await s2.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_migration_backfills_existing_rows_as_sent():
    """구 스키마(delivery_status 없음) DB → 마이그레이션 후 기존 행은 'sent'."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """CREATE TABLE alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_name TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    triggered_at TEXT NOT NULL,
                    passed_stocks TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    threshold_used TEXT NOT NULL
                )"""
            )
            await db.execute(
                "INSERT INTO alert_history "
                "(sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used) "
                "VALUES ('반도체', 1, '2026-04-25T10:00:00+09:00', '[]', '{}', '{}')"
            )
            await db.commit()

        s = SectorStore(db_path=db_path)
        await s.open()
        try:
            cur = await s._db.execute(
                "SELECT delivery_status FROM alert_history WHERE sector_name='반도체'"
            )
            row = await cur.fetchone()
            assert row is not None, "마이그레이션 후 기존 행이 없음"
            assert row[0] == 'sent', f"기존 행 delivery_status={row[0]!r}, expected 'sent'"
        finally:
            await s.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_migration_preserves_existing_data():
    """마이그레이션 후 기존 컬럼 데이터가 보존됨."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """CREATE TABLE alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_name TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    triggered_at TEXT NOT NULL,
                    passed_stocks TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    threshold_used TEXT NOT NULL
                )"""
            )
            await db.execute(
                "INSERT INTO alert_history "
                "(sector_name, stage, triggered_at, passed_stocks, metrics, threshold_used) "
                "VALUES ('AI', 2, '2026-04-25T11:00:00+09:00', '[{\"code\":\"000001\"}]', '{\"x\":1}', '{\"t\":2}')"
            )
            await db.commit()

        s = SectorStore(db_path=db_path)
        await s.open()
        try:
            cur = await s._db.execute(
                "SELECT sector_name, stage, passed_stocks, delivery_status FROM alert_history"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == 'AI'
            assert row[1] == 2
            assert row[2] == '[{"code":"000001"}]'
            assert row[3] == 'sent'
        finally:
            await s.close()
    finally:
        os.unlink(db_path)
