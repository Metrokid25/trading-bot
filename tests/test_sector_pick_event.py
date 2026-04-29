"""_record_sector_pick_event 헬퍼 + upsert_sector record_pick_event 단위 테스트.

in-memory SQLite 사용. 외부 의존 없음.
sector_pick_events 테이블은 migration_runner가 운영 DB에 생성하므로
테스트 fixture에서 직접 CREATE TABLE.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore

_KST = ZoneInfo("Asia/Seoul")

_SPE_DDL = """
CREATE TABLE IF NOT EXISTS sector_pick_events (
    event_id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_name                         TEXT NOT NULL,
    registered_at_kst                   TEXT NOT NULL,
    is_sector_repick                    INTEGER DEFAULT 0,
    prev_event_id                       INTEGER,
    days_since_last_sector_pick         INTEGER,
    total_sector_pick_count             INTEGER DEFAULT 1,
    trading_days_since_last_sector_pick INTEGER,
    pick_date                           TEXT
)
"""


# ---------- fixture ----------

@pytest_asyncio.fixture
async def store():
    s = SectorStore(db_path=":memory:")
    await s.open()
    await s._db.execute(_SPE_DDL)
    yield s
    await s.close()


# ---------- 헬퍼 ----------

def _ts(date_str: str) -> str:
    """YYYY-MM-DD → KST ISO 타임스탬프 문자열."""
    return f"{date_str}T09:00:00+09:00"


def _pick(pick_date: str = "2026-04-21", offset_hours: int = 0) -> SectorPick:
    base = datetime(2026, 4, 21, 9, 0, 0, tzinfo=_KST) + timedelta(hours=offset_hours)
    return SectorPick(
        pick_date=pick_date,
        created_at=base,
        expires_at=base + timedelta(days=7),
    )


def _stock(sector: str, code: str, name: str) -> SectorStock:
    return SectorStock(pick_id=0, sector_name=sector, stock_code=code,
                       stock_name=name, added_order=1)


async def _get_events(store: SectorStore, sector_name: str | None = None) -> list:
    if sector_name:
        cur = await store._db.execute(
            "SELECT event_id, sector_name, registered_at_kst, is_sector_repick, "
            "prev_event_id, days_since_last_sector_pick, "
            "trading_days_since_last_sector_pick, total_sector_pick_count, pick_date "
            "FROM sector_pick_events WHERE sector_name = ? ORDER BY event_id",
            (sector_name,),
        )
    else:
        cur = await store._db.execute(
            "SELECT event_id, sector_name, registered_at_kst, is_sector_repick, "
            "prev_event_id, days_since_last_sector_pick, "
            "trading_days_since_last_sector_pick, total_sector_pick_count, pick_date "
            "FROM sector_pick_events ORDER BY event_id"
        )
    return await cur.fetchall()


# ---------- TC1: 첫 픽 ----------

@pytest.mark.asyncio
async def test_first_pick_no_repick(store: SectorStore):
    """첫 픽: is_sector_repick=0, prev/days NULL, total_count=1."""
    event_id = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    rows = await _get_events(store, "반도체")
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == event_id          # event_id 반환값 일치
    assert r[3] == 0                 # is_sector_repick
    assert r[4] is None              # prev_event_id
    assert r[5] is None              # days_since_last_sector_pick
    assert r[6] is None              # trading_days_since_last_sector_pick
    assert r[7] == 1                 # total_sector_pick_count
    assert r[8] == "2026-04-21"      # pick_date


# ---------- TC2: 재픽업 (금→월, 자연일 3, 거래일 1) ----------

@pytest.mark.asyncio
async def test_repick_friday_to_monday(store: SectorStore):
    """금(2026-04-24) → 월(2026-04-27): 자연일 3, 거래일 1."""
    id1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-24"), date(2026, 4, 24)
    )
    id2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-27"), date(2026, 4, 27)
    )
    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]
    assert r2[3] == 1                # is_sector_repick
    assert r2[4] == id1              # prev_event_id
    assert r2[5] == 3                # days_since_last_sector_pick (자연일)
    assert r2[6] == 1                # trading_days_since_last_sector_pick
    assert r2[7] == 2                # total_sector_pick_count
    assert r2[8] == "2026-04-27"     # pick_date


# ---------- TC3: 재픽업 (자연일 7, 거래일 5 — 한 주 후) ----------

@pytest.mark.asyncio
async def test_repick_one_full_week(store: SectorStore):
    """월(2026-04-21) → 월(2026-04-28): 자연일 7, 거래일 5."""
    id1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    id2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-28"), date(2026, 4, 28)
    )
    rows = await _get_events(store, "반도체")
    r2 = rows[1]
    assert r2[3] == 1                # is_sector_repick
    assert r2[4] == id1              # prev_event_id
    assert r2[5] == 7                # days_since_last_sector_pick
    assert r2[6] == 5                # trading_days_since_last_sector_pick
    assert r2[7] == 2                # total_sector_pick_count


# ---------- TC4: 재픽업 (같은 날 두 번 픽) ----------

@pytest.mark.asyncio
async def test_repick_same_day(store: SectorStore):
    """같은 날 두 번 픽: days=0, trading_days=0."""
    id1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    id2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    rows = await _get_events(store, "반도체")
    r2 = rows[1]
    assert r2[3] == 1                # is_sector_repick
    assert r2[4] == id1              # prev_event_id
    assert r2[5] == 0                # days_since_last_sector_pick
    assert r2[6] == 0                # trading_days_since_last_sector_pick
    assert r2[7] == 2                # total_sector_pick_count


# ---------- TC5: 다른 섹터 이벤트가 끼어 있어도 자기 섹터만 참조 ----------

@pytest.mark.asyncio
async def test_cross_sector_isolation(store: SectorStore):
    """'AI' 이벤트가 중간에 삽입돼도 '반도체' 두 번째 픽은 '반도체' 직전 이벤트를 참조한다."""
    id_semi_1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    _id_ai = await store._record_sector_pick_event(
        "AI", _ts("2026-04-22"), date(2026, 4, 22)
    )
    id_semi_2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-28"), date(2026, 4, 28)
    )

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]
    assert r2[4] == id_semi_1        # AI 이벤트 아닌 반도체 직전 이벤트
    assert r2[5] == 7                # Apr 21 → Apr 28 자연일
    assert r2[6] == 5                # 거래일 5


# ---------- TC6: total_sector_pick_count 누적 ----------

@pytest.mark.asyncio
async def test_total_count_increments(store: SectorStore):
    """3번 픽 후 total_sector_pick_count = 3."""
    await store._record_sector_pick_event("반도체", _ts("2026-04-21"), date(2026, 4, 21))
    await store._record_sector_pick_event("반도체", _ts("2026-04-22"), date(2026, 4, 22))
    await store._record_sector_pick_event("반도체", _ts("2026-04-23"), date(2026, 4, 23))
    rows = await _get_events(store, "반도체")
    assert len(rows) == 3
    assert rows[0][7] == 1
    assert rows[1][7] == 2
    assert rows[2][7] == 3


# ---------- TC7: upsert_sector(record_pick_event=False) → 행 추가 없음 ----------

@pytest.mark.asyncio
async def test_upsert_no_event_when_flag_false(store: SectorStore):
    """record_pick_event=False(기본값): sector_pick_events에 행 추가 없음 (기존 동작 보존)."""
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        _pick(),
        record_pick_event=False,
    )
    rows = await _get_events(store)
    assert len(rows) == 0


# ---------- TC8: upsert_sector(record_pick_event=True) → 행 1개 추가 ----------

@pytest.mark.asyncio
async def test_upsert_records_event_when_flag_true(store: SectorStore):
    """record_pick_event=True: sector_pick_events에 정확히 1행 추가."""
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        _pick(),
        record_pick_event=True,
    )
    rows = await _get_events(store)
    assert len(rows) == 1
    r = rows[0]
    assert r[1] == "반도체"          # sector_name
    assert r[3] == 0                 # is_sector_repick (첫 픽)
    assert r[7] == 1                 # total_sector_pick_count
    assert r[8] == "2026-04-21"      # pick_date = pick_template.pick_date


# ---------- TC9: 직전 이벤트의 pick_date가 NULL이면 첫 픽으로 처리 ----------

@pytest.mark.asyncio
async def test_null_prev_pick_date_treated_as_first(store: SectorStore):
    """마이그레이션 전 기존 행(pick_date=NULL)이 있을 때: 첫 픽처럼 처리, total_count만 누적."""
    await store._db.execute(
        "INSERT INTO sector_pick_events "
        "(sector_name, registered_at_kst, is_sector_repick, total_sector_pick_count) "
        "VALUES (?, ?, ?, ?)",
        ("반도체", _ts("2026-04-20"), 0, 1),
    )

    await store._record_sector_pick_event("반도체", _ts("2026-04-21"), date(2026, 4, 21))

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]
    assert r2[3] == 0                # is_sector_repick = 0 (NULL prev → 첫 픽 처리)
    assert r2[4] is None             # prev_event_id
    assert r2[5] is None             # days_since = None
    assert r2[6] is None             # trading_days_since = None
    assert r2[7] == 2                # total_sector_pick_count = 1 + 1
    assert r2[8] == "2026-04-21"     # pick_date 저장 확인


# ---------- TC10: 백데이팅 픽 — gap이 registered_at_kst가 아닌 pick_date 기준 ----------

@pytest.mark.asyncio
async def test_backdated_pick_gap_uses_pick_date(store: SectorStore):
    """pick_date=2026-04-15(백데이팅) → pick_date=2026-04-29: 자연일 14, 거래일 10."""
    id1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-29"), date(2026, 4, 15)
    )
    id2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-29"), date(2026, 4, 29)
    )
    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]
    assert r2[3] == 1                # is_sector_repick
    assert r2[4] == id1              # prev_event_id
    assert r2[5] == 14               # days_since (2026-04-29 − 2026-04-15)
    assert r2[6] == 10               # trading_days_since (화 4/15 → 화 4/29)
    assert r2[7] == 2                # total_sector_pick_count
    assert r2[8] == "2026-04-29"     # pick_date
