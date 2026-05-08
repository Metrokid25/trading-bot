"""DailyTracker 클래스 (D3) 단위 테스트.

tmp_path 기반 파일 SQLite DB 사용. KIS client는 AsyncMock. 실제 API 호출 없음.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.daily_tracker import DailyTracker


# ---------------------------------------------------------------------------
# 스키마 DDL
# ---------------------------------------------------------------------------

_DDL_STATEMENTS = [
    """
    CREATE TABLE sector_picks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        pick_date  TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        expires_at TEXT    NOT NULL,
        status     TEXT    NOT NULL,
        raw_input  TEXT    DEFAULT ''
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
        sector_name       TEXT    NOT NULL,
        registered_at_kst TEXT    NOT NULL,
        pick_date         TEXT
    )
    """,
    """
    CREATE TABLE pick_daily_tracking (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_pick_id        INTEGER NOT NULL REFERENCES sector_stocks(id),
        trading_day          TEXT    NOT NULL,
        day_offset           INTEGER NOT NULL,
        open                 REAL,
        high                 REAL,
        low                  REAL,
        close                REAL,
        volume               INTEGER,
        transaction_amount   INTEGER,
        return_vs_pick       REAL,
        return_vs_prev_close REAL,
        created_at           TEXT    NOT NULL,
        status               TEXT    NOT NULL DEFAULT 'pending',
        retry_count          INTEGER NOT NULL DEFAULT 0,
        event_id             INTEGER NOT NULL REFERENCES sector_pick_events(event_id),
        UNIQUE(event_id, stock_pick_id, trading_day)
    )
    """,
]


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "tracker_d3.db")
    conn = sqlite3.connect(path)
    for stmt in _DDL_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return path


def _make_tracker(db_path: str, candle_rows: list) -> DailyTracker:
    client = MagicMock()
    client.get_daily_candles = AsyncMock(return_value=candle_rows)
    return DailyTracker(db_path, client)


def _kis_row(date_str: str, oprc, hgpr, lwpr, clpr, vol, value) -> dict:
    return {
        "stck_bsop_date": date_str,
        "stck_oprc": str(oprc),
        "stck_hgpr": str(hgpr),
        "stck_lwpr": str(lwpr),
        "stck_clpr": str(clpr),
        "acml_vol": str(vol),
        "acml_tr_pbmn": str(value),
    }


def _insert_event(
    db_path: str,
    sector_name: str,
    pick_date_str: str,
    stocks: list[tuple[str, str]],
    inactive_stocks: list[tuple[str, str]] | None = None,
) -> int:
    """sector_picks + sector_stocks + sector_pick_events 삽입, event_id 반환."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO sector_picks (pick_date, created_at, expires_at, status)"
        " VALUES (?, '2026-05-01T09:00:00', '2026-05-15T09:00:00', 'active')",
        (pick_date_str,),
    )
    pick_id = cur.lastrowid
    for i, (code, name) in enumerate(stocks, start=1):
        conn.execute(
            "INSERT INTO sector_stocks"
            " (pick_id, sector_name, stock_code, stock_name, added_order, tracking_status)"
            " VALUES (?, ?, ?, ?, ?, 'active')",
            (pick_id, sector_name, code, name, i),
        )
    if inactive_stocks:
        for i, (code, name) in enumerate(inactive_stocks, start=len(stocks) + 1):
            conn.execute(
                "INSERT INTO sector_stocks"
                " (pick_id, sector_name, stock_code, stock_name, added_order, tracking_status)"
                " VALUES (?, ?, ?, ?, ?, 'inactive')",
                (pick_id, sector_name, code, name, i),
            )
    cur2 = conn.execute(
        "INSERT INTO sector_pick_events (sector_name, registered_at_kst, pick_date)"
        " VALUES (?, '2026-05-01T09:00:00', ?)",
        (sector_name, pick_date_str),
    )
    event_id = cur2.lastrowid
    conn.commit()
    conn.close()
    return event_id


def _all_tracking(db_path: str) -> list[tuple]:
    """pick_daily_tracking 전체 (stock_pick_id, trading_day, day_offset, status, event_id)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT stock_pick_id, trading_day, day_offset, status, event_id"
        " FROM pick_daily_tracking ORDER BY stock_pick_id, day_offset"
    ).fetchall()
    conn.close()
    return rows


def _tracking_for(db_path: str, ticker: str, target_date_str: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT pdt.status, pdt.open, pdt.high, pdt.low, pdt.close,
               pdt.volume, pdt.transaction_amount,
               pdt.return_vs_pick, pdt.return_vs_prev_close,
               pdt.event_id, pdt.day_offset
        FROM pick_daily_tracking pdt
        JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
        WHERE ss.stock_code = ? AND pdt.trading_day = ?
        LIMIT 1
        """,
        (ticker, target_date_str),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    keys = (
        "status", "open", "high", "low", "close",
        "volume", "transaction_amount",
        "return_vs_pick", "return_vs_prev_close",
        "event_id", "day_offset",
    )
    return dict(zip(keys, row))


# ---------------------------------------------------------------------------
# TC1: 단일 종목 → 21행, day_offset 0~20, status='pending', event_id 일치
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_single_stock_21_rows(db_path):
    event_id = _insert_event(db_path, "반도체", "2026-05-06", [("005930", "삼성전자")])
    tracker = _make_tracker(db_path, [])

    result = await tracker.ensure_tracking_rows(event_id)

    assert result == 21
    rows = _all_tracking(db_path)
    assert len(rows) == 21
    assert [r[2] for r in rows] == list(range(21))
    assert all(r[3] == "pending" for r in rows)
    assert all(r[4] == event_id for r in rows)


# ---------------------------------------------------------------------------
# TC2: 다중 종목 3개 → 63행
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_three_stocks_63_rows(db_path):
    event_id = _insert_event(
        db_path, "반도체", "2026-05-06",
        [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("042700", "한미반도체")],
    )
    tracker = _make_tracker(db_path, [])

    result = await tracker.ensure_tracking_rows(event_id)

    assert result == 63
    assert len(_all_tracking(db_path)) == 63


# ---------------------------------------------------------------------------
# TC3: 두 번 호출 → 중복 없음 (INSERT OR IGNORE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_idempotent(db_path):
    event_id = _insert_event(db_path, "반도체", "2026-05-06", [("005930", "삼성전자")])
    tracker = _make_tracker(db_path, [])

    first = await tracker.ensure_tracking_rows(event_id)
    second = await tracker.ensure_tracking_rows(event_id)

    assert first == 21
    assert second == 0
    assert len(_all_tracking(db_path)) == 21


# ---------------------------------------------------------------------------
# TC4: 휴장일 스킵 — 금요일 D+0, 월요일 D+1 (주말 건너뜀)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_weekend_skipped(db_path):
    # 2026-04-24 = 금요일
    event_id = _insert_event(db_path, "반도체", "2026-04-24", [("005930", "삼성전자")])
    tracker = _make_tracker(db_path, [])

    await tracker.ensure_tracking_rows(event_id)

    rows = _all_tracking(db_path)
    assert len(rows) == 21
    assert rows[0][1] == "2026-04-24"  # D+0: 금요일
    assert rows[1][1] == "2026-04-27"  # D+1: 월요일 (토·일 스킵)


# ---------------------------------------------------------------------------
# TC5: tracking_status='inactive' 종목 제외
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_inactive_excluded(db_path):
    event_id = _insert_event(
        db_path, "반도체", "2026-05-06",
        [("005930", "삼성전자")],
        inactive_stocks=[("000660", "SK하이닉스")],
    )
    tracker = _make_tracker(db_path, [])

    result = await tracker.ensure_tracking_rows(event_id)

    assert result == 21          # active 1종목만
    assert len(_all_tracking(db_path)) == 21


# ---------------------------------------------------------------------------
# TC6: collect_daily 정상 수집 → status='success', OHLCV 채워짐, return 계산됨
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_success(db_path):
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 8)
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    kis_rows = [
        _kis_row("20260506", 9800, 10200, 9700, 10000, 1_000_000, 10_000_000_000),  # D+0
        _kis_row("20260507", 10000, 10800, 9900, 10500, 800_000,   8_400_000_000),  # D+1
        _kis_row("20260508", 10500, 11500, 10300, 11000, 900_000,  9_900_000_000),  # D+2
    ]
    tracker = _make_tracker(db_path, kis_rows)

    result = await tracker.collect_daily(event_id, "005930", target_date)

    assert result is True
    row = _tracking_for(db_path, "005930", "2026-05-08")
    assert row is not None
    assert row["status"] == "success"
    assert row["open"] == 10500
    assert row["high"] == 11500
    assert row["low"] == 10300
    assert row["close"] == 11000
    assert row["volume"] == 900_000
    assert row["transaction_amount"] == 9_900_000_000
    # return_vs_pick: (11000 - 10000) / 10000 = 0.1
    assert abs(row["return_vs_pick"] - 0.1) < 1e-9
    # return_vs_prev_close: (11000 - 10500) / 10500
    assert abs(row["return_vs_prev_close"] - (11000 - 10500) / 10500) < 1e-9


# ---------------------------------------------------------------------------
# TC7: ensure_tracking_rows 후 collect_daily → UPDATE (pending → success)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_updates_pending_row(db_path):
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 8)
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    await _make_tracker(db_path, []).ensure_tracking_rows(event_id)

    pre = _tracking_for(db_path, "005930", "2026-05-08")
    assert pre is not None and pre["status"] == "pending" and pre["close"] is None

    kis_rows = [
        _kis_row("20260506", 9800, 10200, 9700, 10000, 1_000_000, 10_000_000_000),
        _kis_row("20260508", 10500, 11500, 10300, 11000, 900_000,  9_900_000_000),
    ]
    result = await _make_tracker(db_path, kis_rows).collect_daily(
        event_id, "005930", target_date
    )

    assert result is True
    post = _tracking_for(db_path, "005930", "2026-05-08")
    assert post["status"] == "success"
    assert post["close"] == 11000


# ---------------------------------------------------------------------------
# TC8: ensure_tracking_rows 없이 collect_daily → INSERT (ON CONFLICT 미발동)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_inserts_without_prior_ensure(db_path):
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 7)
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    kis_rows = [
        _kis_row("20260506", 9800, 10200, 9700, 10000, 1_000_000, 10_000_000_000),
        _kis_row("20260507", 10000, 10800, 9900, 10500, 800_000,   8_400_000_000),
    ]
    result = await _make_tracker(db_path, kis_rows).collect_daily(
        event_id, "005930", target_date
    )

    assert result is True
    rows = _all_tracking(db_path)
    assert len(rows) == 1
    assert rows[0][3] == "success"


# ---------------------------------------------------------------------------
# TC9: KIS target_date 데이터 없음 → False, DB 무변경
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_false_when_no_target_date(db_path):
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 8)
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    # KIS 응답에 target_date(2026-05-08) 없음
    kis_rows = [
        _kis_row("20260506", 9800, 10200, 9700, 10000, 1_000_000, 10_000_000_000),
        _kis_row("20260507", 10000, 10800, 9900, 10500, 800_000,   8_400_000_000),
    ]
    result = await _make_tracker(db_path, kis_rows).collect_daily(
        event_id, "005930", target_date
    )

    assert result is False
    assert len(_all_tracking(db_path)) == 0


# ---------------------------------------------------------------------------
# TC10: multi-event 격리 — 같은 stock_pick_id+trading_day에 다른 event_id 행 공존 가능
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_multi_event_isolation(db_path):
    """동일한 종목+날짜라도 event_id가 다르면 별도 행으로 격리 저장.

    Codex 적대적 리뷰 회귀 방지: 한 event의 수집이 다른 event 행을 silently
    덮어쓰지 않음을 검증.
    """
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 7)

    # 같은 섹터·같은 pick_date로 두 개의 event 등록 (재픽업 시뮬레이션)
    event_a = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])
    # 두 번째 event는 sector_pick_events만 추가 (같은 stock_pick_id 공유)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO sector_pick_events (sector_name, registered_at_kst, pick_date)"
        " VALUES ('반도체', '2026-05-01T10:00:00', ?)",
        (pick_date,),
    )
    event_b = cur.lastrowid
    conn.commit()
    conn.close()

    kis_rows = [
        _kis_row("20260506", 9800, 10200, 9700, 10000, 1_000_000, 10_000_000_000),
        _kis_row("20260507", 10000, 10800, 9900, 10500, 800_000,   8_400_000_000),
    ]

    # event_a로 수집
    result_a = await _make_tracker(db_path, kis_rows).collect_daily(
        event_a, "005930", target_date
    )
    # event_b로 수집 (같은 종목, 같은 target_date)
    result_b = await _make_tracker(db_path, kis_rows).collect_daily(
        event_b, "005930", target_date
    )

    assert result_a is True
    assert result_b is True

    # 같은 stock_pick_id + trading_day에 event_id가 다른 두 행이 모두 존재
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT event_id, status, close FROM pick_daily_tracking"
        " WHERE trading_day = '2026-05-07'"
        " ORDER BY event_id"
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert {r[0] for r in rows} == {event_a, event_b}
    assert all(r[1] == "success" for r in rows)
    assert all(r[2] == 10500 for r in rows)


# ---------------------------------------------------------------------------
# TC11: return_vs_pick 계산 정확도 (pick close=10000, target close=11000 → 0.1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_return_vs_pick_accuracy(db_path):
    pick_date = "2026-05-06"
    target_date = date(2026, 5, 8)
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    kis_rows = [
        _kis_row("20260506", 9000, 10500, 8500, 10000, 1_000_000, 10_000_000_000),  # close=10000
        _kis_row("20260507", 10000, 11000, 9500, 10500, 800_000,   8_400_000_000),
        _kis_row("20260508", 10500, 11500, 10300, 11000, 900_000,  9_900_000_000),  # close=11000
    ]
    await _make_tracker(db_path, kis_rows).collect_daily(event_id, "005930", target_date)

    row = _tracking_for(db_path, "005930", "2026-05-08")
    assert abs(row["return_vs_pick"] - 0.1) < 1e-9  # (11000-10000)/10000 = 0.1


# ---------------------------------------------------------------------------
# TC12: 알 수 없는 ticker → False, DB 무변경
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_daily_unknown_ticker_returns_false(db_path):
    pick_date = "2026-05-06"
    event_id = _insert_event(db_path, "반도체", pick_date, [("005930", "삼성전자")])

    result = await _make_tracker(db_path, []).collect_daily(
        event_id, "999999", date(2026, 5, 7)
    )

    assert result is False
    assert len(_all_tracking(db_path)) == 0
