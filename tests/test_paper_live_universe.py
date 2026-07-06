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
