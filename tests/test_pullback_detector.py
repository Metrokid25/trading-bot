from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.breakout_marker import EARLY_BREAKOUT
from core.pullback_alert import (
    emit_pullback_alerts,
    format_pullback_alert,
)
from core.pullback_detector import (
    PULLBACK_HOLD,
    PullbackAggBar,
    PullbackDetector,
    PullbackResult,
    PullbackRuleConfig,
    PullbackTarget,
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
    path = str(tmp_path / "pullback.db")
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
    stock_code: str = "005930",
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
        " VALUES (?, 'semi', ?, 'Samsung', 1, 'active')",
        (pick_id, stock_code),
    )
    stock_pick_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO sector_pick_events (pick_id, sector_name, registered_at_kst, pick_date)"
        " VALUES (?, 'semi', '2026-05-06T09:00:00', '2026-05-06')",
        (pick_id,),
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
    *,
    stock_code: str = "005930",
    trading_day: str = "2026-05-06",
    hhmm: str = "09:21",
    low: float = 1000,
    close: float = 1005,
    open_: float | None = None,
    high: float = 1010,
    volume: int = 100,
    value: int = 200_000_000,
    interval_minutes: int = 3,
) -> int:
    open_ = close - 1.0 if open_ is None else open_
    bucket_start = f"{trading_day}T{hhmm}:00"
    cur = conn.execute(
        "INSERT INTO pick_minute_agg"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, bucket_start, bucket_end,"
        "  open, high, low, close, volume, value, raw_count, expected_count,"
        "  is_complete, source, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'RAW_1M',"
        " '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_id, event_id, stock_pick_id, stock_code, trading_day,
            interval_minutes, bucket_start, f"{trading_day}T{hhmm}:59",
            open_, high, low, close, volume, value, interval_minutes,
            interval_minutes,
        ),
    )
    return int(cur.lastrowid)


def _insert_breakout_mark(
    conn: sqlite3.Connection,
    daily_id: int,
    event_id: int,
    stock_pick_id: int,
    agg_id: int,
    *,
    stock_code: str = "005930",
    trading_day: str = "2026-05-06",
) -> None:
    conn.execute(
        "INSERT INTO pick_breakout_marks"
        " (daily_tracking_id, event_id, stock_pick_id, stock_code, trading_day,"
        "  day_offset, interval_minutes, agg_id, bucket_start, bucket_end,"
        "  breakout_type, threshold_prev_change_rate, threshold_day_open_change_rate,"
        "  threshold_value, threshold_value_ratio, rule_version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, 3, ?, '2026-05-06T08:30:00', '2026-05-06T08:32:00',"
        " ?, 1.5, 3.0, 500000000, 3.0, 'phase25_breakout_v1',"
        " '2026-05-06T16:00:00', '2026-05-06T16:00:00')",
        (
            daily_id, event_id, stock_pick_id, stock_code, trading_day,
            agg_id, EARLY_BREAKOUT,
        ),
    )


# -------------------- evaluate() 순수 로직 --------------------
def _pb_bar(
    hhmm: str,
    low: float,
    close: float,
    *,
    open_: float | None = None,
    value: int | None = 200_000_000,
) -> PullbackAggBar:
    o = close - 1.0 if open_ is None else open_
    return PullbackAggBar(
        interval_minutes=3,
        bucket_start=f"2026-05-06T{hhmm}:00",
        bucket_end=f"2026-05-06T{hhmm}:59",
        open=o,
        high=close + 1.0,
        low=low,
        close=close,
        volume=100,
        value=value,
    )


_TARGET = PullbackTarget(
    daily_tracking_id=1,
    event_id=2,
    stock_pick_id=3,
    stock_code="005930",
    trading_day="2026-05-06",
    day_offset=0,
)


def test_evaluate_happy_path_returns_signal():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=1001, close=1010),
        _pb_bar("09:27", low=1002, close=1015),
    ]
    signal = detector.evaluate(_TARGET, bars, PullbackRuleConfig())
    assert signal is not None
    assert signal.signal_type == PULLBACK_HOLD
    assert signal.window_low == 1000
    assert signal.last_close == 1015
    assert signal.min_window_value == 200_000_000


def test_evaluate_low_break_returns_none():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=980, close=1010),  # 1000*0.995=995 미만 → 저점 깨짐
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is None


def test_evaluate_low_break_within_tolerance_ok():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=996, close=1010),  # 995 이상 → 허용오차 내
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is not None


def test_evaluate_gradual_low_erosion_breaks_support():
    """연속 소폭 하락으로 최초 저점(지지선)을 깨면 탈락.

    running_min 기준이었다면 [1000→996→992]는 각 직전 저점 대비 0.5% 내라 통과했을
    것이나, first_low(1000) 절대 기준 지지선 995를 992가 깨므로 탈락해야 한다.
    """
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=996, close=1004),
        _pb_bar("09:27", low=992, close=1006),  # 1000*0.995=995 미만 → 지지선 붕괴
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is None


def test_evaluate_insufficient_value_returns_none():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005, value=50_000_000),  # 1억 미만
        _pb_bar("09:24", low=1001, close=1010),
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is None


def test_evaluate_red_last_close_returns_none():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=1001, close=1002, open_=1010),  # 음봉 마감
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is None


def test_evaluate_red_last_close_ok_when_green_not_required():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005),
        _pb_bar("09:24", low=1001, close=1002, open_=1010),
    ]
    config = PullbackRuleConfig(require_green_close=False)
    assert detector.evaluate(_TARGET, bars, config) is not None


def test_evaluate_missing_field_returns_none():
    detector = PullbackDetector(":memory:")
    bars = [
        _pb_bar("09:21", low=1000, close=1005, value=None),  # 거래대금 결측
        _pb_bar("09:24", low=1001, close=1010),
    ]
    assert detector.evaluate(_TARGET, bars, PullbackRuleConfig()) is None


def test_evaluate_empty_returns_none():
    detector = PullbackDetector(":memory:")
    assert detector.evaluate(_TARGET, [], PullbackRuleConfig()) is None


# -------------------- detect_for_tracking_row DB 통합 --------------------
@pytest.mark.asyncio
async def test_detect_no_target(db_path: str):
    detector = PullbackDetector(db_path)
    result, signal = await detector.detect_for_tracking_row(9999)
    assert result == PullbackResult.SKIPPED_NO_TARGET
    assert signal is None


@pytest.mark.asyncio
async def test_detect_no_strength_gate(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    # 윈도우 봉은 있지만 강세 마크가 없다 → SKIPPED_NO_STRENGTH.
    _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:21")
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    result, signal = await detector.detect_for_tracking_row(daily_id)
    assert result == PullbackResult.SKIPPED_NO_STRENGTH
    assert signal is None


@pytest.mark.asyncio
async def test_detect_no_window_bars(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    # 강세 마크는 있으나 윈도우(09:20~09:40) 밖 봉만 존재.
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:00")
    _insert_breakout_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    result, signal = await detector.detect_for_tracking_row(daily_id)
    assert result == PullbackResult.SKIPPED_NO_WINDOW_BARS
    assert signal is None


@pytest.mark.asyncio
async def test_detect_success_full(db_path: str):
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:21",
                         low=1000, close=1005)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:24",
                low=1001, close=1010)
    _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:27",
                low=1002, close=1015)
    _insert_breakout_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    result, signal = await detector.detect_for_tracking_row(daily_id)
    assert result == PullbackResult.SUCCESS
    assert signal is not None
    assert signal.stock_code == "005930"
    assert signal.window_low == 1000
    assert signal.last_close == 1015


@pytest.mark.asyncio
async def test_detect_window_excludes_0940_boundary(db_path: str):
    # 주의: 09:40 bucket_start는 3분봉 정렬상 실제로는 생성되지 않는다(09:39가
    # 마지막 버킷). 여기선 substr(bucket_start,12,5) < '09:40' 상한 필터가
    # 경계를 정확히 자르는지 확인하려 인위적으로 09:40 row를 직접 삽입한다.
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn)
    agg_id = _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:39",
                         low=1000, close=1005)
    # 09:40 은 상한 exclusive → 윈도우 밖.
    _insert_agg(conn, daily_id, event_id, stock_pick_id, hhmm="09:40",
                low=900, close=905)
    _insert_breakout_mark(conn, daily_id, event_id, stock_pick_id, agg_id)
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    result, signal = await detector.detect_for_tracking_row(daily_id)
    assert result == PullbackResult.SUCCESS
    assert signal is not None
    # 09:40(저점 900) 봉이 포함됐다면 저점 유지 깨져 None 이었을 것.
    assert signal.window_low == 1000


@pytest.mark.asyncio
async def test_detect_all_d0_counts_and_signals(db_path: str):
    conn = _connect(db_path)
    # 종목 A: 강세+눌림목 성립 → SUCCESS
    a_daily, a_event, a_pick = _seed_tracking(conn, stock_code="005930")
    a_agg = _insert_agg(conn, a_daily, a_event, a_pick, stock_code="005930",
                        hhmm="09:21", low=1000, close=1005)
    _insert_agg(conn, a_daily, a_event, a_pick, stock_code="005930",
                hhmm="09:24", low=1001, close=1010)
    _insert_breakout_mark(conn, a_daily, a_event, a_pick, a_agg, stock_code="005930")
    # 종목 B: 윈도우 봉 있으나 강세 마크 없음 → SKIPPED_NO_STRENGTH
    b_daily, b_event, b_pick = _seed_tracking(conn, stock_code="000660")
    _insert_agg(conn, b_daily, b_event, b_pick, stock_code="000660",
                hhmm="09:21", low=2000, close=2005)
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    counts, signals = await detector.detect_all_d0(trading_day="2026-05-06")

    assert counts[PullbackResult.SUCCESS.value] == 1
    assert counts[PullbackResult.SKIPPED_NO_STRENGTH.value] == 1
    assert [s.stock_code for s in signals] == ["005930"]


@pytest.mark.asyncio
async def test_list_targets_dedupes_daily_id_with_conflicting_agg_stock_code(db_path: str):
    """동일 daily_tracking_id에 stock_code가 오염된 agg row가 섞여도 타겟은 1건.

    stock_code는 pick_minute_agg가 아니라 sector_stocks 기준으로 가져오므로
    pma.stock_code 불일치가 타겟 중복(이중 처리)을 유발하지 않는다.
    """
    conn = _connect(db_path)
    daily_id, event_id, stock_pick_id = _seed_tracking(conn, stock_code="005930")
    _insert_agg(conn, daily_id, event_id, stock_pick_id,
                stock_code="005930", hhmm="09:21")
    _insert_agg(conn, daily_id, event_id, stock_pick_id,
                stock_code="999999", hhmm="09:24")  # 오염된 stock_code
    conn.commit()
    conn.close()

    detector = PullbackDetector(db_path)
    targets = await detector.list_d0_targets(trading_day="2026-05-06")

    assert len(targets) == 1
    assert targets[0].daily_tracking_id == daily_id
    assert targets[0].stock_code == "005930"
    assert targets[0].event_id == event_id


@pytest.mark.asyncio
async def test_invalid_config_returns_failed(db_path: str):
    detector = PullbackDetector(db_path)
    bad = PullbackRuleConfig(window_start_hhmm="09:40", window_end_hhmm="09:20")
    counts, signals = await detector.detect_all_d0(rule_config=bad)
    assert counts[PullbackResult.FAILED.value] == 1
    assert signals == []


# -------------------- 알림 (dry-run / 실발송) --------------------
def _signal() -> "PullbackSignalForTest":
    from core.pullback_detector import PullbackSignal

    return PullbackSignal(
        daily_tracking_id=1, event_id=2, stock_pick_id=3, stock_code="005930",
        trading_day="2026-05-06", day_offset=0, interval_minutes=3,
        signal_type=PULLBACK_HOLD, window_start="09:20", window_end="09:40",
        first_bar_start="2026-05-06T09:21:00", last_bar_start="2026-05-06T09:27:00",
        window_low=1000.0, last_close=1015.0, last_open=1014.0,
        min_window_value=200_000_000, rule_version="phase25_pullback_v1",
        threshold_low_break_tolerance_pct=0.5, threshold_min_window_value=100_000_000,
    )


def test_format_pullback_alert_contains_key_fields():
    text = format_pullback_alert(_signal())
    assert "005930" in text
    assert "눌림목" in text
    assert "09:20~09:40" in text


@pytest.mark.asyncio
async def test_emit_dry_run_does_not_send():
    class Spy:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def notify(self, text: str) -> bool:
            self.sent.append(text)
            return True

    spy = Spy()
    messages = await emit_pullback_alerts([_signal()], telegram=spy, dry_run=True)
    assert len(messages) == 1
    assert spy.sent == []  # dry-run 은 절대 발송하지 않는다


@pytest.mark.asyncio
async def test_emit_real_send_calls_notifier():
    class Spy:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def notify(self, text: str) -> bool:
            self.sent.append(text)
            return True

    spy = Spy()
    messages = await emit_pullback_alerts([_signal()], telegram=spy, dry_run=False)
    assert len(messages) == 1
    assert len(spy.sent) == 1
    assert "005930" in spy.sent[0]


@pytest.mark.asyncio
async def test_emit_real_send_without_telegram_only_logs():
    messages = await emit_pullback_alerts([_signal()], telegram=None, dry_run=False)
    assert len(messages) == 1  # 발송 대상 없음 → 로그만, 예외 없음
