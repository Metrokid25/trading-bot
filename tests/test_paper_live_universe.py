"""paper_runner 라이브 유니버스 배선 + 동적 벤치마크 단위 테스트.

load_universe: 임시 trading.db 에 픽을 심어 웹앱 등록 뷰와 동일하게
active pick × active tracking 종목만, (섹터,종목) dedup 으로 나오는지.
bench_day: 당일 등록 유니버스 동일가중 시가→종가, 종목 dedup, 무봉 제외.
"""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from core.time_utils import now_kst
from data.sector_store import SectorStore
from strategy import paper_runner
from strategy.gm_v3.models import DailyBar
from strategy.paper_runner import bench_day, load_universe


@pytest.fixture()
def live_db(tmp_path):
    """스키마 생성(SectorStore.open 경로) 후 픽 2개를 심은 임시 trading.db."""
    db = str(tmp_path / "trading.db")

    async def _make():
        store = SectorStore(db)
        await store.open()
        await store.close()

    import asyncio
    asyncio.run(_make())

    now = now_kst()
    future = (now + timedelta(days=7)).isoformat()
    past = (now - timedelta(days=1)).isoformat()
    con = sqlite3.connect(db)
    # pick 1: active, 미만료 — 반도체 2 + 기판 1(반도체와 중복 종목 1 포함)
    con.execute("INSERT INTO sector_picks (id, pick_date, created_at, expires_at,"
                " status, raw_input) VALUES (1, '2026-07-06', ?, ?, 'active', '')",
                (now.isoformat(), future))
    rows1 = [
        (1, "반도체", "005930", "삼성전자", 1),
        (1, "반도체", "000660", "SK하이닉스", 2),
        (1, "기판", "007810", "코리아써키트", 1),
    ]
    # pick 2: active, 미만료 — 반도체 중복 1(dedup 대상) + archived 1(제외 대상)
    con.execute("INSERT INTO sector_picks (id, pick_date, created_at, expires_at,"
                " status, raw_input) VALUES (2, '2026-07-06', ?, ?, 'active', '')",
                (now.isoformat(), future))
    rows2 = [(2, "반도체", "005930", "삼성전자", 1)]
    # pick 3: 만료(expires_at 과거) — 통째로 제외 대상
    con.execute("INSERT INTO sector_picks (id, pick_date, created_at, expires_at,"
                " status, raw_input) VALUES (3, '2026-07-01', ?, ?, 'active', '')",
                (now.isoformat(), past))
    rows3 = [(3, "조선엔진", "082740", "한화엔진", 1)]
    for pick_id, sec, code, name, order in rows1 + rows2 + rows3:
        con.execute(
            "INSERT INTO sector_stocks (pick_id, sector_name, stock_code,"
            " stock_name, added_order) VALUES (?,?,?,?,?)",
            (pick_id, sec, code, name, order))
    # pick 1 에 archived 종목 1개 (tracking_status 필터 검증)
    con.execute(
        "INSERT INTO sector_stocks (pick_id, sector_name, stock_code, stock_name,"
        " added_order, tracking_status) VALUES (1, '반도체', '042700', '한미반도체',"
        " 3, 'archived')")
    con.commit()
    con.close()
    return db


def test_load_universe_live_view(live_db):
    uni = load_universe(live_db)
    pairs = {(s, c) for c, _n, s in uni}
    # active + 미만료 픽의 active 종목만, (섹터,종목) dedup
    assert pairs == {("반도체", "005930"), ("반도체", "000660"),
                     ("기판", "007810")}
    # 만료 픽(조선엔진)과 archived(042700)는 제외
    codes = {c for c, _n, _s in uni}
    assert "082740" not in codes and "042700" not in codes
    # 같은 종목이 두 pick 의 같은 섹터에 있어도 1회만
    assert sum(1 for c, _n, s in uni if (s, c) == ("반도체", "005930")) == 1


def test_load_universe_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")

    async def _make():
        store = SectorStore(db)
        await store.open()
        await store.close()

    import asyncio
    asyncio.run(_make())
    assert load_universe(db) == []


# ---------------- bench_day (오버나이트 포함 정의) ----------------

PREV = date(2026, 7, 3)   # 직전 거래일
D = date(2026, 7, 6)      # 기록일


def _bar(d: date, o: float, c: float) -> DailyBar:
    return DailyBar(day=d, open=o, high=max(o, c), low=min(o, c),
                    close=c, volume=1000.0)


@pytest.fixture()
def bench_con():
    """paper_universe_log 만 있는 in-memory paper con — 전일 멤버십 판정용."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE paper_universe_log ("
                "day TEXT, code TEXT, name TEXT, sector TEXT, recorded_at TEXT,"
                " PRIMARY KEY(day, sector, code))")
    return con


@pytest.fixture()
def fake_daily_cache(monkeypatch):
    cache = {
        # A: 전일 close 100 → 당일 open 105 / close 110 (갭업 +5% 후 +4.76%)
        "A": [_bar(PREV, 98.0, 100.0), _bar(D, 105.0, 110.0)],
        # B: 전일 이력 없음(신규 상장 등) — 당일 open 100 / close 95
        "B": [_bar(D, 100.0, 95.0)],
        # C: 당일 봉 없음 → 제외
        "C": [_bar(PREV, 50.0, 50.0)],
    }
    monkeypatch.setattr(paper_runner, "_daily_cache", cache)
    return cache


def _mark_prev_member(con, *codes):
    for c in codes:
        con.execute("INSERT INTO paper_universe_log VALUES (?,?,?,?,?)",
                    (PREV.isoformat(), c, c, "섹터1", "t"))


def test_bench_new_members_use_open_to_close(bench_con, fake_daily_cache):
    # 전일 로그 없음 → 전 종목 신규 편입 취급: open→close
    universe = [("A", "에이", "섹터1"), ("B", "비", "섹터2"), ("C", "씨", "섹터1")]
    day_ret, n, excluded = bench_day(bench_con, D, universe)
    assert n == 2 and excluded == 1
    assert day_ret == pytest.approx((110 / 105 - 1 + 95 / 100 - 1) / 2)


def test_bench_continuing_member_includes_overnight(bench_con, fake_daily_cache):
    # A 가 전일 멤버 → 전일종가(100)→당일종가(110) = +10% (오버나이트 갭 포함)
    _mark_prev_member(bench_con, "A")
    universe = [("A", "에이", "섹터1"), ("B", "비", "섹터2")]
    day_ret, n, _x = bench_day(bench_con, D, universe)
    assert n == 2
    assert day_ret == pytest.approx((110 / 100 - 1 + 95 / 100 - 1) / 2)


def test_joined_bench_uses_explicit_membership_boundary(
        bench_con, fake_daily_cache):
    # 현재 snapshot 로그에 A가 있어도 corrected 경계에서 신규면 시가→종가여야 한다.
    _mark_prev_member(bench_con, "A")
    day_ret, n, excluded = bench_day(
        bench_con, D, [("A", "에이", "")], prev_members=set())
    assert (n, excluded) == (1, 0)
    assert day_ret == pytest.approx(110 / 105 - 1)


def test_joined_bench_empty_day_carries_zero_return(
        bench_con, fake_daily_cache):
    assert bench_day(
        bench_con, D, [], prev_members={"A"}) == (0.0, 0, 0)


def test_bench_prev_member_without_prev_bar_falls_back(bench_con, fake_daily_cache):
    # B 는 전일 멤버였지만 전일 봉이 없음 → open→close 폴백
    _mark_prev_member(bench_con, "B")
    day_ret, n, _x = bench_day(bench_con, D, [("B", "비", "섹터2")])
    assert n == 1
    assert day_ret == pytest.approx(95 / 100 - 1)


def test_bench_dedups_same_code_across_sectors(bench_con, fake_daily_cache):
    universe = [("A", "에이", "섹터1"), ("A", "에이", "섹터2"),
                ("B", "비", "섹터2")]
    day_ret, n, _x = bench_day(bench_con, D, universe)
    assert n == 2
    assert day_ret == pytest.approx((110 / 105 - 1 + 95 / 100 - 1) / 2)


def test_bench_all_missing_returns_zero(bench_con, fake_daily_cache):
    day_ret, n, excluded = bench_day(bench_con, D, [("C", "씨", "섹터1")])
    assert (day_ret, n, excluded) == (0.0, 0, 1)


# ---------------- v2 공유현금 포트폴리오 ----------------

def test_v2_portfolio_uses_known_strength_caps_and_cash():
    def row(code, sector, strength, ret, entry, exit_):
        return {"code": code, "sector": sector, "signal_strength": strength,
                "ret_net": ret, "entry_time": entry, "exit_time": exit_}

    rows = [
        # 같은 09:30 진입에서 동일 섹터 세 번째 종목(C)은 상한 2 때문에 제외.
        row("A", "AI", 0.10, 0.05, "2026-07-06T09:30:00+09:00",
            "2026-07-06T10:00:00+09:00"),
        row("B", "ai ", 0.09, -0.04, "2026-07-06T09:30:00+09:00",
            "2026-07-06T11:00:00+09:00"),
        row("C", "Ai", 0.08, 0.50, "2026-07-06T09:30:00+09:00",
            "2026-07-06T12:00:00+09:00"),
        row("D", "바이오", 0.07, 0.02, "2026-07-06T09:30:00+09:00",
            "2026-07-06T13:00:00+09:00"),
        # D의 다른 섹터 중복행 — 고유 종목은 한 슬롯만 사용
        row("D", "헬스케어", 0.06, 0.02, "2026-07-06T09:30:00+09:00",
            "2026-07-06T13:00:00+09:00"),
    ]
    day_ret, selected, skipped = paper_runner.v2_portfolio_day(rows)

    assert [r["code"] for r in selected] == ["A", "B", "D"]
    assert skipped == 1
    # 5개 슬롯 중 3개만 사용: 빈 40%는 현금, 미래 ret은 순위에 쓰지 않는다.
    assert day_ret == pytest.approx(
        paper_runner.V2_PORTFOLIO_SLOT_WEIGHT * (0.05 - 0.04 + 0.02))


def test_v2_portfolio_never_selects_more_than_five():
    rows = [
        {"code": f"C{i}", "sector": f"S{i}", "signal_strength": 1 - i / 10,
         "ret_net": 0.01, "entry_time": "2026-07-06T09:30:00+09:00",
         "exit_time": "2026-07-06T15:30:00+09:00"}
        for i in range(7)
    ]
    day_ret, selected, skipped = paper_runner.v2_portfolio_day(rows)

    assert len(selected) == paper_runner.V2_PORTFOLIO_MAX_POSITIONS == 5
    assert skipped == 2
    assert day_ret == pytest.approx(0.01)


def test_v2_portfolio_late_signal_cannot_evict_earlier_trade():
    rows = [
        {"code": "EARLY", "sector": "S1", "signal_strength": 0.05,
         "ret_net": 0.01, "entry_time": "2026-07-06T09:30:00+09:00",
         "exit_time": "2026-07-06T15:30:00+09:00"},
        *[
            {"code": f"F{i}", "sector": f"S{i+2}", "signal_strength": 0.04 - i / 100,
             "ret_net": 0.0, "entry_time": "2026-07-06T09:31:00+09:00",
             "exit_time": "2026-07-06T15:30:00+09:00"}
            for i in range(4)
        ],
        {"code": "LATE", "sector": "S9", "signal_strength": 0.99,
         "ret_net": 0.50, "entry_time": "2026-07-06T10:00:00+09:00",
         "exit_time": "2026-07-06T15:30:00+09:00"},
    ]
    _ret, selected, skipped = paper_runner.v2_portfolio_day(rows)
    assert [r["code"] for r in selected] == ["EARLY", "F0", "F1", "F2", "F3"]
    assert skipped == 1


def test_v2_portfolio_reuses_slot_after_exit():
    rows = [
        {"code": "A", "sector": "S1", "signal_strength": 0.1,
         "ret_net": 0.10, "entry_time": "2026-07-06T09:30:00+09:00",
         "exit_time": "2026-07-06T10:00:00+09:00"},
        {"code": "B", "sector": "S2", "signal_strength": 0.1,
         "ret_net": -0.05, "entry_time": "2026-07-06T10:01:00+09:00",
         "exit_time": "2026-07-06T11:00:00+09:00"},
    ]
    day_ret, selected, skipped = paper_runner.v2_portfolio_day(rows)
    assert [r["code"] for r in selected] == ["A", "B"]
    assert skipped == 0
    assert day_ret == pytest.approx(0.2 * 0.10 + 0.2 * -0.05)


def test_v2_portfolio_does_not_reuse_exit_cash_at_same_bar_timestamp():
    rows = [
        {
            "code": f"OPEN{i}", "sector": f"S{i}", "signal_strength": 0.2,
            "ret_net": 0.0, "entry_time": "2026-07-06T09:30:00+09:00",
            "exit_time": "2026-07-06T10:00:00+09:00",
        }
        for i in range(5)
    ]
    rows.append({
        "code": "NEW", "sector": "S9", "signal_strength": 0.99,
        "ret_net": 0.5, "entry_time": "2026-07-06T10:00:00+09:00",
        "exit_time": "2026-07-06T11:00:00+09:00",
    })

    day_ret, selected, skipped = paper_runner.v2_portfolio_day(rows)

    assert [r["code"] for r in selected] == [
        "OPEN0", "OPEN1", "OPEN2", "OPEN3", "OPEN4"]
    assert skipped == 1
    assert day_ret == pytest.approx(0.0)


def test_portfolio_allocations_are_replaceable_audit_rows():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE paper_portfolio_allocations ("
        "day TEXT,strategy TEXT,code TEXT,name TEXT,sector TEXT,"
        "entry_time TEXT,exit_time TEXT,weight REAL,ret_net REAL,pnl REAL,"
        "detail TEXT,recorded_at TEXT,"
        "PRIMARY KEY(day,strategy,code,entry_time))")
    d = date(2026, 7, 6)
    rows = [{
        "code": "A", "name": "에이", "sector": "AI",
        "entry_time": "2026-07-06T09:30:00+09:00",
        "exit_time": "2026-07-06T10:00:00+09:00",
        "weight": 0.2, "ret_net": 0.05, "pnl": 0.01, "detail": "1TP/BE",
    }]
    paper_runner._replace_portfolio_allocations(
        con, d, "v2_portfolio", rows, "recorded")
    paper_runner._replace_portfolio_allocations(
        con, d, "v2_portfolio", rows, "recorded-again")

    saved = con.execute(
        "SELECT code,weight,pnl,recorded_at FROM paper_portfolio_allocations"
    ).fetchall()
    assert saved == [("A", 0.2, 0.01, "recorded-again")]


def test_paper_conn_creates_portfolio_audit_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_runner, "PAPER_DB", tmp_path / "paper.db")
    con = paper_runner.paper_conn()
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "paper_portfolio_allocations" in tables
    finally:
        con.close()


# ---------------- v4r 관찰 축 (2026-07-19) ----------------

def test_run_v4r_replay_wiring(monkeypatch):
    """run_v4r_replay: dict 변환·EOR 비용(편도)·removed 종목 end 캡 검증."""
    from backtest.run_premarket_pullback import Trade as BTrade

    calls: list[tuple] = []

    def fake_backtest_symbol(cache, code, name, start, end, *, mode, **params):
        calls.append((code, start, end, mode))
        if code == "AAA":
            return [
                BTrade("AAA", name, date(2026, 7, 7), 100, 110, 1000, 1050,
                       "2TP/BE", 0.05, exit_day=date(2026, 7, 8)),
                BTrade("AAA", name, date(2026, 7, 9), 100, 110, 1000, 1010,
                       "1TP/EOR", 0.01, exit_day=date(2026, 7, 10)),
                # 당일 재진입 2건 — 같은 (code, 진입일, 청산일)이지만
                # entry_time 으로 PK 유니크 (리뷰 F1)
                BTrade("AAA", name, date(2026, 7, 10), 100, 110, 1000, 1050,
                       "1TP/BE", 0.05, exit_day=date(2026, 7, 10),
                       entry_time="2026-07-10T09:30:00+09:00"),
                BTrade("AAA", name, date(2026, 7, 10), 100, 110, 1020, 980,
                       "0TP/SL", -0.04, exit_day=date(2026, 7, 10),
                       entry_time="2026-07-10T11:00:00+09:00"),
            ]
        return []

    monkeypatch.setattr(paper_runner, "backtest_symbol", fake_backtest_symbol)
    monkeypatch.setattr(paper_runner, "_cache_conn", lambda: type(
        "C", (), {"close": lambda self: None})())

    uni = [("AAA", "에이", "섹터1"), ("BBB", "비", "섹터2")]
    removed = [("CCC", "씨", date(2026, 7, 8))]
    rows = paper_runner.run_v4r_replay(date(2026, 7, 6), date(2026, 7, 10),
                                       uni, removed)

    # 호출: 현재 유니버스는 end=today, removed 는 end=제거일 캡
    ends = {c: e for c, _s, e, _m in calls}
    assert ends["AAA"] == date(2026, 7, 10)
    assert ends["CCC"] == date(2026, 7, 8)
    assert all(m == "v4r" for _c, _s, _e, m in calls)

    real = [r for r in rows if not r["eor"]]
    eor = [r for r in rows if r["eor"]]
    assert len(real) == 3 and len(eor) == 1
    # 실청산 = 왕복 비용, EOR = 편도 비용 (gm_v3 동일 규약)
    assert real[0]["ret_net"] == pytest.approx(0.05 - 2 * paper_runner.COST_PER_SIDE)
    assert eor[0]["ret_net"] == pytest.approx(0.01 - 1 * paper_runner.COST_PER_SIDE)
    assert real[0]["closed_on"] == date(2026, 7, 8)   # exit_day 사용(오버나이트)
    assert eor[0]["detail"].endswith("EOR")           # 알림 EOR 필터와 호환
    # 당일 재진입 2건: PK 키(code, opened_on, closed_on)가 서로 달라야 함 (F1)
    keys = {(r["code"], str(r["opened_on"]), str(r["closed_on"])) for r in rows}
    assert len(keys) == len(rows)


def test_membership_windows_use_append_only_events_and_split_reentry(tmp_path):
    """snapshot 결측을 추측하지 않고 이벤트 시각을 보수적 거래일 경계로 바꾼다."""
    db = tmp_path / "trading.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE universe_membership_events ("
        "id INTEGER PRIMARY KEY,occurred_at TEXT,action TEXT,"
        "stock_code TEXT,stock_name TEXT,source TEXT)")
    con.executemany(
        "INSERT INTO universe_membership_events VALUES (?,?,?,?,?,?)",
        [
            (1, "2026-07-06T07:00:00+09:00", "activate", "A", "에이", "test"),
            (2, "2026-07-06T09:00:00+09:00", "activate", "B", "비", "test"),
            (3, "2026-07-08T10:00:00+09:00", "deactivate", "A", "에이", "test"),
            (4, "2026-07-09T09:00:00+09:00", "activate", "A", "에이새이름", "test"),
        ],
    )
    con.commit()
    con.close()

    d1, d2, d4, d5 = (
        date(2026, 7, 6), date(2026, 7, 7),
        date(2026, 7, 9), date(2026, 7, 10))
    windows = paper_runner.load_membership_windows(str(db), d1, d5)
    assert windows == [
        ("A", "에이", d1, d2),
        ("B", "비", d2, d5),
        ("A", "에이새이름", d5, d5),
    ]


def test_membership_windows_missing_event_table_is_unavailable(tmp_path):
    db = tmp_path / "legacy.db"
    sqlite3.connect(db).close()
    assert paper_runner.load_membership_windows(
        str(db), date(2026, 7, 6), date(2026, 7, 10)) is None


def test_membership_universe_uses_same_conservative_day_boundary():
    d1, d2, d3 = date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)
    windows = [
        ("A", "에이", d1, d2),
        ("B", "비", d2, d3),
    ]

    assert paper_runner.membership_universe_on(windows, d1) == [
        ("A", "에이", "")]
    assert paper_runner.membership_universe_on(windows, d2) == [
        ("A", "에이", ""), ("B", "비", "")]
    assert paper_runner.membership_universe_on(windows, d3) == [
        ("B", "비", "")]


def test_missing_data_skips_when_corrected_universe_is_still_active():
    joined = [("A", "에이", "")]
    assert paper_runner._must_skip_for_missing_market_data(
        False, [], joined) is True


def test_missing_data_allows_carry_only_when_both_universes_are_empty():
    assert paper_runner._must_skip_for_missing_market_data(
        False, [], []) is False


def test_v4r_membership_windows_bound_each_replay(monkeypatch):
    """v4r은 종목의 실제 활성 구간별 start/end만 backtest에 넘긴다."""
    calls = []

    def fake_backtest(cache, code, name, start, end, *, mode, **params):
        calls.append((code, name, start, end, mode))
        return []

    monkeypatch.setattr(paper_runner, "backtest_symbol", fake_backtest)
    monkeypatch.setattr(paper_runner, "_cache_conn", lambda: type(
        "C", (), {"close": lambda self: None})())
    d1, d2, d4 = date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 9)
    windows = [("A", "에이", d1, d2), ("A", "에이", d4, d4)]

    paper_runner.run_v4r_replay(
        d1, d4, [("A", "에이", "섹터")],
        membership_windows=windows)

    assert calls == [
        ("A", "에이", d1, d2, "v4r"),
        ("A", "에이", d4, d4, "v4r"),
    ]


# ---------------- gm_v3 변형 축 (GM3_VARIANTS, 2026-07-11) ----------------

def test_gm3_replay_cfg_variant_changes_result(monkeypatch):
    """run_gm3_replay 가 cfg 주입을 존중하는지 — R13 켠 변형은 지지레벨 매수로
    베이스에 없는 트레이드를 만든다 (변형 축 배선 회귀 방지)."""
    from dataclasses import replace as dc_replace

    from strategy.gm_v3.config import GmV3Config
    from strategy.gm_v3.synth import make_bars

    rows = ([(10000, 10100, 9900, 10000, 200)] * 5           # 워밍업 (20봉 하한 충족)
            + [(10000, 10100, 9900, 10000, 200),
               (10100, 10850, 10050, 10800, 200),
               (10800, 11650, 10750, 11600, 200),
               (11600, 12550, 11550, 12500, 200),
               (12500, 13550, 12450, 13500, 200),
               (13500, 14550, 13450, 14500, 200),
               (14500, 15650, 14450, 15600, 200),
               (15600, 16850, 15550, 16800, 200),
               (16800, 18050, 16750, 18000, 200),
               (18000, 19350, 17950, 19300, 200),
               (19300, 20000, 19250, 19900, 200)]            # 상승 파동 (고점 20000)
            + [(19900, 19950, 18900, 19000, 100),
               (19000, 19050, 18300, 18400, 100),
               (18400, 18450, 17800, 17900, 100),
               (17900, 17950, 17500, 17600, 100),
               (16900, 17500, 16850, 17400, 100)]            # 눌림 → 되돌림30% 지지 양봉
            + [(17400, 17500, 17300, 17450, 100),
               (17450, 17500, 17350, 17400, 100)])           # 체결·EOR 마감용
    bars = make_bars(rows)
    monkeypatch.setattr(paper_runner, "_daily_cache", {"X": bars})
    uni = [("X", "테스트", "섹터1")]

    base = paper_runner.run_gm3_replay(bars[0].day, bars[-1].day, uni)
    r13 = paper_runner.run_gm3_replay(
        bars[0].day, bars[-1].day, uni,
        cfg=dc_replace(GmV3Config(), r13_enabled=True).validated())
    assert len(base) == 0          # 베이스는 이 시나리오에서 무거래
    assert len(r13) == 1           # R13 지지레벨 매수 → EOR 스냅샷 1건
    assert "GM3_VARIANTS" in dir(paper_runner)
    assert [s for s, _f in paper_runner.GM3_VARIANTS] == [
        "gm_v3", "gm_v3_r13", "gm_v3_r14", "gm_v3_r13r14"]


def test_gm3_membership_windows_become_action_bounds(monkeypatch):
    """gm_v3 워밍업은 유지하되 신호/체결은 실제 편입~이탈 구간으로 제한한다."""
    calls = []
    bars = [_bar(date(2026, 6, 1) + timedelta(days=i), 100, 100)
            for i in range(40)]

    def fake_simulate(code, actual_bars, cfg, *, fill_mode, act_from, act_to):
        calls.append((code, fill_mode, act_from, act_to, len(actual_bars)))
        return [], []

    monkeypatch.setattr(paper_runner, "_daily_cache", {"A": bars})
    monkeypatch.setattr(paper_runner, "simulate", fake_simulate)
    d1, d2, d4 = date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 9)
    windows = [("A", "에이", d1, d2), ("A", "에이", d4, d4)]

    paper_runner.run_gm3_replay(
        d1, d4, [("A", "에이", "섹터")],
        membership_windows=windows)

    assert calls == [
        ("A", "next_open", d1, d2, 40),
        ("A", "next_open", d4, d4, 40),
    ]
