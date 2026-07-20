"""모의투자(paper) 일일 하네스 — 3전략 + 벤치마크 forward 기록.

운영 헌장(우선순위 2) 구현:
  매 거래일 16:00 이후 실행되어 아래 4개를 db/paper.db(WAL)에 기록한다.
    v2          — 프리장 급등→눌림 지지·다지기→아침고점 재돌파 (당일 스캘핑)
    v2_leader   — v2 + 주도섹터 필터(신호일 d-1 기준 최근 5거래일 수익률 1위 섹터만)
    gm_v3       — 멘토 룰엔진 R1~R12 (일봉 스윙, 다음날 시가 체결)
    gm_v3_r13 / gm_v3_r14 / gm_v3_r13r14 — Tier1 변형 축 (GM3_VARIANTS, 07-11)
    v4r         — v4재폭등 관찰 축 (07-19): 국소 기준선+재진입+오버나이트,
                  애프터 진입 제외(--no-after 상당). 채택 아님, forward 관찰 전용
    bench_bh    — 당일 등록 유니버스 동일가중 (시가→종가, 무비용 기준선)

명시적 체결/비용 가정 (paper_meta 에 스탬프):
  - 비용 0.25%/편도(왕복 0.5%). v2 트레이드는 ret-0.005, gm_v3 는 realized
    - 0.005×max_invested. 벤치마크는 무비용 기준선(비용 미차감).
  - v2 체결 = 당일 3분봉 실측가(백테스트 로직 그대로), gm_v3 = 다음날 시가
    (R10 손절만 당일 스탑가).
  - 벤치마크 = 당일 유니버스 동일가중: 연속 등록 종목은 전일종가→당일종가
    (오버나이트 포함), 신규 편입 종목은 당일 시가→종가. 일수익 직렬 체인.
  - 애프터 급변 취소 / 프리장 갭 보류 규칙은 미반영(보수적 미확정분).
  - 자산곡선 = 청산 순 직렬 복리(포트폴리오 병렬 회계 아님 — 백테스트 평가와
    동일 방식, 벤치마크와 상대 비교 목적).

데이터: 토스 1분봉(당일분 매일 캐시 적재) + gm_v3 워밍업은 KIS 일봉 보충.
유니버스: trading.db 라이브 조회 — active(미만료) pick × tracking_status='active'
  종목, (섹터,종목) dedup. 주의: 웹앱 /api/picks 는 tracking_status 를 필터하지
  않으므로(archived 표시됨) 화면과 1:1 은 아니다. 픽 등록/교체는 반드시 이 기기
  (모의투자 지정 기기)의 웹앱에서 한다 — trading.db 는 gitignore(기기 로컬)라
  다른 기기에서 등록한 픽은 여기 오지 않는다.
  2026-07-06 운영 전환(A안): 동결 스냅샷(universe_snapshot.json) 사용 종료.

결측일/부분기록 처리 (record_upto):
  - 기록 사이에 빠진 거래일은 오래된 날부터 자동 소급 기록(유니버스는 현재
    라이브 — 기기가 꺼져 있었다면 픽도 못 바꿨으므로 사실상 동일).
  - finalized=0(장중 임시 스냅샷) 행은 다음 기록 전에 재확정한다. 20:05 이후
    또는 과거일 기록만 finalized=1.

사용 (반드시 -m 로 — strategy/signal.py 가 stdlib signal 을 가리므로 직접 실행 금지.
      출력 한글 깨짐 방지: $env:PYTHONIOENCODING='utf-8' 접두):
  ./.venv/Scripts/python.exe -m strategy.paper_runner --init 2026-07-06   # 1회
  ./.venv/Scripts/python.exe -m strategy.paper_runner                     # 당일 기록
  ./.venv/Scripts/python.exe -m strategy.paper_runner --day 2026-07-06    # 특정일
  ./.venv/Scripts/python.exe -m strategy.paper_runner --report            # 현황 조회
  ./.venv/Scripts/python.exe -m strategy.paper_runner --market-schedule   # 상주 루프
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time as time_mod
from dataclasses import replace as dc_replace
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from backtest.run_premarket_pullback import (  # noqa: E402
    _cache_conn, _ensure_cached, _load_bars, backtest_symbol,
)
from backtest.toss_client import TossClient  # noqa: E402
from config import settings  # noqa: E402
from core.market_calendar import is_trading_day  # noqa: E402
from core.market_schedule import next_action  # noqa: E402
from core.time_utils import now_kst, to_db_iso  # noqa: E402
from strategy.paper_notify import fmt_outperf, notify_events  # noqa: E402
from strategy.gm_v3.config import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import (  # noqa: E402
    kis_backfill_daily, load_daily_from_toss,
)
from strategy.gm_v3.models import DailyBar  # noqa: E402
from strategy.gm_v3.paper import simulate  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_DB = PROJECT_ROOT / "db" / "paper.db"

COST_PER_SIDE = 0.0025          # 0.25%/편도 (왕복 0.5%)
GM3_WARMUP_DAYS = 90            # 지표 워밍업용 과거 일봉(달력 아님, 거래일 여유)

# gm_v3 변형 축 (2026-07-11 오너 지시) — Tier1 백테스트 채택 후보를 forward 에서
# 병행 관찰. 이름 = paper_trades/paper_daily 의 strategy 값. 기존 gm_v3 축은 불변.
# 백테스트 근거(1~7월 71종목): +R13 거래 2배·기대값 유지 / +R14 중립 / 조합은 R13 단독보다 열위.
GM3_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("gm_v3", {}),
    ("gm_v3_r13", {"r13_enabled": True}),
    ("gm_v3_r14", {"r14_enabled": True}),
    ("gm_v3_r13r14", {"r13_enabled": True, "r14_enabled": True}),
)

V2_PARAMS = dict(pre_surge=0.05, pullback_min=0.03, support_tol=0.005,
                 tp_levels=(0.05, 0.10, 0.15, 0.20, 0.25), stop_pct=0.04,
                 consol_bars=3)

# v4r 관찰 축 (2026-07-19 오너 지시) — A/B 판정(PROJECT_HANDOFF 07-17)에서
# 기각된 애프터장 진입을 뺀 정제형(--no-after 상당). 국소 기준선 + 재진입≤4 +
# 승자 게이트 + 오버나이트 무기한. 채택 아님 — forward 관찰 전용.
V4R_PARAMS = dict(**V2_PARAMS, max_entries=4, use_after=False, winner_gate=True)

REGIME = "live_universe_v1"     # 2026-07-06 운영 전환(A안). 정의 변경 시 v2 로 올릴 것.

ASSUMPTIONS = {
    "regime": REGIME,
    "cost_per_side": COST_PER_SIDE,
    "v2_fill": "당일 3분봉 실측가 (백테스트 로직 동일)",
    "gm3_fill": "next_open (R10 손절만 당일 스탑가)",
    "gm3_open_positions": "미청산 포지션은 EOR(MTM) 행으로 equity에 반영, "
                          "청산 비용은 실제 청산 시에만 차감. n_trades 는 실청산만 집계",
    "bench_day": "당일 유니버스(종목 dedup) 동일가중, 무비용 기준선. "
                 "연속 등록 종목=전일종가→당일종가(오버나이트 포함), "
                 "신규 편입=당일 시가→종가. equity 는 일수익 직렬 체인(레짐 필터)",
    "equity": "청산순 직렬 복리 (포트폴리오 병렬 회계 아님)",
    "after_hours_rules": "애프터 급변 취소/프리장 갭 보류 미반영(보수적)",
    "v2_params": {k: (list(v) if isinstance(v, tuple) else v)
                  for k, v in V2_PARAMS.items()},
    "universe": "trading.db 라이브(active 미만료 pick × active tracking, "
                "이 기기 웹앱에서 등록) — 당일 유니버스는 paper_universe_log 감사 기록. "
                "결측일 소급 기록 시에도 현재 라이브 유니버스 사용(기기 꺼짐=픽 불변)",
    "gm3_universe": "리플레이 = 현재 유니버스 + 과거 제외 종목(제외일까지 act, "
                    "그 시점 EOR 동결) — 웹앱 제거로 과거 손실이 소멸하는 "
                    "생존편향 채널 차단",
    "v4r": "관찰 축(채택 아님): v2+국소 스윙 기준선+재진입≤4+승자 게이트+"
           "오버나이트 무기한, 애프터 진입 제외. 전체 리플레이 멱등, "
           "removed 는 제거일까지. EOR 은 편도 비용·실청산 집계 제외 (gm_v3 동일). "
           "한계: 분봉 캐시는 종목당 편입-12일부터라(ensure_day_cached lookback) "
           "중도 편입 종목의 그 이전 구간은 리플레이에서 빠짐. opened_on 은 "
           "진입 봉 ISO 시각(당일 재진입 PK 유니크). 축 도입 첫 기록일 day_ret "
           "에는 paper_start 이후 소급 손익이 일괄 반영됨",
    "finalized": "finalized=1 행만 확정치(20:05 이후 또는 과거일 기록). "
                 "finalized=0 은 장중 임시 스냅샷 — 다음 기록 전 자동 재확정",
}


# ---------------- DB ----------------

def paper_conn() -> sqlite3.Connection:
    con = sqlite3.connect(PAPER_DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("CREATE TABLE IF NOT EXISTS paper_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_trades ("
        " strategy TEXT NOT NULL, code TEXT NOT NULL, name TEXT,"
        " opened_on TEXT NOT NULL, closed_on TEXT NOT NULL,"
        " ret_gross REAL NOT NULL, ret_net REAL NOT NULL,"
        " detail TEXT, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY(strategy, code, opened_on, closed_on))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_daily ("
        " day TEXT NOT NULL, strategy TEXT NOT NULL,"
        " n_trades INTEGER NOT NULL, day_ret REAL NOT NULL,"
        " equity REAL NOT NULL, note TEXT, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY(day, strategy))"
    )
    # 당일 유니버스 감사 로그 — 동적 벤치마크 재현/검증용 (라이브 전환 후 필수)
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_universe_log ("
        " day TEXT NOT NULL, code TEXT NOT NULL, name TEXT,"
        " sector TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY(day, sector, code))"
    )
    # 라이브 전환 마이그레이션: 레짐 스플라이스 가드 + 임시/확정 구분
    _ensure_column(con, "paper_daily", "regime", "TEXT DEFAULT ''")
    _ensure_column(con, "paper_daily", "finalized", "INTEGER DEFAULT 0")
    # 텔레그램 팩트 알림 중복 차단 (5분 재기록/재시작에도 이벤트당 1회)
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_notified ("
        " key TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,"
        " sent_at TEXT NOT NULL)"
    )
    return con


def _ensure_column(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _stamp_meta(con: sqlite3.Connection) -> None:
    """현행 가정/레짐 스탬프 + 레짐별 아카이브(전환 후에도 이전 가정이 남도록)."""
    dumped = json.dumps(ASSUMPTIONS, ensure_ascii=False)
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('assumptions', ?)", (dumped,))
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('regime', ?)", (REGIME,))
    con.execute("INSERT OR IGNORE INTO paper_meta VALUES (?, ?)",
                (f"assumptions@{REGIME}", dumped))


# ---------------- 유니버스 (trading.db 라이브 — 웹앱 등록 뷰) ----------------

def load_universe(db_path: str | None = None) -> list[tuple[str, str, str]]:
    """[(code, name, sector), ...] — trading.db 라이브 조회 (읽기 전용).

    필터 = active pick(미만료) × tracking_status='active' 종목.
    SectorStore.get_active_picks 와 같은 뷰지만 의도적으로 raw SELECT 를 쓴다:
      - 쓰기 없음 (get_active_picks 는 expire_old_picks UPDATE 를 동반 —
        5분 주기 상주 루프가 trading.db 에 쓰는 것을 피한다)
      - SectorStore.open() 의 DDL/마이그레이션 프로브 없음
      - 만료는 expires_at 비교로 동일하게 걸러짐 (status 플립은 알림봇/웹앱 몫)
    참고: 웹앱 /api/picks 는 tracking_status 를 필터하지 않는다(archived 도 표시)
    — 여기서는 제외하는 것이 기록 목적에 맞다.
    (섹터, 종목) 단위 dedup — 같은 종목이 두 섹터에 있으면 둘 다 유지.
    """
    path = str(db_path or settings.DB_PATH)
    con = sqlite3.connect(path, timeout=15)
    try:
        rows = con.execute(
            "SELECT ss.stock_code, ss.stock_name, ss.sector_name "
            "FROM sector_stocks ss JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sp.status='active' AND sp.expires_at > ? "
            "AND COALESCE(ss.tracking_status, 'active') = 'active' "
            "ORDER BY sp.created_at DESC, ss.added_order",
            (to_db_iso(now_kst()),)).fetchall()
    finally:
        con.close()

    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for code, name, sector in rows:
        key = (sector, code)
        if key in seen:
            continue
        seen.add(key)
        out.append((code, name, sector))
    n_codes = len({c for c, _n, _s in out})
    n_sectors = len({s for _c, _n, s in out})
    logger.info("[paper][universe] 라이브 로드: {}종목({}코드 dedup) / {}섹터",
                len(out), n_codes, n_sectors)
    return out


# ---------------- 데이터 준비 ----------------

def ensure_day_cached(day: date, codes: list[str], *, lookback_days: int = 12) -> None:
    """[day-lookback, day] 중 캐시에 없는 날짜만 토스에서 받아 적재(증분·멱등).

    lookback 이유: v2 전일종가 + 주도섹터 5거래일 수익률에 과거 일봉 필요.
    정착 후에는 매일 1일치만 추가 수집된다(주말/휴장일은 0봉으로 마킹).
    """
    win_start = day - timedelta(days=lookback_days)
    # 당일을 20:05(애프터 종료+버퍼) 전에 받으면 불완전할 수 있음 → 항상 재수집
    # 하고 완료 마커를 남기지 않는다 (부분 수집 영구 고착 방지, M1).
    now = now_kst()
    day_incomplete = (day == now.date() and now.time() < dtime(20, 5))
    con = _cache_conn()
    with TossClient() as client:
        for code in codes:
            have = {r[0][:10] for r in con.execute(
                "SELECT DISTINCT substr(ts,1,10) FROM candles "
                "WHERE symbol=? AND ts>=? AND ts<=?",
                (code, win_start.isoformat(), day.isoformat() + "T99"))}
            done = {r[0] for r in con.execute(
                "SELECT start FROM fetched WHERE symbol=? AND start=end "
                "AND start>=? AND start<=?",
                (code, win_start.isoformat(), day.isoformat()))}
            d = win_start
            while d <= day:
                ds = d.isoformat()
                force = day_incomplete and d == day
                if force:
                    con.execute("DELETE FROM fetched WHERE symbol=? AND start=? AND end=?",
                                (code, ds, ds))
                    con.commit()
                    # 당일 불완전분: 이미 일부 있으면 tail 만 증분 수집 (상주 루프가
                    # 5분마다 당일 전체를 재다운로드하지 않도록 — 리뷰 F6)
                    last = con.execute(
                        "SELECT MAX(ts) FROM candles WHERE symbol=? AND ts LIKE ?",
                        (code, ds + "T%")).fetchone()[0]
                    if last:
                        bars = client.fetch_1m_since(code, datetime.fromisoformat(last))
                        if bars:
                            con.executemany(
                                "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)",
                                [(code, b.ts.isoformat(), b.open, b.high, b.low,
                                  b.close, b.volume) for b in bars])
                            con.commit()
                        d += timedelta(days=1)
                        continue
                if force or (ds not in have and ds not in done):
                    _ensure_cached(con, client, code, d, d)
                    if force:   # 불완전 수집 — 다음 실행에서 다시 받도록 마커 제거
                        con.execute("DELETE FROM fetched WHERE symbol=? AND start=? AND end=?",
                                    (code, ds, ds))
                        con.commit()
                d += timedelta(days=1)
    con.close()


_daily_cache: dict[str, list[DailyBar]] = {}
# KIS 워밍업 백필은 불변 과거 데이터 — 프로세스 수명 캐시 (_daily_cache.clear() 의
# 영향을 받지 않아, 상주 루프가 5분마다 KIS 를 재호출하지 않는다 — 리뷰 F6)
_kis_warmup_cache: dict[str, list[DailyBar]] = {}


def daily_bars(code: str) -> list[DailyBar]:
    """정규장 일봉(토스 캐시 합성 + 필요 시 KIS 워밍업 보충)."""
    if code in _daily_cache:
        return _daily_cache[code]
    bars = load_daily_from_toss(code)
    if bars:
        first = bars[0].day
        have = len(bars)
        if have < GM3_WARMUP_DAYS:
            if code not in _kis_warmup_cache:
                _kis_warmup_cache[code] = asyncio.run(
                    kis_backfill_daily(code, first, GM3_WARMUP_DAYS - have))
            # 토스 이력과 겹치는 날은 토스 쪽 우선 (경계 중복 방지)
            back = [b for b in _kis_warmup_cache[code] if b.day < first]
            bars = back + bars
    _daily_cache[code] = bars
    return bars


# ---------------- 전략 실행 ----------------

def _leader_sector(universe, day: date) -> str | None:
    """day 직전(d-1)까지 최근 5거래일 수익률 1위 섹터 (사전 정보만 사용)."""
    perf: dict[str, list[float]] = {}
    for code, _name, sector in universe:
        bars = [b for b in daily_bars(code) if b.day < day]
        if len(bars) < 6:
            continue
        r = bars[-1].close / bars[-6].close - 1
        perf.setdefault(sector, []).append(r)
    if not perf:
        return None
    return max(perf.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[0]


def run_v2_for_day(day: date, universe) -> list[dict]:
    """v2 트레이드(당일) — 백테스트 로직 재사용. 반환: dict 리스트."""
    cache = _cache_conn()
    out: list[dict] = []
    for code, name, sector in universe:
        trades = backtest_symbol(cache, code, name, day, day,
                                 mode="v2", **V2_PARAMS)
        for t in trades:
            out.append({"code": code, "name": name, "sector": sector,
                        "day": t.day, "ret_gross": t.ret,
                        "ret_net": t.ret - 2 * COST_PER_SIDE,
                        "entry": t.entry, "exit": t.exit,   # 알림용 진입/청산가
                        "reason": t.reason, "detail": t.reason})
    cache.close()
    return out


def run_v4r_replay(paper_start: date, today: date, universe,
                   removed: list[tuple[str, str, date]] = ()) -> list[dict]:
    """v4r 전체 리플레이(결정적·멱등) — 오버나이트 멀티데이라 gm_v3 처럼
    매일 [paper_start, act_to] 를 통째로 재계산한다.

    removed = 과거 유니버스에 있었다 제거된 종목: 제거일까지만 시뮬레이션
    (end=min(제거일, today)) — 웹앱 제거로 과거 손실이 소멸하는 생존편향 차단.
    EOR(범위 끝 미청산 = 보유 중 MTM)은 gm_v3 와 동일하게 진입 비용(편도)만
    차감하고 equity 에 반영, 실청산 집계에서는 제외한다.
    """
    cache = _cache_conn()
    targets: dict[str, tuple[str, date]] = {}
    for code, name, _sector in universe:
        targets.setdefault(code, (name, today))
    for code, name, last_day in removed:
        targets.setdefault(code, (name, min(last_day, today)))
    out: list[dict] = []
    for code, (name, end_d) in targets.items():
        trades = backtest_symbol(cache, code, name, paper_start, end_d,
                                 mode="v4r", **V4R_PARAMS)
        for t in trades:
            eor = t.reason.endswith("EOR")
            sides = 1 if eor else 2
            # opened_on = 진입 봉 시각(ISO datetime) — 당일 재진입 트레이드가
            # paper_trades PK(strategy,code,opened_on,closed_on)에서 서로
            # 덮어쓰지 않게 유니크화 (리뷰 F1). 폴백은 진입일.
            out.append({"code": code, "name": name, "eor": eor,
                        "opened_on": t.entry_time or t.day,
                        "closed_on": t.exit_day or t.day,
                        "ret_gross": t.ret,
                        "ret_net": t.ret - sides * COST_PER_SIDE,
                        "detail": t.reason})   # EOR 은 reason 자체에 포함됨
    cache.close()
    return out


def run_gm3_replay(paper_start: date, today: date, universe,
                   removed: list[tuple[str, str, date]] = (),
                   cfg: GmV3Config | None = None) -> list[dict]:
    """gm_v3 전체 리플레이(결정적) — act 윈도우 [paper_start, act_to].

    상태를 DB에 영속하지 않고 매일 데이터에서 재구성 → 멱등.
    removed = [(code, name, 마지막 등록일)] — 과거 유니버스에 있었다 제외된 종목.
    제외일까지 act 하고 그 시점 EOR 로 동결한다: 웹앱에서 종목을 지워도 과거
    손실 트레이드가 리플레이에서 소멸하지 않는다 (생존편향 차단, 리뷰 F4).
    코드 단위 dedup — 두 섹터에 걸친 종목이 두 번 리플레이되지 않게.
    cfg 로 룰 토글 변형(GM3_VARIANTS)을 주입한다 — None 이면 기본 gm_v3.
    """
    cfg = cfg if cfg is not None else GmV3Config()
    targets: dict[str, tuple[str, date]] = {}
    for code, name, _sector in universe:
        targets.setdefault(code, (name, today))
    for code, name, last_day in removed:
        targets.setdefault(code, (name, min(last_day, today)))
    out: list[dict] = []
    skipped: list[str] = []
    for code, (name, act_to) in targets.items():
        bars = daily_bars(code)
        if len(bars) < 20:
            skipped.append(code)
            continue
        trades, _sigs = simulate(code, bars, cfg, fill_mode="next_open",
                                 act_from=paper_start, act_to=act_to)
        for t in trades:
            inv = min(t.max_invested, 1.0)   # 방어적 캡 (L5)
            # EOR = 아직 열린 포지션의 MTM 스냅샷 — 청산 비용은 실제 청산 시에만.
            sides = 1 if t.forced_eor else 2
            out.append({"code": code, "name": name, "eor": t.forced_eor,
                        "opened_on": t.opened_on, "closed_on": t.closed_on,
                        "ret_gross": t.realized,
                        "ret_net": t.realized - sides * COST_PER_SIDE * inv,
                        "detail": ",".join(t.exit_rules) + ("|EOR" if t.forced_eor else "")})
    if skipped:
        logger.warning("[paper][gm_v3] 일봉 부족으로 제외된 종목 {}개: {}",
                       len(skipped), ",".join(skipped))
    return out


def bench_day(con: sqlite3.Connection, day: date, universe) -> tuple[float, int, int]:
    """당일 유니버스 동일가중 일수익: (day_ret, 반영 종목수, 제외 종목수).

    2026-07-06 운영 전환(A안) 재정의 + 리뷰 F1 반영:
      - 매일 유니버스가 바뀌므로(오너가 웹앱에서 등록/교체) 고정 진입점 B&H 가
        성립하지 않음 → 일수익을 직렬 체인한다.
      - 연속 등록 종목(전 기록일 paper_universe_log 에 존재) = 전일종가→당일종가
        — 오버나이트 갭 포함. 시가→종가만 쓰면 오버나이트를 먹는 스윙(gm_v3)
        대비 벤치가 체계적으로 과소평가되는 편향이 있었다.
      - 신규 편입 종목 = 당일 시가→종가 (그날 처음 살 수 있으므로).
      - 종목 단위 dedup: 같은 종목이 두 섹터에 있어도 벤치에는 1회만.
      - 무비용 기준선. 당일 봉 없는 종목(거래정지 등)은 그날 제외.
    equity 체인은 record_day 에서 _prev_equity × (1+day_ret) 로 잇는다.
    """
    prev_log_day = con.execute(
        "SELECT MAX(day) FROM paper_universe_log WHERE day<?",
        (day.isoformat(),)).fetchone()[0]
    prev_members: set[str] = set()
    if prev_log_day:
        prev_members = {r[0] for r in con.execute(
            "SELECT DISTINCT code FROM paper_universe_log WHERE day=?",
            (prev_log_day,))}

    rets: list[float] = []
    excluded = 0
    for code in {c for c, _n, _s in universe}:
        bars = daily_bars(code)
        today = [b for b in bars if b.day == day]
        if not today or today[0].open <= 0:
            excluded += 1
            continue
        tb = today[0]
        prev = [b for b in bars if b.day < day]
        if code in prev_members and prev and prev[-1].close > 0:
            rets.append(tb.close / prev[-1].close - 1)   # 연속 보유: 오버나이트 포함
        else:
            rets.append(tb.close / tb.open - 1)          # 신규 편입: 당일 시가 진입
    if not rets:
        return 0.0, 0, excluded
    return sum(rets) / len(rets), len(rets), excluded


def _prev_equity(con: sqlite3.Connection, strategy: str, day: date) -> float:
    """직전 기록일 equity — 같은 레짐 행만 (구정의 행 위 무검증 체인 방지, 리뷰 F8)."""
    row = con.execute(
        "SELECT equity FROM paper_daily WHERE strategy=? AND day<? AND regime=? "
        "ORDER BY day DESC LIMIT 1",
        (strategy, day.isoformat(), REGIME)).fetchone()
    return row[0] if row else 1.0


# ---------------- 기록 ----------------

def _serial_equity(con: sqlite3.Connection, strategy: str, upto: date) -> float:
    # closed_on <= upto 필터: 과거일 재기록 시 미래 트레이드 혼입 방지 (M2)
    eq = 1.0
    for (r,) in con.execute(
            "SELECT ret_net FROM paper_trades WHERE strategy=? AND closed_on<=? "
            "ORDER BY closed_on, code", (strategy, upto.isoformat())):
        eq *= (1 + r)
    return eq


def _upsert_trades(con, strategy: str, rows: list[dict], now_iso: str) -> None:
    for r in rows:
        con.execute(
            "INSERT OR REPLACE INTO paper_trades "
            "(strategy, code, name, opened_on, closed_on, ret_gross, ret_net,"
            " detail, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (strategy, r["code"], r.get("name"),
             str(r.get("opened_on", r.get("day"))), str(r.get("closed_on", r.get("day"))),
             r["ret_gross"], r["ret_net"], r.get("detail"), now_iso))


def _upsert_daily(con, day: date, strategy: str, n: int, day_ret: float,
                  equity: float, note: str, now_iso: str, finalized: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO paper_daily "
        "(day, strategy, n_trades, day_ret, equity, note, recorded_at,"
        " regime, finalized) VALUES (?,?,?,?,?,?,?,?,?)",
        (day.isoformat(), strategy, n, day_ret, equity, note, now_iso,
         REGIME, finalized))


def _removed_members(con: sqlite3.Connection, universe,
                     day: date) -> list[tuple[str, str, date]]:
    """과거 paper_universe_log 에 있었으나 현재 유니버스에 없는 종목과 마지막 등록일."""
    cur = {c for c, _n, _s in universe}
    rows = con.execute(
        "SELECT code, MAX(name), MAX(day) FROM paper_universe_log "
        "WHERE day<? GROUP BY code", (day.isoformat(),)).fetchall()
    return [(code, name or code, date.fromisoformat(last))
            for code, name, last in rows if code not in cur]


def record_day(day: date) -> dict:
    """하루치 페이퍼 기록 실행. 반환: 요약 dict."""
    con = paper_conn()
    start_s = _meta_get(con, "paper_start")
    if start_s is None:
        con.close()
        raise SystemExit("paper_start 미설정 — 먼저 --init YYYY-MM-DD 실행")
    paper_start = date.fromisoformat(start_s)
    if day < paper_start:
        con.close()
        raise SystemExit(f"day({day}) < paper_start({paper_start})")
    # 과거일 소급 기록은 이후 일자의 equity 이력을 오염시키므로 거부 (M2)
    last_rec = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    if last_rec and day < date.fromisoformat(last_rec):
        con.close()
        raise SystemExit(
            f"day({day}) < 마지막 기록일({last_rec}) — 소급 기록 불가. "
            "이력 재구축이 필요하면 paper.db 리셋 후 순서대로 재기록.")

    universe = load_universe()
    if not universe:
        con.close()
        raise SystemExit("라이브 유니버스 0종목 — 웹앱 픽 등록/만료 상태 확인 필요")
    codes = [c for c, _n, _s in universe]

    now = now_kst()
    now_iso = to_db_iso(now)

    # 0) 메타 + 당일 유니버스 감사 기록 — 즉시 커밋 (리뷰 F5: 수 분짜리 네트워크
    #    구간 동안 paper.db 쓰기 락을 잡고 있지 않도록 트랜잭션을 짧게 끊는다)
    _stamp_meta(con)
    # 장중 유니버스 변경 관찰성: 직전 사이클 기록과 diff — 웹앱 장중 등록이
    # 다음 사이클에 편입되는지(그리고 만료로 이탈하는지) 로그로 즉시 확인 가능
    prev_codes = {r[0] for r in con.execute(
        "SELECT code FROM paper_universe_log WHERE day=?", (day.isoformat(),))}
    cur_codes = {c for c, _n, _s in universe}
    if prev_codes and prev_codes != cur_codes:
        joined = sorted(cur_codes - prev_codes)
        left = sorted(prev_codes - cur_codes)
        logger.info("[paper][universe] {} 장중 변경 — 편입 {}건{} / 이탈 {}건{}",
                    day, len(joined), f" {joined}" if joined else "",
                    len(left), f" {left}" if left else "")
    con.execute("DELETE FROM paper_universe_log WHERE day=?", (day.isoformat(),))
    con.executemany(
        "INSERT INTO paper_universe_log VALUES (?,?,?,?,?)",
        [(day.isoformat(), c, n, s, now_iso) for c, n, s in universe])
    con.commit()

    # 1) 당일 분봉 적재 (토스, 당일분은 tail 증분) — paper.db 트랜잭션 없음
    ensure_day_cached(day, codes)
    _daily_cache.clear()                    # 새 데이터 반영해 일봉 재합성

    # 1.5) 시장 데이터 0건이면 기록하지 않는다 — 새벽 사이클/수집 전면 실패가
    #      day_ret=0 유령 행을 만들어 체인·M2 가드를 오염시키는 것 방지 (리뷰 F2)
    if not any(b.day == day for c in set(codes) for b in daily_bars(c)):
        con.close()
        logger.info("[paper] {} 시장 데이터 0건 — 기록 스킵 (장 시작 전/수집 실패)", day)
        return {"day": day.isoformat(), "skipped": "no_market_data"}

    summary: dict = {"day": day.isoformat(), "universe": len(universe)}

    # 2) 전략 계산 — 쓰기 전에 전부 계산해 쓰기 트랜잭션을 최소화
    v2_rows = run_v2_for_day(day, universe)
    leader = _leader_sector(universe, day)
    leader_rows = [r for r in v2_rows if r["sector"] == leader] if leader else []
    # 주도섹터 필터 관찰성: 유니버스/트레이드 양쪽에서 채택·스킵을 명시 로그
    if leader:
        n_uni_leader = sum(1 for _c, _n, s in universe if s == leader)
        skipped_secs = sorted({s for _c, _n, s in universe if s != leader})
        logger.info(
            "[paper][leader] {} 주도섹터={} — 유니버스 {}종목 중 {}종목만 거래 대상, "
            "{}종목 스킵 (비주도: {}) | 트레이드 채택 {}건 / 스킵 {}건",
            day, leader, len(universe), n_uni_leader,
            len(universe) - n_uni_leader, ",".join(skipped_secs) or "-",
            len(leader_rows), len(v2_rows) - len(leader_rows))
    else:
        logger.info("[paper][leader] {} 주도섹터 판정 불가(일봉 부족) — v2_leader 스킵",
                    day)
    removed = _removed_members(con, universe, day)
    # gm_v3 변형 축 병행 리플레이 — 일봉 캐시(_daily_cache)는 변형 간 공유
    gm3_by_strat = {
        strat: run_gm3_replay(paper_start, day, universe, removed,
                              cfg=dc_replace(GmV3Config(), **flags).validated())
        for strat, flags in GM3_VARIANTS
    }
    gm3_rows = gm3_by_strat["gm_v3"]     # 기존 소비자(알림 등)는 기본 축 유지
    v4r_rows = run_v4r_replay(paper_start, day, universe, removed)
    b_ret, n_bench, n_excl = bench_day(con, day, universe)

    # 확정 판정: 과거일 기록이거나 20:05(애프터 종료+버퍼) 이후만 확정치 (리뷰 F2)
    finalized = 1 if (day < now.date() or now.time() >= dtime(20, 5)) else 0

    # 3) 기록 — 단일 짧은 트랜잭션
    for strat, rows in (("v2", v2_rows), ("v2_leader", leader_rows)):
        con.execute("DELETE FROM paper_trades WHERE strategy=? AND closed_on=?",
                    (strat, day.isoformat()))
        _upsert_trades(con, strat, rows, now_iso)
        day_ret = 1.0
        for r in rows:
            day_ret *= (1 + r["ret_net"])
        eq = _serial_equity(con, strat, day)
        note = f"leader={leader}" if strat == "v2_leader" else ""
        _upsert_daily(con, day, strat, len(rows), day_ret - 1, eq, note,
                      now_iso, finalized)
        summary[strat] = {"trades": len(rows), "day_ret": day_ret - 1, "equity": eq}

    #    gm_v3 (+변형 축) — 전체 리플레이 재기록(멱등). EOR(미청산 MTM)은 equity
    #    반영, 실청산 집계 제외 (H1). 제거 종목 이력은 removed 로 보존.
    for strat, _flags in GM3_VARIANTS:
        rows = gm3_by_strat[strat]
        con.execute("DELETE FROM paper_trades WHERE strategy=?", (strat,))
        _upsert_trades(con, strat, rows, now_iso)
        real_closed_today = [r for r in rows
                             if not r["eor"] and str(r["closed_on"]) == day.isoformat()]
        open_mtm = [r for r in rows if r["eor"]]
        eq = _serial_equity(con, strat, day)
        # day_ret = 전일 기록 equity 대비 변화 (실현+MTM 통합, 이중집계 방지)
        prev_eq = _prev_equity(con, strat, day)
        _upsert_daily(con, day, strat, len(real_closed_today),
                      eq / prev_eq - 1 if prev_eq else 0.0, eq,
                      f"open_mtm={len(open_mtm)},removed={len(removed)}",
                      now_iso, finalized)
        summary[strat] = {"closed_today": len(real_closed_today),
                          "open_positions": len(open_mtm), "equity": eq}

    #    v4r 관찰 축 — gm_v3 와 동일한 전체 리플레이 재기록(멱등)
    con.execute("DELETE FROM paper_trades WHERE strategy='v4r'")
    _upsert_trades(con, "v4r", v4r_rows, now_iso)
    v4r_closed_today = [r for r in v4r_rows
                        if not r["eor"] and str(r["closed_on"]) == day.isoformat()]
    v4r_open = [r for r in v4r_rows if r["eor"]]
    eq_v = _serial_equity(con, "v4r", day)
    prev_eq_v = _prev_equity(con, "v4r", day)
    _upsert_daily(con, day, "v4r", len(v4r_closed_today),
                  eq_v / prev_eq_v - 1 if prev_eq_v else 0.0, eq_v,
                  f"open_mtm={len(v4r_open)},removed={len(removed)}",
                  now_iso, finalized)
    summary["v4r"] = {"closed_today": len(v4r_closed_today),
                      "open_positions": len(v4r_open), "equity": eq_v}

    #    벤치마크 — 당일 유니버스 일수익을 직전 레짐 equity 에 체인
    eq_b = _prev_equity(con, "bench_bh", day) * (1 + b_ret)
    _upsert_daily(con, day, "bench_bh", n_bench, b_ret, eq_b,
                  f"stocks={n_bench},excluded={n_excl}", now_iso, finalized)
    summary["bench_bh"] = {"equity": eq_b, "day_ret": b_ret,
                           "stocks": n_bench, "excluded": n_excl}

    # 4) 알파(초과수익) 스냅샷
    for strat in ("v2", "v2_leader", *(s for s, _f in GM3_VARIANTS), "v4r"):
        summary[strat]["alpha_vs_bench"] = summary[strat]["equity"] - eq_b
    summary["finalized"] = finalized

    con.commit()

    # 5) 텔레그램 팩트 알림 (확정분만). notify_events 자체가 예외를 삼키므로
    #    별도 방어 불필요 — 절대 record_day 를 깨지 않는다.
    notify_events(con, day, finalized, leader_rows, gm3_rows, summary)

    con.close()
    logger.info("[paper] {} 기록 완료(finalized={}): {}", day, finalized, summary)
    return summary


def record_upto(day: date) -> list[dict]:
    """빠진 거래일 소급 + 미확정 마지막 기록일 재확정 후 day 기록 (리뷰 F2·F3).

    - 마지막 기록일이 finalized=0 인 채 과거가 되었으면(크래시로 부분 스냅샷
      고착) 먼저 재기록해 확정한다 — 과거일이므로 데이터는 이미 완전하다.
    - (마지막 기록일, day) 사이 결측 거래일은 오래된 날부터 순서대로 소급 기록
      — 벤치 체인에 구멍(그날 수익 0 처리)이 나지 않게 한다. gm_v3 는 전체
      리플레이라 어차피 포함되므로, 벤치만 빠지면 알파가 구조적으로 왜곡된다.
    - 소급일 유니버스 = 현재 라이브 (기기가 꺼져 있었다면 픽도 못 바꿨으므로
      사실상 동일 — ASSUMPTIONS 에 명시).
    """
    con = paper_conn()
    start_s = _meta_get(con, "paper_start")
    last_s = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    unfinal = False
    if last_s:
        unfinal = con.execute(
            "SELECT 1 FROM paper_daily WHERE day=? AND finalized=0 LIMIT 1",
            (last_s,)).fetchone() is not None
    con.close()

    days: list[date] = []
    if start_s:
        anchor = (date.fromisoformat(last_s) if last_s
                  else date.fromisoformat(start_s) - timedelta(days=1))
        if last_s and unfinal and anchor < day:
            days.append(anchor)                     # 미확정 → 재확정
        d = anchor + timedelta(days=1)
        while d < day:
            if _is_trading_day_cached(d):
                days.append(d)                      # 결측 거래일 소급
            d += timedelta(days=1)
    days.append(day)

    out: list[dict] = []
    for d in days:
        if d != day:
            logger.info("[paper] 소급/재확정 기록: {}", d)
        out.append(record_day(d))
    return out


@lru_cache(maxsize=16)
def _is_trading_day_cached(d: date) -> bool:
    """pandas_market_calendars 조회 캐시 — 상주 루프가 매 사이클 재계산하지 않게."""
    return is_trading_day(d)


def report() -> None:
    con = paper_conn()
    start = _meta_get(con, "paper_start")
    print(f"paper_start={start}")
    print(f"{'day':<12}{'strategy':<13}{'n':>3}{'day_ret':>9}{'equity':>9}")
    for row in con.execute(
            "SELECT day, strategy, n_trades, day_ret, equity FROM paper_daily "
            "ORDER BY day, strategy"):
        print(f"{row[0]:<12}{row[1]:<13}{row[2]:>3}{row[3]*100:>8.2f}%{row[4]:>9.4f}")
    # 최신일 기준 누적 초과수익 (절대수익 병기 + 손실회피 태그)
    last = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    if last:
        rows = dict(con.execute(
            "SELECT strategy, equity FROM paper_daily WHERE day=?", (last,)).fetchall())
        bench = rows.get("bench_bh")
        if bench:
            print(f"\n[{last}] 누적 성과 (초과수익 = 손실회피 + 매매수익):")
            for s in ("v2", "v2_leader", *(s for s, _f in GM3_VARIANTS), "v4r"):
                if s in rows:
                    print(f"  {s:<13} {fmt_outperf(rows[s], bench)}")
    con.close()


def init(paper_start: date) -> None:
    con = paper_conn()
    now_iso = to_db_iso(now_kst())
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('paper_start', ?)",
                (paper_start.isoformat(),))
    _stamp_meta(con)
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('initialized_at', ?)",
                (now_iso,))
    con.commit()
    con.close()
    print(f"[paper] 초기화 완료: paper_start={paper_start}, 가정 스탬프 기록")


async def paper_job(day: date | None = None) -> None:
    """외부 스케줄러용 진입점 (best-effort). 현 운영은 --market-schedule 루프가
    전담하고 main_tracker 16:00 잡에서는 제거됨 — 이중 기록자 방지 (리뷰 F5)."""
    d = day or now_kst().date()
    try:
        await asyncio.to_thread(record_upto, d)
    except SystemExit as exc:
        logger.warning("[paper] 스킵: {}", exc)
    except Exception as exc:
        logger.error("[paper] 기록 실패 {}: {}", d, exc)


def run_market_schedule_loop() -> None:
    """--market-schedule 상주 루프 (아카이브봇 타임테이블 이식).

    KST 구간별 간격으로 record_upto(오늘)를 반복 실행한다 — 멱등 재기록이며
    결측일 소급·미확정 재확정을 포함한다. 20:05 이후 실행분만 finalized=1.
    23~06시는 중단, 휴장일은 해당 구간 간격으로 대기만 한다.
    플래그 없는 기존 1회 실행 동작은 그대로다.
    """
    logger.info("[paper][loop] market-schedule 상주 시작 (regime={})", REGIME)
    while True:
        now = now_kst()
        active, wait_s, label = next_action(now)
        if active and _is_trading_day_cached(now.date()):
            started = time_mod.monotonic()
            try:
                record_upto(now.date())
            except SystemExit as exc:
                logger.warning("[paper][loop] 스킵: {}", exc)
            except Exception:
                logger.exception("[paper][loop] 기록 실패 — 다음 주기 재시도")
            elapsed = time_mod.monotonic() - started
            # 창 재판정 + 경과 차감 — 긴 사이클이 실효 주기를 늘리지 않게 (리뷰 F6)
            active2, wait2, label2 = next_action(now_kst())
            wait_s = max(wait2 - elapsed, 5.0) if active2 else wait2
            logger.info("[paper][loop] {} 사이클 {:.1f}s — {}초 대기 ({})",
                        label, elapsed, int(wait_s), label2)
        elif active:
            logger.info("[paper][loop] 휴장일({}) — {} 구간 대기 {}초",
                        now.date(), label, int(wait_s))
        else:
            logger.info("[paper][loop] 중단 구간 — 다음 06:00 까지 {}초 대기",
                        int(wait_s))
        try:
            time_mod.sleep(wait_s)
        except KeyboardInterrupt:
            logger.info("[paper][loop] 종료")
            return


def main() -> None:
    # 상주(hidden 프로세스) 로그 소실 방지 — 파일 싱크 추가 (main.py/main_tracker 와 동일 패턴)
    logger.add(settings.LOG_DIR / "paper_{time:YYYYMMDD}.log",
               level=settings.LOG_LEVEL, rotation="1 day", encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", metavar="YYYY-MM-DD", help="paper_start 설정(1회)")
    ap.add_argument("--day", metavar="YYYY-MM-DD", help="기록할 날짜(기본 오늘)")
    ap.add_argument("--report", action="store_true", help="현황 조회")
    ap.add_argument("--market-schedule", action="store_true",
                    help="상주 루프 (KST 시간대별 간격, 23~06시 중단)")
    args = ap.parse_args()

    if args.init:
        init(date.fromisoformat(args.init))
        return
    if args.report:
        report()
        return
    if args.market_schedule:
        run_market_schedule_loop()
        return
    d = date.fromisoformat(args.day) if args.day else now_kst().date()
    record_upto(d)


if __name__ == "__main__":
    main()
