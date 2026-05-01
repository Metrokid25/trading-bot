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
    """같은 날 두 번 픽: pick_date < ? 조건으로 동일 날짜 prev 미참조 → is_repick=0, days=NULL.
    total_count는 MAX 기반 누적이므로 2."""
    id1 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    id2 = await store._record_sector_pick_event(
        "반도체", _ts("2026-04-21"), date(2026, 4, 21)
    )
    rows = await _get_events(store, "반도체")
    r2 = rows[1]
    assert r2[3] == 0                # is_sector_repick: 동일 날짜 prev 미참조
    assert r2[4] is None             # prev_event_id
    assert r2[5] is None             # days_since_last_sector_pick
    assert r2[6] is None             # trading_days_since_last_sector_pick
    assert r2[7] == 2                # total_sector_pick_count (MAX 누적)


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


# ---------- TC-Integration1: upsert_sector 경로 end-to-end 통합 ----------

@pytest.mark.asyncio
async def test_integration_upsert_two_picks_repick_marked(store: SectorStore):
    """upsert_sector(record_pick_event=True) 두 번 → sector_pick_events 2행, 두 번째 재픽업."""
    pick1 = _pick("2026-04-21")
    pick2 = _pick("2026-04-28", offset_hours=1)

    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        pick1,
        record_pick_event=True,
    )
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        pick2,
        record_pick_event=True,
    )

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2

    r1, r2 = rows
    assert r1[3] == 0               # is_sector_repick: 첫 픽
    assert r1[8] == "2026-04-21"    # pick_date

    assert r2[3] == 1               # is_sector_repick: 재픽업
    assert r2[4] == r1[0]           # prev_event_id → 첫 번째 event_id
    assert r2[5] == 7               # days_since (4/21 → 4/28)
    assert r2[6] == 5               # trading_days_since (월→월 한 주)
    assert r2[7] == 2               # total_sector_pick_count
    assert r2[8] == "2026-04-28"    # pick_date


# ---------- TC-Integration2: record_pick_event=False → sector_pick_events 0행 ----------

@pytest.mark.asyncio
async def test_integration_no_event_record_pick_event_false(store: SectorStore):
    """record_pick_event=False(기본값): upsert_sector 경로로 호출해도 sector_pick_events 0행."""
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        _pick("2026-04-21"),
        record_pick_event=False,
    )
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        _pick("2026-04-28", offset_hours=1),
        record_pick_event=False,
    )
    rows = await _get_events(store)
    assert len(rows) == 0


# ---------- TC-Integration3: 이벤트 기록 실패해도 픽 저장은 유지 (H1 격리 검증) ----------

@pytest.mark.asyncio
async def test_integration_event_failure_does_not_rollback_pick(store: SectorStore):
    """sector_pick_events 테이블 없으면 이벤트 기록 실패하지만 픽 저장은 유지."""
    from loguru import logger as _logger
    await store._db.execute("DROP TABLE IF EXISTS sector_pick_events")

    warning_messages: list[str] = []
    sink_id = _logger.add(
        lambda msg: warning_messages.append(msg.record["message"]),
        level="WARNING",
        format="{message}",
    )
    try:
        result = await store.upsert_sector(
            "반도체",
            [_stock("반도체", "005930", "삼성전자")],
            _pick("2026-04-21"),
            record_pick_event=True,
        )
    finally:
        _logger.remove(sink_id)

    # 픽은 저장됨 (예외 전파 없음)
    assert result.pick_id is not None
    cur = await store._db.execute("SELECT COUNT(*) FROM sector_picks")
    assert (await cur.fetchone())[0] == 1

    # warning 로그 발생 확인 (loguru sink 직접 캡처)
    assert any("sector_pick_event 기록 실패" in m for m in warning_messages)


# ---------- TC-Integration4: 별도 트랜잭션 분리 후 정상 케이스 회귀 ----------

@pytest.mark.asyncio
async def test_integration_separate_tx_two_picks_repick_marked(store: SectorStore):
    """best-effort 분리 후에도 두 번 호출 시 sector_pick_events 2행 + 재픽업 마킹 정상."""
    pick1 = _pick("2026-04-21")
    pick2 = _pick("2026-04-28", offset_hours=1)

    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        pick1,
        record_pick_event=True,
    )
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "000660", "SK하이닉스")],
        pick2,
        record_pick_event=True,
    )

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r1, r2 = rows
    assert r1[3] == 0               # is_sector_repick: 첫 픽
    assert r1[8] == "2026-04-21"
    assert r2[3] == 1               # is_sector_repick: 재픽업
    assert r2[4] == r1[0]           # prev_event_id 연결
    assert r2[5] == 7               # days_since
    assert r2[6] == 5               # trading_days_since
    assert r2[8] == "2026-04-28"


# ---------- TC-Integration5: pick_date 형식 오류 시 픽 저장은 유지 (M3 부분 해소) ----------

@pytest.mark.asyncio
async def test_integration_bad_pick_date_format_does_not_rollback_pick(store: SectorStore):
    """pick_date 형식 오류('2026/04/29')로 fromisoformat 실패해도 픽 저장은 유지."""
    from loguru import logger as _logger
    kst = ZoneInfo("Asia/Seoul")
    bad_pick = SectorPick(
        pick_date="2026/04/29",  # 잘못된 형식 — date.fromisoformat() 실패 유도
        created_at=datetime(2026, 4, 29, 9, 0, 0, tzinfo=kst),
        expires_at=datetime(2026, 4, 29, 9, 0, 0, tzinfo=kst) + timedelta(days=7),
    )

    warning_messages: list[str] = []
    sink_id = _logger.add(
        lambda msg: warning_messages.append(msg.record["message"]),
        level="WARNING",
        format="{message}",
    )
    try:
        result = await store.upsert_sector(
            "반도체",
            [_stock("반도체", "005930", "삼성전자")],
            bad_pick,
            record_pick_event=True,
        )
    finally:
        _logger.remove(sink_id)

    # 픽은 저장됨 (예외 전파 없음)
    assert result.pick_id is not None
    cur = await store._db.execute("SELECT COUNT(*) FROM sector_picks")
    assert (await cur.fetchone())[0] == 1

    # warning 로그 발생 확인 (loguru sink 직접 캡처)
    assert any("sector_pick_event 기록 실패" in m for m in warning_messages)

    # sector_pick_events는 0행
    rows = await _get_events(store)
    assert len(rows) == 0


# ---------- TC-Integration6: H2 백데이팅 픽 — days_since 음수 방지 ----------

@pytest.mark.asyncio
async def test_integration6_backdating_no_negative_days(store: SectorStore):
    """H2: 미래 pick_date 행이 prev로 잡혀 days_since 음수가 되지 않는다."""
    # 먼저 pick_date=2026-04-30 이벤트 기록
    await store._record_sector_pick_event(
        "반도체", _ts("2026-04-30"), date(2026, 4, 30)
    )

    # 이후 백데이팅 픽: pick_date=2026-04-25 (이미 등록된 4/30보다 과거)
    await store._record_sector_pick_event(
        "반도체", _ts("2026-05-01"), date(2026, 4, 25)
    )

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]  # event_id 순서 기준 두 번째 = 백데이팅 픽
    # days_since는 NULL이거나 양수 — 절대 음수 아님
    assert r2[5] is None or r2[5] >= 0


# ---------- TC-Integration7: H3 NULL 행 제외 + total_count 누적 확인 ----------

@pytest.mark.asyncio
async def test_integration7_null_pick_date_excluded_and_count_accumulated(store: SectorStore):
    """H3: pick_date=NULL 행은 prev 조회에서 제외, days_since=NULL.
    total_count는 NULL 행 포함 MAX+1로 누적되어야 한다."""
    # 마이그레이션 이전 데이터 시뮬레이션: pick_date=NULL 행 직접 INSERT
    await store._db.execute(
        "INSERT INTO sector_pick_events "
        "(sector_name, registered_at_kst, is_sector_repick, total_sector_pick_count) "
        "VALUES (?, ?, ?, ?)",
        ("반도체", _ts("2026-04-01"), 0, 1),
    )

    # 정상 픽 등록 (upsert_sector 경로로 통합 검증)
    await store.upsert_sector(
        "반도체",
        [_stock("반도체", "005930", "삼성전자")],
        _pick("2026-04-21"),
        record_pick_event=True,
    )

    rows = await _get_events(store, "반도체")
    assert len(rows) == 2
    r2 = rows[1]
    assert r2[3] == 0      # is_sector_repick = 0 (NULL 행이 prev로 잡히지 않음)
    assert r2[4] is None   # prev_event_id = None
    assert r2[5] is None   # days_since = None
    assert r2[7] == 2      # total_sector_pick_count: NULL 행(count=1) 포함 MAX+1
