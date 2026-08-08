"""paper_runner P0 데이터 완결성·집계 불변식 회귀 테스트."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from backtest.toss_client import Bar, KST
from strategy import paper_runner


DAY = date(2026, 7, 14)


def _cache_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE candles (symbol TEXT,ts TEXT,open INT,high INT,low INT,"
        "close INT,volume INT,PRIMARY KEY(symbol,ts))")
    con.execute(
        "CREATE TABLE fetched (symbol TEXT,start TEXT,end TEXT,"
        "PRIMARY KEY(symbol,start,end))")


def _bars(code: str, start: datetime, count: int):
    return [
        (code, (start + timedelta(minutes=i)).isoformat(), 100, 101, 99, 100, 1)
        for i in range(count)
    ]


def test_session_coverage_blocks_truncated_symbol():
    con = sqlite3.connect(":memory:")
    _cache_schema(con)
    pre_start = datetime(2026, 7, 14, 8, 0)
    start = datetime(2026, 7, 14, 9, 0)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("FULL", pre_start, 59))
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("FULL", start, 391))
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("CUT", pre_start, 59))
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("CUT", start, 52))

    coverage = paper_runner.session_coverages(
        con, DAY, ["FULL", "CUT", "NONE"])
    by_code = {row.code: row for row in coverage}

    assert by_code["FULL"].status == "complete"
    assert by_code["FULL"].last_minute == "15:30"
    assert by_code["CUT"].status == "partial"
    assert by_code["CUT"].last_minute == "09:51"
    assert by_code["NONE"].status == "no_data"
    ok, note = paper_runner.session_quality(coverage)
    assert not ok
    assert "CUT:52/391@09:00-09:51" in note
    assert paper_runner.session_coverages_many(
        con, [DAY], ["FULL", "CUT", "NONE"])[DAY] == coverage


def test_session_coverage_requires_premarket_for_historically_eligible_code():
    con = sqlite3.connect(":memory:")
    _cache_schema(con)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("NXT", datetime(2026, 7, 13, 8, 0), 59))
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("NXT", datetime(2026, 7, 13, 9, 0), 391))
    for offset in range(1, 4):
        con.executemany(
            "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
            _bars("NON_NXT", datetime(2026, 7, 14 - offset, 9, 0), 391))
    regular_start = datetime(2026, 7, 14, 9, 0)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("NXT", regular_start, 391))
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("NON_NXT", regular_start, 391))

    unknown = {row.code: row for row in paper_runner.session_coverages(
        con, DAY, ["NXT", "NON_NXT"])}
    by_code = {row.code: row for row in paper_runner.session_coverages(
        con, DAY, ["NXT", "NON_NXT"],
        eligibility={"NON_NXT": False})}

    assert by_code["NXT"].premarket_expected
    assert by_code["NXT"].status == "partial"
    assert not by_code["NON_NXT"].premarket_expected
    assert by_code["NON_NXT"].status == "complete"
    assert not unknown["NON_NXT"].premarket_known
    assert unknown["NON_NXT"].status == "partial"


def test_session_coverage_blocks_missing_market_open_window():
    con = sqlite3.connect(":memory:")
    _cache_schema(con)
    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        _bars("LATE", datetime(2026, 7, 14, 8, 0), 59))
    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        _bars("LATE", datetime(2026, 7, 14, 9, 29), 352))

    coverage = paper_runner.session_coverages(con, DAY, ["LATE"])[0]

    assert coverage.valid_bar_count == 352
    assert coverage.first_minute == "09:29"
    assert coverage.status == "partial"


def test_session_coverage_blocks_unknown_premarket_eligibility():
    con = sqlite3.connect(":memory:")
    _cache_schema(con)
    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        _bars("NEW", datetime(2026, 7, 14, 9, 0), 391))

    coverage = paper_runner.session_coverages(con, DAY, ["NEW"])[0]

    assert not coverage.premarket_known
    assert coverage.status == "partial"


def test_session_coverage_checks_regular_and_premarket_continuity():
    con = sqlite3.connect(":memory:")
    _cache_schema(con)
    pre_start = datetime(2026, 7, 14, 8, 0)
    regular_start = datetime(2026, 7, 14, 9, 0)
    for code in ("PRE_LATE", "PRE_GAP", "REG_GAP"):
        con.executemany(
            "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
            _bars(code, regular_start, 391))

    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        _bars("PRE_LATE", pre_start + timedelta(minutes=5), 54))
    pre_gap = _bars("PRE_GAP", pre_start, 59)
    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        [row for i, row in enumerate(pre_gap) if not 20 <= i <= 24])
    con.executemany(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        _bars("REG_GAP", pre_start, 59))
    con.execute(
        "DELETE FROM candles WHERE symbol='REG_GAP' "
        "AND substr(ts,12,5) BETWEEN '10:00' AND '10:06'")

    by_code = {row.code: row for row in paper_runner.session_coverages(
        con, DAY, ["PRE_LATE", "PRE_GAP", "REG_GAP"])}

    assert by_code["PRE_LATE"].premarket_bar_count == 54
    assert by_code["PRE_LATE"].premarket_first_minute == "08:05"
    assert by_code["PRE_LATE"].status == "partial"
    assert by_code["PRE_GAP"].premarket_bar_count == 54
    assert by_code["PRE_GAP"].premarket_max_gap_minutes == 6
    assert by_code["PRE_GAP"].status == "partial"
    assert by_code["REG_GAP"].valid_bar_count == 384
    assert by_code["REG_GAP"].max_gap_minutes == 8
    assert by_code["REG_GAP"].status == "partial"


def test_session_quality_blocks_even_one_unverified_no_data_symbol():
    complete = [
        paper_runner.SessionCoverage(
            code=str(i), bar_count=391, valid_bar_count=391,
            expected_count=391, premarket_bar_count=59,
            premarket_expected=True, premarket_known=True,
            premarket_first_minute="08:00",
            premarket_last_minute="08:58", premarket_max_gap_minutes=1,
            max_gap_minutes=1, first_minute="09:00",
            last_minute="15:30", status="complete")
        for i in range(19)
    ]
    no_data = paper_runner.SessionCoverage(
        code="HALT", bar_count=0, valid_bar_count=0, expected_count=391,
        premarket_bar_count=0, premarket_expected=False,
        premarket_known=False, premarket_first_minute=None,
        premarket_last_minute=None, premarket_max_gap_minutes=0,
        max_gap_minutes=0,
        first_minute=None, last_minute=None, status="no_data")

    ok, note = paper_runner.session_quality([*complete, no_data])

    assert not ok
    assert "ratio=0.950" in note


def test_paper_conn_migrates_quality_columns_and_audit_table(
        tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    monkeypatch.setattr(paper_runner, "PAPER_DB", db)

    con = paper_runner.paper_conn()
    columns = {row[1] for row in con.execute("PRAGMA table_info(paper_daily)")}
    table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='paper_data_quality'").fetchone()
    session_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='paper_session_status'").fetchone()
    eligibility_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='paper_premarket_eligibility'").fetchone()
    quality_columns = {
        row[1] for row in con.execute("PRAGMA table_info(paper_data_quality)")}
    con.close()

    assert {"data_complete", "data_quality_note"} <= columns
    assert {"premarket_bar_count", "premarket_expected", "premarket_known",
            "premarket_first_minute", "premarket_last_minute",
            "premarket_max_gap_minutes", "max_gap_minutes"} <= quality_columns
    assert table == (1,)
    assert session_table == (1,)
    assert eligibility_table == (1,)


def test_official_registry_backfills_historical_true_and_false(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    result = paper_runner.premarket_eligibility_for_days(
        con, [DAY], ["000660", "001440"],
        datetime(2026, 8, 8, 12, 0, tzinfo=KST))

    assert result == {DAY: {"000660": True, "001440": False}}
    assert con.execute(
        "SELECT code,expected,source FROM paper_premarket_eligibility "
        "ORDER BY code").fetchall() == [
            ("000660", 1, "official_nxt_2026q3"),
            ("001440", 0, "official_nxt_2026q3")]
    con.close()


def test_expired_registry_blocks_current_day(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()

    expired_day = date(2026, 10, 1)
    with pytest.raises(RuntimeError, match="registry expired"):
        paper_runner.premarket_eligibility_for_days(
            con, [expired_day], ["000660"],
            datetime(2026, 10, 1, 9, 0, tzinfo=KST))
    con.close()


def _daily(con, day_s: str, strategy: str, n: int,
           day_ret: float, equity: float) -> None:
    con.execute(
        "INSERT INTO paper_daily "
        "(day,strategy,n_trades,day_ret,equity,note,recorded_at,regime,"
        "finalized,data_complete,data_quality_note) "
        "VALUES (?,?,?,?,?,'','t',?,1,1,'ok')",
        (day_s, strategy, n, day_ret, equity, paper_runner.REGIME))


def test_trade_ledger_invariant_rejects_daily_count_mismatch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    _daily(con, DAY.isoformat(), "v2", 1, 0.10, 1.10)
    con.execute(
        "INSERT INTO paper_trades VALUES "
        "('v2','A','A',?,?,?,?,?,'t')",
        (DAY.isoformat(), DAY.isoformat(), 0.10, 0.10, "closed"))

    paper_runner._assert_day_invariants(con, DAY, {"v2"})
    con.execute(
        "UPDATE paper_daily SET n_trades=2 WHERE day=? AND strategy='v2'",
        (DAY.isoformat(),))

    with pytest.raises(RuntimeError, match="trade count mismatch"):
        paper_runner._assert_day_invariants(con, DAY, {"v2"})
    con.close()


def test_full_replay_invariant_checks_past_daily_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    previous = DAY - timedelta(days=1)
    _daily(con, previous.isoformat(), "gm_v3", 1, 0.10, 1.10)
    _daily(con, DAY.isoformat(), "gm_v3", 0, 0.0, 1.10)

    with pytest.raises(RuntimeError, match=previous.isoformat()):
        paper_runner._assert_day_invariants(con, DAY, {"gm_v3"})
    con.close()


def test_portfolio_invariant_rejects_allocation_count_mismatch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    _daily(con, DAY.isoformat(), "v2_portfolio", 1, 0.0, 1.0)

    with pytest.raises(RuntimeError, match="allocation count mismatch"):
        paper_runner._assert_day_invariants(
            con, DAY, {"v2_portfolio"})
    con.close()


def test_record_upto_stops_after_unrepaired_previous_day(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    con.execute("INSERT INTO paper_meta VALUES ('paper_start','2026-07-14')")
    _daily(con, DAY.isoformat(), "v2", 0, 0.0, 1.0)
    con.execute(
        "UPDATE paper_daily SET finalized=0,data_complete=0 WHERE day=?",
        (DAY.isoformat(),))
    con.commit()
    con.close()
    calls = []

    def fake_record_day(day):
        calls.append(day)
        return {"day": day.isoformat(), "finalized": 0}

    monkeypatch.setattr(paper_runner, "record_day", fake_record_day)
    monkeypatch.setattr(paper_runner, "_is_trading_day_cached", lambda _d: True)

    result = paper_runner.record_upto(DAY + timedelta(days=1))

    assert calls == [DAY]
    assert result == [{"day": DAY.isoformat(), "finalized": 0}]


def test_record_upto_blocks_legacy_unverified_history(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    con.execute("INSERT INTO paper_meta VALUES ('paper_start','2026-07-13')")
    previous = DAY - timedelta(days=1)
    _daily(con, previous.isoformat(), "v2", 0, 0.0, 1.0)
    _daily(con, DAY.isoformat(), "v2", 0, 0.0, 1.0)
    con.execute("UPDATE paper_daily SET data_complete=0 WHERE day=?",
                (previous.isoformat(),))
    con.commit()
    con.close()

    with pytest.raises(SystemExit, match="paper_start부터"):
        paper_runner.record_upto(DAY + timedelta(days=1))


def test_record_upto_skips_confirmed_empty_session(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    previous = date(2026, 7, 16)
    empty_day = date(2026, 7, 17)
    target = date(2026, 7, 18)
    con.execute("INSERT INTO paper_meta VALUES ('paper_start','2026-07-16')")
    _daily(con, previous.isoformat(), "v2", 0, 0.0, 1.0)
    con.execute(
        "INSERT INTO paper_session_status VALUES "
        "(?,'confirmed_empty',2,'all-zero','t')", (empty_day.isoformat(),))
    con.commit()
    con.close()
    calls = []
    monkeypatch.setattr(
        paper_runner, "record_day",
        lambda day: calls.append(day) or {"day": day.isoformat(), "finalized": 0})
    monkeypatch.setattr(paper_runner, "_is_trading_day_cached", lambda _d: True)

    paper_runner.record_upto(target)

    assert calls == [target]


def test_record_day_only_confirms_explicit_empty_session_override(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    con.execute("INSERT INTO paper_meta VALUES ('paper_start','2026-07-01')")
    con.commit()
    con.close()
    cache_db = tmp_path / "cache.db"
    cache = sqlite3.connect(cache_db)
    _cache_schema(cache)
    cache.close()
    universe = [(f"C{i:02d}", f"N{i}", "S") for i in range(10)]

    monkeypatch.setattr(paper_runner, "materialize_expired_picks", lambda _p: 0)
    monkeypatch.setattr(paper_runner, "load_universe", lambda **_kw: universe)
    monkeypatch.setattr(
        paper_runner, "load_membership_windows", lambda *_args: None)
    monkeypatch.setattr(paper_runner, "ensure_day_cached", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        paper_runner, "_cache_conn", lambda: sqlite3.connect(cache_db))
    monkeypatch.setattr(paper_runner, "daily_bars", lambda _code: [])
    monkeypatch.setattr(
        paper_runner, "now_kst",
        lambda: datetime(2026, 7, 18, 9, 0, tzinfo=KST))

    confirmed = paper_runner.record_day(date(2026, 7, 17))
    ordinary_first = paper_runner.record_day(date(2026, 7, 16))
    ordinary_second = paper_runner.record_day(date(2026, 7, 16))

    assert confirmed["skipped"] == "confirmed_empty"
    assert confirmed["finalized"] == 1
    assert ordinary_first["skipped"] == "no_market_data"
    assert ordinary_second["skipped"] == "no_market_data"
    con = paper_runner.paper_conn()
    row = con.execute(
        "SELECT status,attempts FROM paper_session_status WHERE day='2026-07-17'"
    ).fetchone()
    ordinary = con.execute(
        "SELECT status FROM paper_session_status WHERE day='2026-07-16'"
    ).fetchone()
    con.close()
    assert row == ("confirmed_empty", 0)
    assert ordinary is None


def test_ensure_day_cached_skips_confirmed_empty_lookback(
        tmp_path, monkeypatch):
    cache_db = tmp_path / "cache.db"
    con = sqlite3.connect(cache_db)
    _cache_schema(con)
    con.close()

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def fetch_1m_range(self, *_args):
            raise AssertionError("confirmed empty day must not be fetched")

    empty_day = date(2026, 7, 17)
    monkeypatch.setattr(
        paper_runner, "_cache_conn", lambda: sqlite3.connect(cache_db))
    monkeypatch.setattr(paper_runner, "TossClient", FakeClient)
    monkeypatch.setattr(
        paper_runner, "now_kst",
        lambda: datetime(2026, 7, 18, 9, 0, tzinfo=KST))

    paper_runner.ensure_day_cached(
        empty_day, ["A"], lookback_days=0,
        confirmed_empty_days={empty_day})


def test_ensure_day_cached_repairs_partial_with_full_day_fetch(
        tmp_path, monkeypatch):
    cache_db = tmp_path / "cache.db"
    con = sqlite3.connect(cache_db)
    _cache_schema(con)
    start = datetime(2026, 7, 14, 9, 0, tzinfo=KST)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    _bars("CUT", start, 52))
    con.execute(
        "UPDATE candles SET open=0,high=0,low=0,close=0 "
        "WHERE symbol='CUT' AND ts=?", (start.isoformat(),))
    con.execute("INSERT INTO fetched VALUES (?,?,?)",
                ("CUT", DAY.isoformat(), DAY.isoformat()))
    next_day_tile = datetime(2026, 7, 15, 8, 0, tzinfo=KST)
    con.execute(
        "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
        ("CUT", next_day_tile.isoformat(), 777, 777, 777, 777, 1))
    con.commit()
    con.close()

    tail = [
        Bar(start + timedelta(minutes=i), 100, 101, 99, 100, 1)
        for i in range(52, 391)
    ]
    full = [
        *[Bar(datetime(2026, 7, 14, 8, 0, tzinfo=KST)
              + timedelta(minutes=i), 100, 101, 99, 100, 1)
          for i in range(59)],
        *[Bar(start + timedelta(minutes=i), 100, 101, 99, 100, 1)
          for i in range(391)],
        # Toss 범위 응답은 end+1 09:00 커서 때문에 다음날 프리장 타일을 포함할 수 있다.
        Bar(next_day_tile, 100, 101, 99, 100, 1),
    ]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def fetch_1m_since(self, _code, _last):
            return tail

        def fetch_1m_range(self, _code, _start, _end):
            return full

    monkeypatch.setattr(
        paper_runner, "_cache_conn", lambda: sqlite3.connect(cache_db))
    monkeypatch.setattr(paper_runner, "TossClient", FakeClient)
    monkeypatch.setattr(paper_runner, "_is_trading_day_cached", lambda _d: True)
    monkeypatch.setattr(
        paper_runner, "now_kst",
        lambda: datetime(2026, 7, 15, 9, 0, tzinfo=KST))

    paper_runner.ensure_day_cached(DAY, ["CUT"], lookback_days=0)

    con = sqlite3.connect(cache_db)
    coverage = paper_runner.session_coverages(con, DAY, ["CUT"])[0]
    marker = con.execute(
        "SELECT 1 FROM fetched WHERE symbol='CUT' AND start=? AND end=?",
        (DAY.isoformat(), DAY.isoformat())).fetchone()
    repaired_open = con.execute(
        "SELECT open FROM candles WHERE symbol='CUT' AND ts=?",
        (start.isoformat(),)).fetchone()[0]
    preserved_tile = con.execute(
        "SELECT open FROM candles WHERE symbol='CUT' AND ts=?",
        (next_day_tile.isoformat(),)).fetchone()[0]
    con.close()
    assert coverage.status == "complete"
    assert coverage.valid_bar_count == 391
    assert marker == (1,)
    assert repaired_open == 100
    assert preserved_tile == 777


def test_ensure_day_cached_retries_no_data_marker(tmp_path, monkeypatch):
    cache_db = tmp_path / "cache.db"
    con = sqlite3.connect(cache_db)
    _cache_schema(con)
    con.execute("INSERT INTO fetched VALUES (?,?,?)",
                ("EMPTY", DAY.isoformat(), DAY.isoformat()))
    con.commit()
    con.close()
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def fetch_1m_range(self, code, start, end):
            calls.append((code, start, end))
            return []

    monkeypatch.setattr(
        paper_runner, "_cache_conn", lambda: sqlite3.connect(cache_db))
    monkeypatch.setattr(paper_runner, "TossClient", FakeClient)
    monkeypatch.setattr(paper_runner, "_is_trading_day_cached", lambda _d: True)
    monkeypatch.setattr(
        paper_runner, "now_kst",
        lambda: datetime(2026, 7, 15, 9, 0, tzinfo=KST))

    paper_runner.ensure_day_cached(DAY, ["EMPTY"], lookback_days=0)
    paper_runner.ensure_day_cached(DAY, ["EMPTY"], lookback_days=0)

    con = sqlite3.connect(cache_db)
    marker = con.execute(
        "SELECT 1 FROM fetched WHERE symbol='EMPTY' AND start=? AND end=?",
        (DAY.isoformat(), DAY.isoformat())).fetchone()
    repair = con.execute(
        "SELECT attempts,status FROM paper_cache_repairs "
        "WHERE symbol='EMPTY' AND day=?", (DAY.isoformat(),)).fetchone()
    con.close()
    assert calls == [("EMPTY", DAY, DAY)]
    assert marker is None
    assert repair == (1, "retry_wait")
