from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.breakout_marker import EARLY_BREAKOUT
from core.sector_strength import (
    SectorCandidate,
    SectorStrengthConfig,
    SectorStrengthRanker,
    format_sector_selection,
)
from scripts.migrations import m007_phase25_minute_raw_rebuild as m007
from scripts.migrations import m008_phase25_minute_agg as m008
from scripts.migrations import m009_phase25_breakout_marks as m009


BASE_DDL = [
    """
    CREATE TABLE sector_picks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_date  TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        expires_at TEXT    NOT NULL,
        status     TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE sector_stocks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id         INTEGER NOT NULL REFERENCES sector_picks(id),
        sector_name     TEXT    NOT NULL,
        stock_code      TEXT    NOT NULL,
        stock_name      TEXT    NOT NULL,
        added_order     INTEGER NOT NULL,
        tracking_status TEXT    NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE sector_pick_events (
        event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_id           INTEGER NOT NULL REFERENCES sector_picks(id),
        sector_name       TEXT    NOT NULL,
        registered_at_kst TEXT    NOT NULL,
        pick_date         TEXT
    )
    """,
    """
    CREATE TABLE pick_daily_tracking (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_pick_id INTEGER NOT NULL REFERENCES sector_stocks(id),
        trading_day   TEXT    NOT NULL,
        day_offset    INTEGER NOT NULL,
        created_at    TEXT    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending',
        retry_count   INTEGER NOT NULL DEFAULT 0,
        event_id      INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
        UNIQUE(event_id, stock_pick_id, trading_day)
    )
    """,
]

LEGACY_MINUTE_DDL = """
CREATE TABLE pick_minute_raw (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_pick_id      INTEGER NOT NULL REFERENCES sector_stocks(id),
    trading_day        TEXT    NOT NULL,
    bar_time           TEXT    NOT NULL,
    minute_idx         INTEGER NOT NULL,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    volume             INTEGER,
    transaction_amount INTEGER,
    created_at         TEXT    NOT NULL,
    UNIQUE(stock_pick_id, trading_day, minute_idx)
)
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "sector.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    for stmt in BASE_DDL:
        conn.execute(stmt)
    conn.execute(LEGACY_MINUTE_DDL)
    m007.up(conn)
    m008.up(conn)
    m009.up(conn)
    conn.commit()
    conn.close()
    return path


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_tracking(
    conn: sqlite3.Connection,
    *,
    stock_code: str,
    sector_name: str,
    trading_day: str = "2026-05-06",
) -> tuple[int, int, int]:
    cur = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES ('2026-05-06', '2026-05-06T09:00:00', '2026-05-30T09:00:00', 'active')"
    )
    pick_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO sector_stocks"
        " (pick_id, sector_name, stock_code, stock_name, added_order, tracking_status)"
        " VALUES (?, ?, ?, ?, 1, 'active')",
        (pick_id, sector_name, stock_code, stock_code),
    )
    stock_pick_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, ?, '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id, sector_name),
    )
    event_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO pick_daily_tracking"
        " (stock_pick_id, trading_day, day_offset, created_at, status, event_id)"
        " VALUES (?, ?, 0, '2026-05-06T09:00:00', 'pending', ?)",
        (stock_pick_id, trading_day, event_id),
    )
    daily_tracking_id = int(cur.lastrowid)
    return daily_tracking_id, event_id, stock_pick_id


def _insert_agg(
    conn: sqlite3.Connection,
    daily_id: int,
    event_id: int,
    stock_pick_id: int,
    stock_code: str,
    *,
    trading_day: str = "2026-05-06",
    hhmm: str = "09:21",
    low: float = 1000,
    close: float = 1005,
    value: int = 200_000_000,
) -> int:
    bucket_start = f"{trading_day}T{hhmm}:00"
    cur = conn.execute(
        "INSERT INTO pick_minute_agg"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, bucket_start, bucket_end,"
        "  open, high, low, close, volume, value, raw_count, expected_count,"
        "  is_complete, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, 3, ?, ?, ?, ?, ?, ?, 100, ?, 3, 3, 1, 'RAW_1M',"
        " '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_id, event_id, stock_pick_id, stock_code, trading_day,
            bucket_start, f"{trading_day}T{hhmm}:59",
            close - 1.0, close + 1.0, low, close, value,
        ),
    )
    return int(cur.lastrowid)


def _insert_breakout_mark(
    conn: sqlite3.Connection,
    daily_id: int,
    event_id: int,
    stock_pick_id: int,
    agg_id: int,
    stock_code: str,
    *,
    trading_day: str = "2026-05-06",
    day_open_change_rate: float | None = 4.0,
    value: int | None = 600_000_000,
) -> None:
    conn.execute(
        "INSERT INTO pick_breakout_marks"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, agg_id, bucket_start, bucket_end,"
        "  breakout_type, day_open_change_rate, value,"
        "  threshold_prev_change_rate, threshold_day_open_change_rate,"
        "  threshold_value, threshold_value_ratio, rule_version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, 3, ?, '2026-05-06T08:30:00', '2026-05-06T08:32:00',"
        " ?, ?, ?, 1.5, 3.0, 500000000, 3.0, 'phase25_breakout_v1',"
        " '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_id, event_id, stock_pick_id, stock_code, trading_day,
            agg_id, EARLY_BREAKOUT, day_open_change_rate, value,
        ),
    )


# -------------------- rank() 순수 로직 --------------------
def _cand(
    sector: str,
    code: str,
    strength: float | None,
    *,
    value: int | None = 1000,
    did: int = 1,
) -> SectorCandidate:
    return SectorCandidate(
        daily_tracking_id=did, event_id=1, stock_pick_id=1,
        stock_code=code, sector_name=sector, trading_day="2026-05-06",
        strength_score=strength, value=value,
    )


def test_rank_filters_sectors_below_min_candidates():
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", 5.0),
        _cand("반도체", "000660", 3.0),
        _cand("2차전지", "373220", 9.0),  # 단독 → 필터됨
    ]
    selections = ranker.rank(candidates, SectorStrengthConfig(min_sector_candidates=2))
    assert [s.sector_name for s in selections] == ["반도체"]
    assert selections[0].candidate_count == 2


def test_rank_selects_highest_strength_as_best():
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", 3.0),
        _cand("반도체", "000660", 7.0),
        _cand("반도체", "042700", 5.0),
    ]
    selections = ranker.rank(candidates)
    assert selections[0].best.stock_code == "000660"
    # 강한 순으로 candidates 정렬.
    assert [c.stock_code for c in selections[0].candidates] == ["000660", "042700", "005930"]


def test_rank_tie_break_by_value():
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", 5.0, value=1_000),
        _cand("반도체", "000660", 5.0, value=9_000),  # 동점 → 거래대금 우위
    ]
    selections = ranker.rank(candidates)
    assert selections[0].best.stock_code == "000660"


def test_rank_none_strength_sorts_last():
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", None),
        _cand("반도체", "000660", 1.0),
    ]
    selections = ranker.rank(candidates)
    assert selections[0].best.stock_code == "000660"


def test_rank_sorts_sectors_by_best_strength():
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", 4.0),
        _cand("반도체", "000660", 2.0),
        _cand("2차전지", "373220", 8.0),
        _cand("2차전지", "006400", 1.0),
    ]
    selections = ranker.rank(candidates)
    # 2차전지 최강(8.0) > 반도체 최강(4.0).
    assert [s.sector_name for s in selections] == ["2차전지", "반도체"]


def test_rank_all_none_strength_falls_back_to_value():
    """섹터 멤버 강도가 전부 None이면 거래대금(value)으로 최강을 가른다."""
    ranker = SectorStrengthRanker(":memory:")
    candidates = [
        _cand("반도체", "005930", None, value=1_000),
        _cand("반도체", "000660", None, value=5_000),
    ]
    selections = ranker.rank(candidates)
    assert len(selections) == 1
    assert selections[0].best.stock_code == "000660"


def test_rank_empty_returns_empty():
    assert SectorStrengthRanker(":memory:").rank([]) == []


# -------------------- _load_candidates / select_for_day DB --------------------
@pytest.mark.asyncio
async def test_load_candidates_reads_strength_from_breakout_marks(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, pick_id = _seed_tracking(
        conn, stock_code="005930", sector_name="반도체"
    )
    agg_id = _insert_agg(conn, daily_id, event_id, pick_id, "005930", hhmm="09:21")
    _insert_breakout_mark(conn, daily_id, event_id, pick_id, agg_id, "005930",
                          day_open_change_rate=6.5, value=700_000_000)
    conn.commit()
    conn.close()

    ranker = SectorStrengthRanker(db_path)
    candidates = await ranker._load_candidates("2026-05-06", [daily_id])

    assert len(candidates) == 1
    assert candidates[0].sector_name == "반도체"
    assert candidates[0].strength_score == 6.5
    assert candidates[0].value == 700_000_000


@pytest.mark.asyncio
async def test_load_candidates_excludes_id_without_breakout_mark(db_path: str):
    """강세마크 없는 daily_tracking_id는 INNER JOIN 강세 게이트로 제외된다."""
    conn = _connect(db_path)
    daily_id, event_id, pick_id = _seed_tracking(
        conn, stock_code="005930", sector_name="반도체"
    )
    _insert_agg(conn, daily_id, event_id, pick_id, "005930", hhmm="09:21")
    # 강세마크를 넣지 않는다.
    conn.commit()
    conn.close()

    ranker = SectorStrengthRanker(db_path)
    candidates = await ranker._load_candidates("2026-05-06", [daily_id])
    assert candidates == []


@pytest.mark.asyncio
async def test_select_for_day_full_pipeline_picks_sector_best(db_path: str):
    conn = _connect(db_path)
    # 반도체 섹터 2종목: 둘 다 강세마크 + 눌림목 통과, 강도 A>B.
    a_daily, a_event, a_pick = _seed_tracking(conn, stock_code="005930", sector_name="반도체")
    a_agg = _insert_agg(conn, a_daily, a_event, a_pick, "005930", hhmm="09:21",
                        low=1000, close=1005)
    _insert_agg(conn, a_daily, a_event, a_pick, "005930", hhmm="09:24",
                low=1001, close=1010)
    _insert_breakout_mark(conn, a_daily, a_event, a_pick, a_agg, "005930",
                          day_open_change_rate=7.0)

    b_daily, b_event, b_pick = _seed_tracking(conn, stock_code="000660", sector_name="반도체")
    b_agg = _insert_agg(conn, b_daily, b_event, b_pick, "000660", hhmm="09:21",
                        low=2000, close=2005)
    _insert_agg(conn, b_daily, b_event, b_pick, "000660", hhmm="09:24",
                low=2001, close=2010)
    _insert_breakout_mark(conn, b_daily, b_event, b_pick, b_agg, "000660",
                          day_open_change_rate=4.0)
    conn.commit()
    conn.close()

    ranker = SectorStrengthRanker(db_path)
    selections = await ranker.select_for_day("2026-05-06")

    assert len(selections) == 1
    assert selections[0].sector_name == "반도체"
    assert selections[0].candidate_count == 2
    assert selections[0].best.stock_code == "005930"  # 강도 7.0 > 4.0


@pytest.mark.asyncio
async def test_select_for_day_single_candidate_sector_excluded(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, pick_id = _seed_tracking(
        conn, stock_code="005930", sector_name="반도체"
    )
    agg_id = _insert_agg(conn, daily_id, event_id, pick_id, "005930", hhmm="09:21",
                         low=1000, close=1005)
    _insert_agg(conn, daily_id, event_id, pick_id, "005930", hhmm="09:24",
                low=1001, close=1010)
    _insert_breakout_mark(conn, daily_id, event_id, pick_id, agg_id, "005930")
    conn.commit()
    conn.close()

    ranker = SectorStrengthRanker(db_path)
    # 기본 min_sector_candidates=2 → 단독 종목 섹터는 제외.
    selections = await ranker.select_for_day("2026-05-06")
    assert selections == []


@pytest.mark.asyncio
async def test_select_for_day_invalid_config_returns_empty(db_path: str):
    ranker = SectorStrengthRanker(db_path)
    bad = SectorStrengthConfig(min_sector_candidates=0)
    assert await ranker.select_for_day("2026-05-06", config=bad) == []


# -------------------- format --------------------
def test_format_sector_selection_contains_key_fields():
    best = _cand("반도체", "005930", 7.0)
    other = _cand("반도체", "000660", 4.0)
    from core.sector_strength import SectorSelection

    selection = SectorSelection(
        sector_name="반도체", candidate_count=2, best=best,
        candidates=(best, other), rule_version="phase25_sector_v1",
    )
    text = format_sector_selection(selection)
    assert "반도체" in text
    assert "005930" in text
    assert "000660" in text  # 동섹터 후보
    assert "최강" in text
