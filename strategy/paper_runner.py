"""모의투자(paper) 일일 하네스 — 3전략 + 벤치마크 forward 기록.

운영 헌장(우선순위 2) 구현:
  매 거래일 16:00 이후 실행되어 아래 4개를 db/paper.db(WAL)에 기록한다.
    v2          — 프리장 급등→눌림 지지·다지기→아침고점 재돌파 (당일 스캘핑)
    v2_qv       — v2 + 눌림 거래량 마름 + 돌파 거래량 확인(관찰 후보)
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
  종목, 종목코드 dedup(최신 active pick 섹터 채택). 주의: 웹앱 /api/picks 는 tracking_status 를 필터하지
  않으므로(archived 표시됨) 화면과 1:1 은 아니다. 픽 등록/교체는 반드시 이 기기
  (모의투자 지정 기기)의 웹앱에서 한다 — trading.db 는 gitignore(기기 로컬)라
  다른 기기에서 등록한 픽은 여기 오지 않는다.
  2026-07-06 운영 전환(A안): 동결 스냅샷(universe_snapshot.json) 사용 종료.

결측일/부분기록 처리 (record_upto):
  - 기록 사이에 빠진 거래일은 오래된 날부터 자동 소급 기록(유니버스는 현재
    라이브 — 기기가 꺼져 있었다면 픽도 못 바꿨으므로 사실상 동일).
  - finalized=0(장중/데이터 미완 스냅샷) 행은 다음 기록 전에 재확정한다.
    20:05 이후 또는 과거일이면서 정규장 분봉 완결성 게이트까지 통과해야 finalized=1.

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
from dataclasses import dataclass, replace as dc_replace
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from math import ceil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from backtest.run_premarket_pullback import (  # noqa: E402
    _cache_conn, _ensure_cached, _load_bars, backtest_symbol,
    v2_volume_quality_score,
)
from backtest.toss_client import TossClient  # noqa: E402
from config import settings  # noqa: E402
from core.market_calendar import add_trading_days, is_trading_day  # noqa: E402
from core.market_schedule import next_action  # noqa: E402
from core.time_utils import now_kst, to_db_iso  # noqa: E402
from data.sector_store import materialize_expired_picks, sector_key  # noqa: E402
from strategy.paper_notify import (  # noqa: E402
    ACCOUNT_PORTFOLIO_LABELS, PRIMARY_ACCOUNT_PORTFOLIO,
    fmt_account_return, fmt_outperf, notify_events,
)
from strategy.gm_v3.config import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import (  # noqa: E402
    kis_backfill_daily, load_daily_from_toss,
)
from strategy.gm_v3.models import DailyBar  # noqa: E402
from strategy.gm_v3.paper import simulate  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_DB = PROJECT_ROOT / "db" / "paper.db"
NXT_REGISTRY_PATH = PROJECT_ROOT / "config" / "nxt_eligibility_2026q3.json"

MembershipWindow = tuple[str, str, date, date]  # code, name, active_from, active_to

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

# v2 현실 포트폴리오 관찰축. 신호 자체는 바꾸지 않고 사전 관측 가능한 프리장
# 상대강도로만 선별한다. 종목당 20% × 최대 5종목, 동일 섹터 최대 2종목.
V2_PORTFOLIO_MAX_POSITIONS = 5
V2_PORTFOLIO_MAX_PER_SECTOR = 2
V2_PORTFOLIO_SLOT_WEIGHT = 1.0 / V2_PORTFOLIO_MAX_POSITIONS

# 세션 분봉 완결성 게이트. 토스 통합시세에서 09:00~15:30은 391분이며,
# 정규장은 98% 이상·09:02 이전 시작·15:28 이후 종료·내부 최대 간격 5분을
# 모두 요구한다. NXT 프리장 기대 종목은 90% 이상과 동일한 시작/연속성 검사도
# 통과해야 한다. 출처가 확인되지 않은 partial/no_data 종목은 하나라도 있으면
# 확정하지 않는다.
REGULAR_SESSION_START = "09:00"
REGULAR_SESSION_END = "15:30"
REGULAR_SESSION_FIRST_MINUTE_MAX = "09:02"
REGULAR_SESSION_TERMINAL_MINUTE = "15:28"
REGULAR_SESSION_EXPECTED_MINUTES = 391
REGULAR_SESSION_MIN_COVERAGE = 0.98
REGULAR_SESSION_MAX_GAP_MINUTES = 5
REGULAR_SESSION_MIN_COMPLETE_RATIO = 1.0
PREMARKET_START = "08:00"
PREMARKET_END_EXCLUSIVE = "09:00"
PREMARKET_FIRST_MINUTE_MAX = "08:02"
PREMARKET_TERMINAL_MINUTE = "08:50"
PREMARKET_EXPECTED_MINUTES = 59
PREMARKET_MIN_COVERAGE = 0.90
PREMARKET_MAX_GAP_MINUTES = 5
PREMARKET_HISTORY_MIN_BARS = 50
REPAIR_MAX_ATTEMPTS = 3
REPAIR_BASE_BACKOFF_MINUTES = 30

# KRX 달력에는 거래일로 남았지만 운영 당시 Toss 직접 프로브로 전 종목 0봉을
# 확인한 실질 휴장. 자동 추론하지 않으며 새 예외는 오너 승인+독립 근거로만 추가한다.
CONFIRMED_EMPTY_SESSION_OVERRIDES = {
    date(2026, 7, 17): "owner-documented market-wide zero bars",
}

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
    "v2_portfolio": {
        "axes": ["v2_portfolio", "v2_leader_portfolio", "v2_qv_portfolio"],
        "ranking": "진입 전에 확정된 프리장 상승률 내림차순, code tie-break",
        "max_positions": V2_PORTFOLIO_MAX_POSITIONS,
        "max_per_sector": V2_PORTFOLIO_MAX_PER_SECTOR,
        "slot_weight": V2_PORTFOLIO_SLOT_WEIGHT,
        "cash": "빈 슬롯은 현금. 당일 전량 청산 v2만 공유현금 NAV 산출",
    },
    "universe": "trading.db 라이브(active 미만료 pick × active tracking, "
                "이 기기 웹앱에서 등록) — 당일 유니버스는 paper_universe_log 감사 기록. "
                "결측일 소급 기록 시에도 현재 라이브 유니버스 사용(기기 꺼짐=픽 불변)",
    "gm3_universe": "기존 축은 현재 유니버스 + 과거 제외 종목을 paper_start부터 "
                    "리플레이(비교 연속성용 legacy)",
    "joined_axes": "gm_v3_joined/v4r_joined는 trading.db의 append-only "
                   "universe_membership_events만 사용. 장중 경계의 일봉 모호성을 피하려고 "
                   "활성은 다음 거래일부터, 비활성은 직전 거래일까지 보수 적용",
    "v4r": "관찰 축(채택 아님): v2+국소 스윙 기준선+재진입≤4+승자 게이트+"
           "오버나이트 무기한, 애프터 진입 제외. 전체 리플레이 멱등, "
           "removed 는 제거일까지. EOR 은 편도 비용·실청산 집계 제외 (gm_v3 동일). "
           "한계: 분봉 캐시는 종목당 편입-12일부터라(ensure_day_cached lookback) "
           "중도 편입 종목의 그 이전 구간은 리플레이에서 빠짐. opened_on 은 "
           "진입 봉 ISO 시각(당일 재진입 PK 유니크). 축 도입 첫 기록일 day_ret "
           "에는 paper_start 이후 소급 손익이 일괄 반영됨",
    "finalized": "finalized=1 행만 확정치(20:05 이후 또는 과거일 + 정규장 분봉 "
                 "완결성 통과). finalized=0 은 장중/데이터 미완 스냅샷 — 다음 "
                 "기록 전 자동 재수집·재확정",
    "empty_session": "달력상 거래일의 전 종목 0봉은 오너 승인·독립 근거가 있는 "
                     "명시적 override만 confirmed_empty로 기록하고 daily 체인에서 제외",
    "premarket_eligibility": "넥스트레이드 공식 effective-dated 전체 선정목록. "
                             "목록 포함=True, 완전목록 여집합=False; 유효기간 만료 시 "
                             "새 공식 목록 없이는 fail-fast",
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
    _ensure_column(con, "paper_daily", "data_complete", "INTEGER DEFAULT 0")
    _ensure_column(con, "paper_daily", "data_quality_note", "TEXT DEFAULT ''")
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_data_quality ("
        " day TEXT NOT NULL, code TEXT NOT NULL, bar_count INTEGER NOT NULL,"
        " valid_bar_count INTEGER NOT NULL, expected_count INTEGER NOT NULL,"
        " premarket_bar_count INTEGER NOT NULL DEFAULT 0,"
        " premarket_expected INTEGER NOT NULL DEFAULT 0,"
        " premarket_known INTEGER NOT NULL DEFAULT 0,"
        " premarket_first_minute TEXT, premarket_last_minute TEXT,"
        " premarket_max_gap_minutes INTEGER NOT NULL DEFAULT 0,"
        " max_gap_minutes INTEGER NOT NULL DEFAULT 0,"
        " first_minute TEXT, last_minute TEXT, status TEXT NOT NULL,"
        " recorded_at TEXT NOT NULL, PRIMARY KEY(day, code))"
    )
    _ensure_column(
        con, "paper_data_quality", "premarket_bar_count", "INTEGER DEFAULT 0")
    _ensure_column(
        con, "paper_data_quality", "premarket_expected", "INTEGER DEFAULT 0")
    _ensure_column(
        con, "paper_data_quality", "premarket_known", "INTEGER DEFAULT 0")
    _ensure_column(
        con, "paper_data_quality", "premarket_first_minute", "TEXT")
    _ensure_column(
        con, "paper_data_quality", "premarket_last_minute", "TEXT")
    _ensure_column(
        con, "paper_data_quality", "premarket_max_gap_minutes",
        "INTEGER DEFAULT 0")
    _ensure_column(
        con, "paper_data_quality", "max_gap_minutes", "INTEGER DEFAULT 0")
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_session_status ("
        " day TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL,"
        " note TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_premarket_eligibility ("
        " day TEXT NOT NULL, code TEXT NOT NULL, expected INTEGER,"
        " source TEXT NOT NULL, detail TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY(day,code))"
    )
    # 텔레그램 팩트 알림 중복 차단 (5분 재기록/재시작에도 이벤트당 1회)
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_notified ("
        " key TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,"
        " sent_at TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_portfolio_allocations ("
        " day TEXT NOT NULL, strategy TEXT NOT NULL, code TEXT NOT NULL,"
        " name TEXT, sector TEXT NOT NULL, entry_time TEXT NOT NULL,"
        " exit_time TEXT NOT NULL, weight REAL NOT NULL, ret_net REAL NOT NULL,"
        " pnl REAL NOT NULL, detail TEXT, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY(day, strategy, code, entry_time))"
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

def load_universe(
    db_path: str | None = None, as_of_day: date | None = None
) -> list[tuple[str, str, str]]:
    """[(code, name, sector), ...] — trading.db 라이브 조회 (읽기 전용).

    필터 = active pick(미만료) × tracking_status='active' 종목.
    SectorStore.get_active_picks 와 같은 뷰지만 의도적으로 raw SELECT 를 쓴다:
      - 쓰기 없음 (get_active_picks 는 expire_old_picks UPDATE 를 동반 —
        5분 주기 상주 루프가 trading.db 에 쓰는 것을 피한다)
      - SectorStore.open() 의 DDL/마이그레이션 프로브 없음
      - 만료는 expires_at 비교로 동일하게 걸러짐 (status 플립은 알림봇/웹앱 몫)
    참고: 웹앱 /api/picks 는 tracking_status 를 필터하지 않는다(archived 도 표시)
    — 여기서는 제외하는 것이 기록 목적에 맞다.
    종목코드 단위 dedup — 같은 종목이 여러 섹터에 있으면 가장 최근 active pick의
    섹터를 결정적으로 채택한다. 한 종목을 두 번 거래/집계하지 않는다.
    """
    path = str(db_path or settings.DB_PATH)
    con = sqlite3.connect(path, timeout=15)
    try:
        rows = con.execute(
            "SELECT ss.stock_code, ss.stock_name, ss.sector_name, sp.raw_input "
            "FROM sector_stocks ss JOIN sector_picks sp ON sp.id = ss.pick_id "
            "WHERE sp.status='active' AND sp.expires_at > ? "
            "AND COALESCE(ss.tracking_status, 'active') = 'active' "
            "AND (? IS NULL OR sp.raw_input NOT LIKE '[mentor:%' "
            "OR substr(COALESCE(ss.tracking_start_date, sp.created_at),1,10) < ?) "
            "ORDER BY sp.created_at DESC, sp.id DESC, ss.added_order, ss.id DESC",
            (to_db_iso(now_kst()), as_of_day.isoformat() if as_of_day else None,
             as_of_day.isoformat() if as_of_day else None)).fetchall()
    finally:
        con.close()

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for code, name, sector, raw_input in rows:
        if (raw_input or "").startswith("[mentor:") and sector.startswith("멘토 자동픽 · "):
            sector = sector.removeprefix("멘토 자동픽 · ")
        if code in seen:
            continue
        seen.add(code)
        out.append((code, name, sector))
    n_codes = len({c for c, _n, _s in out})
    n_sectors = len({s for _c, _n, s in out})
    logger.info("[paper][universe] 라이브 로드: {}종목({}코드 dedup) / {}섹터",
                len(out), n_codes, n_sectors)
    return out


# ---------------- 데이터 준비 ----------------


@dataclass(frozen=True, slots=True)
class SessionCoverage:
    code: str
    bar_count: int
    valid_bar_count: int
    expected_count: int
    premarket_bar_count: int
    premarket_expected: bool
    premarket_known: bool
    premarket_first_minute: str | None
    premarket_last_minute: str | None
    premarket_max_gap_minutes: int
    max_gap_minutes: int
    first_minute: str | None
    last_minute: str | None
    status: str


def _premarket_history(
        con: sqlite3.Connection, codes: list[str],
        ) -> dict[str, list[tuple[date, int, int]]]:
    """종목별 세션의 (날짜, 정규장 유효봉, 프리장 유효봉)을 한 번에 읽는다."""
    unique_codes = sorted(set(codes))
    if not unique_codes:
        return {}
    placeholders = ",".join("?" for _ in unique_codes)
    valid_ohlc = "open>0 AND high>0 AND low>0 AND close>0"
    minimum_valid = ceil(
        REGULAR_SESSION_EXPECTED_MINUTES * REGULAR_SESSION_MIN_COVERAGE)
    rows = con.execute(
        "SELECT symbol,substr(ts,1,10) AS day,"
        f" COUNT(DISTINCT CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        " THEN substr(ts,1,16) END) AS reg_n,"
        f" COUNT(DISTINCT CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        " THEN substr(ts,1,16) END) AS pre_n"
        f" FROM candles WHERE symbol IN ({placeholders}) "
        "GROUP BY symbol,day ORDER BY symbol,day",
        (REGULAR_SESSION_START, REGULAR_SESSION_END,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE, *unique_codes),
    ).fetchall()
    out: dict[str, list[tuple[date, int, int]]] = {
        code: [] for code in unique_codes}
    for code, day_s, reg_n, pre_n in rows:
        if int(reg_n) >= minimum_valid:
            out[code].append((date.fromisoformat(day_s), int(reg_n), int(pre_n)))
    return out


def premarket_profiles(
        con: sqlite3.Connection, codes: list[str], day: date, *,
        history: dict[str, list[tuple[date, int, int]]] | None = None,
        eligibility: dict[str, bool | None] | None = None,
        ) -> dict[str, tuple[bool, bool]]:
    """관측 봉과 독립 메타데이터로 당일 NXT 기대 여부를 판정한다.

    프리장 봉의 존재는 NXT 기대를 입증하지만, 부재는 NXT 비대상과 수집 장애를
    구분하지 못한다. 따라서 0봉 이력만으로 known=False를 True로 바꾸지 않는다.
    """
    unique_codes = sorted(set(codes))
    history = history if history is not None else _premarket_history(
        con, unique_codes)
    eligibility = eligibility or {}
    out: dict[str, tuple[bool, bool]] = {}
    for code in unique_codes:
        recent = [row for row in history.get(code, []) if row[0] <= day][-3:]
        observed_today = any(
            hist_day == day and pre_n >= PREMARKET_HISTORY_MIN_BARS
            for hist_day, _reg_n, pre_n in recent)
        observed_recent = any(
            pre_n >= PREMARKET_HISTORY_MIN_BARS
            for _hist_day, _reg_n, pre_n in recent)
        metadata = eligibility.get(code)
        expected = observed_today or metadata is True or (
            metadata is None and observed_recent)
        known = observed_recent or metadata is not None
        out[code] = (expected, known)
    return out


@lru_cache(maxsize=1)
def _load_nxt_registry() -> tuple[date, date, frozenset[str], str]:
    """공식 NXT 분기 매매대상 전체 목록을 읽는다."""
    payload = json.loads(NXT_REGISTRY_PATH.read_text(encoding="utf-8"))
    selected_codes = payload.get("selected_codes")
    source_hash = payload.get("source_sha256", "")
    if (not isinstance(selected_codes, list)
            or len(selected_codes) != 610
            or len(set(selected_codes)) != 610
            or any(not isinstance(code, str) or len(code) != 6
                   for code in selected_codes)
            or payload.get("official_selected_count") != 610
            or payload.get("default_expected") is not False
            or len(source_hash) != 64):
        raise RuntimeError("invalid NXT eligibility registry")
    return (
        date.fromisoformat(payload["effective_from"]),
        date.fromisoformat(payload["effective_to"]),
        frozenset(selected_codes),
        f"sha256={source_hash};url={payload['source_url']}",
    )


def premarket_eligibility_for_days(
        con: sqlite3.Connection, days: list[date], codes: list[str],
        now: datetime) -> dict[date, dict[str, bool | None]]:
    """공식 effective-dated NXT registry를 날짜별 감사행으로 물질화한다.

    공식 목록의 유효기간 밖 현재일은 시세 응답으로 추정하지 않고 즉시 중단한다.
    과거 범위 밖 날짜는 unknown으로 남아 rebuild preflight에서 차단된다.
    """
    unique_days = sorted(set(days))
    unique_codes = sorted(set(codes))
    result = {day: {} for day in unique_days}
    if not unique_days or not unique_codes:
        return result
    day_placeholders = ",".join("?" for _ in unique_days)
    code_placeholders = ",".join("?" for _ in unique_codes)
    rows = con.execute(
        "SELECT day,code,expected,recorded_at FROM paper_premarket_eligibility "
        f"WHERE day IN ({day_placeholders}) AND code IN ({code_placeholders})",
        (*[day.isoformat() for day in unique_days], *unique_codes),
    ).fetchall()
    naive_now = datetime.fromisoformat(to_db_iso(now))
    for day_s, code, expected, recorded_at in rows:
        row_day = date.fromisoformat(day_s)
        stale_unknown = (
            row_day == now.date() and expected is None
            and datetime.fromisoformat(recorded_at)
            <= naive_now - timedelta(minutes=REPAIR_BASE_BACKOFF_MINUTES))
        if not stale_unknown:
            result[row_day][code] = (
                None if expected is None else bool(expected))

    effective_from, effective_to, selected_codes, registry_detail = (
        _load_nxt_registry())
    if now.date() in result and not effective_from <= now.date() <= effective_to:
        raise RuntimeError(
            f"NXT registry expired for {now.date()} "
            f"(valid {effective_from}..{effective_to})")
    registry_rows = []
    now_iso = to_db_iso(now)
    for registry_day in unique_days:
        if not effective_from <= registry_day <= effective_to:
            continue
        for code in unique_codes:
            if code in result[registry_day]:
                continue
            expected = code in selected_codes
            result[registry_day][code] = expected
            registry_rows.append((
                registry_day.isoformat(), code, int(expected),
                "official_nxt_2026q3", registry_detail, now_iso))
    if registry_rows:
        con.executemany(
            "INSERT OR REPLACE INTO paper_premarket_eligibility "
            "(day,code,expected,source,detail,recorded_at) VALUES (?,?,?,?,?,?)",
            registry_rows)
        con.commit()

    return result


def session_coverages(
        con: sqlite3.Connection, day: date, codes: list[str], *,
        profiles: dict[str, tuple[bool, bool]] | None = None,
        eligibility: dict[str, bool | None] | None = None,
        ) -> list[SessionCoverage]:
    """캐시의 종목별 정규장 분봉 완결성을 읽기 전용으로 판정한다."""
    unique_codes = sorted(set(codes))
    if not unique_codes:
        return []
    placeholders = ",".join("?" for _ in unique_codes)
    valid_ohlc = "open>0 AND high>0 AND low>0 AND close>0"
    minimum_valid = ceil(
        REGULAR_SESSION_EXPECTED_MINUTES * REGULAR_SESSION_MIN_COVERAGE)
    minimum_premarket = ceil(
        PREMARKET_EXPECTED_MINUTES * PREMARKET_MIN_COVERAGE)
    rows = con.execute(
        "SELECT symbol, "
        "COUNT(DISTINCT CASE WHEN substr(ts,12,5) BETWEEN ? AND ? "
        "THEN substr(ts,1,16) END), "
        f"COUNT(DISTINCT CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,1,16) END), "
        f"MIN(CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END), "
        f"MAX(CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END), "
        f"COUNT(DISTINCT CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? "
        f"AND {valid_ohlc} THEN substr(ts,1,16) END), "
        f"MIN(CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END), "
        f"MAX(CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END) "
        "FROM candles WHERE substr(ts,1,10)=? "
        f"AND symbol IN ({placeholders}) GROUP BY symbol",
        (REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         day.isoformat(), *unique_codes),
    ).fetchall()
    by_code = {row[0]: row[1:] for row in rows}
    profiles = profiles if profiles is not None else premarket_profiles(
        con, unique_codes, day, eligibility=eligibility)
    valid_minutes: dict[str, list[int]] = {code: [] for code in unique_codes}
    premarket_minutes: dict[str, list[int]] = {
        code: [] for code in unique_codes}
    for code, hhmm in con.execute(
            f"SELECT symbol,substr(ts,12,5) FROM candles WHERE substr(ts,1,10)=? "
            "AND substr(ts,12,5)>=? AND substr(ts,12,5)<=? "
            f"AND {valid_ohlc} AND symbol IN ({placeholders}) "
            "GROUP BY symbol,substr(ts,1,16) ORDER BY symbol,substr(ts,1,16)",
            (day.isoformat(), PREMARKET_START, REGULAR_SESSION_END,
             *unique_codes)):
        hour, minute = map(int, hhmm.split(":"))
        minute_of_day = hour * 60 + minute
        if hhmm < PREMARKET_END_EXCLUSIVE:
            premarket_minutes[code].append(minute_of_day)
        elif hhmm >= REGULAR_SESSION_START:
            valid_minutes[code].append(minute_of_day)
    out: list[SessionCoverage] = []
    for code in unique_codes:
        (bar_count, valid_count, first_minute, last_minute,
         premarket_count, premarket_first, premarket_last) = by_code.get(
            code, (0, 0, None, None, 0, None, None))
        profile_expected, profile_known = profiles.get(code, (False, False))
        premarket_expected = bool(profile_expected or premarket_count > 0)
        premarket_known = bool(profile_known or premarket_expected)
        pre_minutes = premarket_minutes[code]
        premarket_max_gap = max(
            (later - earlier
             for earlier, later in zip(pre_minutes, pre_minutes[1:])),
            default=0)
        premarket_complete = (
            premarket_known and (
                not premarket_expected
                or (premarket_count >= minimum_premarket
                    and premarket_first is not None
                    and premarket_first <= PREMARKET_FIRST_MINUTE_MAX
                    and premarket_last is not None
                    and premarket_last >= PREMARKET_TERMINAL_MINUTE
                    and premarket_max_gap <= PREMARKET_MAX_GAP_MINUTES)))
        minutes = valid_minutes[code]
        max_gap = max(
            (later - earlier for earlier, later in zip(minutes, minutes[1:])),
            default=0)
        if bar_count == 0:
            status = "no_data"
        elif (valid_count >= minimum_valid
              and first_minute is not None
              and first_minute <= REGULAR_SESSION_FIRST_MINUTE_MAX
              and last_minute is not None
              and last_minute >= REGULAR_SESSION_TERMINAL_MINUTE
              and max_gap <= REGULAR_SESSION_MAX_GAP_MINUTES
              and premarket_complete):
            status = "complete"
        else:
            status = "partial"
        out.append(SessionCoverage(
            code=code,
            bar_count=int(bar_count),
            valid_bar_count=int(valid_count),
            expected_count=REGULAR_SESSION_EXPECTED_MINUTES,
            premarket_bar_count=int(premarket_count),
            premarket_expected=premarket_expected,
            premarket_known=premarket_known,
            premarket_first_minute=premarket_first,
            premarket_last_minute=premarket_last,
            premarket_max_gap_minutes=premarket_max_gap,
            max_gap_minutes=max_gap,
            first_minute=first_minute,
            last_minute=last_minute,
            status=status,
        ))
    return out


def session_coverages_many(
        con: sqlite3.Connection, days: list[date], codes: list[str], *,
        history: dict[str, list[tuple[date, int, int]]] | None = None,
        eligibility_by_day: dict[date, dict[str, bool | None]] | None = None,
        ) -> dict[date, list[SessionCoverage]]:
    """여러 날짜×종목 완결성을 두 번의 범위 SQL로 일괄 판정한다."""
    unique_days = sorted(set(days))
    unique_codes = sorted(set(codes))
    if not unique_days:
        return {}
    if not unique_codes:
        return {day: [] for day in unique_days}
    placeholders = ",".join("?" for _ in unique_codes)
    valid_ohlc = "open>0 AND high>0 AND low>0 AND close>0"
    rows = con.execute(
        "SELECT substr(ts,1,10),symbol,"
        "COUNT(DISTINCT CASE WHEN substr(ts,12,5) BETWEEN ? AND ? "
        "THEN substr(ts,1,16) END),"
        f"COUNT(DISTINCT CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,1,16) END),"
        f"MIN(CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END),"
        f"MAX(CASE WHEN substr(ts,12,5) BETWEEN ? AND ? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END),"
        f"COUNT(DISTINCT CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        "THEN substr(ts,1,16) END),"
        f"MIN(CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END),"
        f"MAX(CASE WHEN substr(ts,12,5)>=? AND substr(ts,12,5)<? AND {valid_ohlc} "
        "THEN substr(ts,12,5) END) FROM candles "
        "WHERE substr(ts,1,10) BETWEEN ? AND ? "
        f"AND symbol IN ({placeholders}) GROUP BY substr(ts,1,10),symbol",
        (REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         REGULAR_SESSION_START, REGULAR_SESSION_END,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         PREMARKET_START, PREMARKET_END_EXCLUSIVE,
         unique_days[0].isoformat(), unique_days[-1].isoformat(),
         *unique_codes),
    ).fetchall()
    aggregates = {(date.fromisoformat(row[0]), row[1]): row[2:] for row in rows}
    minute_map: dict[tuple[date, str], list[int]] = {}
    premarket_minute_map: dict[tuple[date, str], list[int]] = {}
    for day_s, code, hhmm in con.execute(
            f"SELECT substr(ts,1,10),symbol,substr(ts,12,5) FROM candles "
            "WHERE substr(ts,1,10) BETWEEN ? AND ? "
            "AND substr(ts,12,5)>=? AND substr(ts,12,5)<=? "
            f"AND {valid_ohlc} AND symbol IN ({placeholders}) "
            "GROUP BY substr(ts,1,10),symbol,substr(ts,1,16) "
            "ORDER BY substr(ts,1,10),symbol,substr(ts,1,16)",
            (unique_days[0].isoformat(), unique_days[-1].isoformat(),
             PREMARKET_START, REGULAR_SESSION_END, *unique_codes)):
        hour, minute = map(int, hhmm.split(":"))
        key = (date.fromisoformat(day_s), code)
        minute_of_day = hour * 60 + minute
        if hhmm < PREMARKET_END_EXCLUSIVE:
            premarket_minute_map.setdefault(key, []).append(minute_of_day)
        elif hhmm >= REGULAR_SESSION_START:
            minute_map.setdefault(key, []).append(minute_of_day)

    minimum_valid = ceil(
        REGULAR_SESSION_EXPECTED_MINUTES * REGULAR_SESSION_MIN_COVERAGE)
    minimum_premarket = ceil(
        PREMARKET_EXPECTED_MINUTES * PREMARKET_MIN_COVERAGE)
    history = history if history is not None else _premarket_history(
        con, unique_codes)
    eligibility_by_day = eligibility_by_day or {}
    result: dict[date, list[SessionCoverage]] = {}
    for day in unique_days:
        profiles = premarket_profiles(
            con, unique_codes, day, history=history,
            eligibility=eligibility_by_day.get(day))
        coverage: list[SessionCoverage] = []
        for code in unique_codes:
            (bar_count, valid_count, first_minute, last_minute,
             premarket_count, premarket_first, premarket_last) = aggregates.get(
                (day, code), (0, 0, None, None, 0, None, None))
            profile_expected, profile_known = profiles.get(code, (False, False))
            premarket_expected = bool(profile_expected or premarket_count > 0)
            premarket_known = bool(profile_known or premarket_expected)
            pre_minutes = premarket_minute_map.get((day, code), [])
            premarket_max_gap = max(
                (later - earlier
                 for earlier, later in zip(pre_minutes, pre_minutes[1:])),
                default=0)
            premarket_complete = (
                premarket_known and (
                    not premarket_expected
                    or (premarket_count >= minimum_premarket
                        and premarket_first is not None
                        and premarket_first <= PREMARKET_FIRST_MINUTE_MAX
                        and premarket_last is not None
                        and premarket_last >= PREMARKET_TERMINAL_MINUTE
                        and premarket_max_gap <= PREMARKET_MAX_GAP_MINUTES)))
            minutes = minute_map.get((day, code), [])
            max_gap = max(
                (later - earlier
                 for earlier, later in zip(minutes, minutes[1:])), default=0)
            if bar_count == 0:
                status = "no_data"
            elif (valid_count >= minimum_valid
                  and first_minute is not None
                  and first_minute <= REGULAR_SESSION_FIRST_MINUTE_MAX
                  and last_minute is not None
                  and last_minute >= REGULAR_SESSION_TERMINAL_MINUTE
                  and max_gap <= REGULAR_SESSION_MAX_GAP_MINUTES
                  and premarket_complete):
                status = "complete"
            else:
                status = "partial"
            coverage.append(SessionCoverage(
                code=code, bar_count=int(bar_count),
                valid_bar_count=int(valid_count),
                expected_count=REGULAR_SESSION_EXPECTED_MINUTES,
                premarket_bar_count=int(premarket_count),
                premarket_expected=premarket_expected,
                premarket_known=premarket_known,
                premarket_first_minute=premarket_first,
                premarket_last_minute=premarket_last,
                premarket_max_gap_minutes=premarket_max_gap,
                max_gap_minutes=max_gap, first_minute=first_minute,
                last_minute=last_minute, status=status))
        result[day] = coverage
    return result


def session_quality(coverage: list[SessionCoverage]) -> tuple[bool, str]:
    """확정 허용 여부와 감사용 요약을 반환한다."""
    total = len(coverage)
    complete = [row for row in coverage if row.status == "complete"]
    partial = [row for row in coverage if row.status == "partial"]
    no_data = [row for row in coverage if row.status == "no_data"]
    ratio = len(complete) / total if total else 0.0
    ok = bool(total and not partial and not no_data
              and ratio >= REGULAR_SESSION_MIN_COMPLETE_RATIO)
    offenders = ",".join(
        f"{row.code}:{row.valid_bar_count}/{row.expected_count}@"
        f"{row.first_minute or '-'}-{row.last_minute or '-'}:"
        f"gap={row.max_gap_minutes}:pre={row.premarket_bar_count}"
        f"@{row.premarket_first_minute or '-'}-"
        f"{row.premarket_last_minute or '-'}:"
        f"gap={row.premarket_max_gap_minutes}"
        f"{'*' if row.premarket_expected else ''}" for row in partial[:8])
    missing = ",".join(row.code for row in no_data[:8])
    note = (
        f"complete={len(complete)}/{total},partial={len(partial)},"
        f"no_data={len(no_data)},ratio={ratio:.3f}"
        + (f",offenders={offenders}" if offenders else "")
        + (f",missing={missing}" if missing else "")
    )
    return ok, note


def _insert_cache_bars(con: sqlite3.Connection, code: str, bars) -> None:
    if not bars:
        return
    con.executemany(
        "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)",
        [(code, b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume)
         for b in bars],
    )
    con.commit()


def _ensure_repair_table(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS paper_cache_repairs ("
        " symbol TEXT NOT NULL, day TEXT NOT NULL, attempts INTEGER NOT NULL,"
        " last_attempt TEXT NOT NULL, next_attempt TEXT NOT NULL,"
        " status TEXT NOT NULL, detail TEXT NOT NULL,"
        " PRIMARY KEY(symbol,day))")
    con.commit()


def _repair_due(con: sqlite3.Connection, code: str, day: date,
                now: datetime) -> bool:
    row = con.execute(
        "SELECT status,next_attempt FROM paper_cache_repairs "
        "WHERE symbol=? AND day=?", (code, day.isoformat())).fetchone()
    if row is None:
        return True
    status, next_attempt = row
    if status == "quarantined":
        return False
    return datetime.fromisoformat(next_attempt) <= datetime.fromisoformat(to_db_iso(now))


def _record_repair_failure(con: sqlite3.Connection, code: str, day: date,
                           now: datetime, detail: str) -> None:
    row = con.execute(
        "SELECT attempts FROM paper_cache_repairs WHERE symbol=? AND day=?",
        (code, day.isoformat())).fetchone()
    attempts = (row[0] if row else 0) + 1
    quarantined = attempts >= REPAIR_MAX_ATTEMPTS
    wait_minutes = REPAIR_BASE_BACKOFF_MINUTES * (2 ** (attempts - 1))
    next_attempt = now + timedelta(minutes=wait_minutes)
    con.execute(
        "DELETE FROM fetched WHERE symbol=? AND start=? AND end=?",
        (code, day.isoformat(), day.isoformat()))
    con.execute(
        "INSERT OR REPLACE INTO paper_cache_repairs "
        "(symbol,day,attempts,last_attempt,next_attempt,status,detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (code, day.isoformat(), attempts, to_db_iso(now),
         to_db_iso(next_attempt),
         "quarantined" if quarantined else "retry_wait", detail[:500]))
    con.commit()
    logger.warning(
        "[paper][cache-repair] {}/{} 실패 {}회 status={} detail={}",
        day, code, attempts,
        "quarantined" if quarantined else "retry_wait", detail)


def _repair_cached_day(con: sqlite3.Connection, client: TossClient,
                       code: str, day: date, now: datetime, *,
                       eligibility: dict[str, bool | None] | None = None) -> bool:
    """완료 세션 하루를 원격 전체 응답으로 원자 교체하고 재검증한다."""
    try:
        bars = client.fetch_1m_range(code, day, day)
    except Exception as exc:
        _record_repair_failure(
            con, code, day, now, f"fetch:{type(exc).__name__}:{exc}")
        return False
    # Toss의 종료 커서가 end+1 09:00이라 다음 거래일 프리장 봉이 섞일 수 있다.
    # 목표 날짜만 원자 교체해야 기존 다음날 PK와 충돌하거나 타일 봉이 오염되지 않는다.
    bars = [bar for bar in bars if bar.ts.date() == day]
    if not bars:
        _record_repair_failure(con, code, day, now, "fetch:zero-bars")
        return False

    con.execute("SAVEPOINT paper_day_repair")
    try:
        con.execute(
            "DELETE FROM candles WHERE symbol=? AND substr(ts,1,10)=?",
            (code, day.isoformat()))
        con.executemany(
            "INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
            [(code, bar.ts.isoformat(), bar.open, bar.high, bar.low,
              bar.close, bar.volume) for bar in bars])
        coverage = session_coverages(
            con, day, [code], eligibility=eligibility)[0]
        if coverage.status != "complete":
            raise ValueError(
                f"{coverage.status}:{coverage.valid_bar_count}/"
                f"{coverage.expected_count}:{coverage.first_minute}-"
                f"{coverage.last_minute}:gap={coverage.max_gap_minutes}")
        con.execute(
            "INSERT OR REPLACE INTO fetched VALUES (?,?,?)",
            (code, day.isoformat(), day.isoformat()))
        con.execute("RELEASE SAVEPOINT paper_day_repair")
        con.execute(
            "DELETE FROM paper_cache_repairs WHERE symbol=? AND day=?",
            (code, day.isoformat()))
        con.commit()
        return True
    except Exception as exc:
        con.execute("ROLLBACK TO SAVEPOINT paper_day_repair")
        con.execute("RELEASE SAVEPOINT paper_day_repair")
        con.commit()
        _record_repair_failure(
            con, code, day, now, f"validate:{type(exc).__name__}:{exc}")
        return False


def ensure_day_cached(day: date, codes: list[str], *, lookback_days: int = 12,
                      confirmed_empty_days: set[date] | None = None,
                      required_codes: set[str] | None = None,
                      eligibility_by_day: (
                          dict[date, dict[str, bool | None]] | None) = None) -> None:
    """[day-lookback, day] 중 캐시에 없는 날짜만 토스에서 받아 적재(증분·멱등).

    lookback 이유: v2 전일종가 + 주도섹터 5거래일 수익률에 과거 일봉 필요.
    정착 후에는 매일 1일치만 추가 수집된다(주말/휴장일은 0봉으로 마킹).
    """
    win_start = day - timedelta(days=lookback_days)
    empty_days = confirmed_empty_days or set()
    # 당일을 20:05(애프터 종료+버퍼) 전에 받으면 불완전할 수 있음 → 항상 재수집
    # 하고 완료 마커를 남기지 않는다 (부분 수집 영구 고착 방지, M1).
    now = now_kst()
    day_incomplete = (day == now.date() and now.time() < dtime(20, 5))
    con = _cache_conn()
    _ensure_repair_table(con)
    unique_codes = sorted(set(codes))
    required = set(unique_codes) if required_codes is None else set(required_codes)
    profile_history = _premarket_history(con, sorted(required))
    initial_status: dict[tuple[date, str], str] = {}
    completed_days: list[date] = []
    d = win_start
    while d <= day:
        if (d not in empty_days and _is_trading_day_cached(d)
                and (d < now.date() or now.time() >= dtime(20, 5))):
            completed_days.append(d)
        d += timedelta(days=1)
    batch_coverage = session_coverages_many(
        con, completed_days, sorted(required), history=profile_history,
        eligibility_by_day=eligibility_by_day)
    for coverage_day, rows in batch_coverage.items():
        for row in rows:
            initial_status[(coverage_day, row.code)] = row.status
    with TossClient() as client:
        for code in unique_codes:
            d = win_start
            while d <= day:
                if d in empty_days:
                    d += timedelta(days=1)
                    continue
                ds = d.isoformat()
                done = con.execute(
                    "SELECT 1 FROM fetched WHERE symbol=? AND start=? AND end=?",
                    (code, ds, ds)).fetchone() is not None
                last = con.execute(
                    "SELECT MAX(ts) FROM candles WHERE symbol=? AND ts LIKE ?",
                    (code, ds + "T%"),).fetchone()[0]
                force_live = day_incomplete and d == day
                status = initial_status.get((d, code))
                repair_incomplete = bool(
                    not force_live and code in required
                    and status is not None and status != "complete")

                if force_live:
                    if last:
                        bars = client.fetch_1m_since(code, datetime.fromisoformat(last))
                        _insert_cache_bars(con, code, bars)
                    else:
                        _ensure_cached(con, client, code, d, d)
                    # 장중에는 다음 주기 tail 수집을 위해 완료 마커를 남기지 않는다.
                    con.execute(
                        "DELETE FROM fetched WHERE symbol=? AND start=? AND end=?",
                        (code, ds, ds))
                    con.commit()
                elif repair_incomplete:
                    if _repair_due(con, code, d, now):
                        _repair_cached_day(
                            con, client, code, d, now,
                            eligibility=(eligibility_by_day or {}).get(d))
                elif status == "complete" and not done:
                    con.execute(
                        "INSERT OR IGNORE INTO fetched VALUES (?,?,?)", (code, ds, ds))
                    con.commit()
                elif not done:
                    _ensure_cached(con, client, code, d, d)
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
    labels: dict[str, set[str]] = {}
    for code, _name, sector in universe:
        bars = [b for b in daily_bars(code) if b.day < day]
        if len(bars) < 6:
            continue
        r = bars[-1].close / bars[-6].close - 1
        key = sector_key(sector)
        perf.setdefault(key, []).append(r)
        labels.setdefault(key, set()).add(sector)
    if not perf:
        return None
    leader_key = max(
        perf.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), kv[0]))[0]
    return min(labels[leader_key], key=lambda label: (label.casefold(), label))


def run_v2_for_day(day: date, universe) -> list[dict]:
    """v2 트레이드(당일) — 백테스트 로직 재사용. 반환: dict 리스트."""
    cache = _cache_conn()
    out: list[dict] = []
    for code, name, sector in universe:
        trades = backtest_symbol(cache, code, name, day, day,
                                 mode="v2", **V2_PARAMS)
        for t in trades:
            strength = ((t.pre_high - t.prev_close) / t.prev_close
                        if t.prev_close else 0.0)
            out.append({"code": code, "name": name, "sector": sector,
                        "day": t.day, "ret_gross": t.ret,
                        "ret_net": t.ret - 2 * COST_PER_SIDE,
                        "signal_strength": strength,
                        "entry_time": t.entry_time,
                        "exit_time": t.exit_time,
                        "entry": t.entry, "exit": t.exit,   # 알림용 진입/청산가
                        "reason": t.reason, "detail": t.reason,
                        "quality_score": t.quality_score,
                        "quality_tags": t.quality_tags,
                        "volume_quality_score":
                            v2_volume_quality_score(t.quality_tags)})
    cache.close()
    return out


def select_v2_qv(rows: list[dict]) -> list[dict]:
    """마름 2점 + 돌파 거래량 1점을 모두 충족한 v2 관찰 후보."""
    return [row for row in rows if row.get("volume_quality_score") == 3]


def v2_portfolio_day(rows: list[dict]) -> tuple[float, list[dict], int]:
    """진입·청산 시각 순으로 v2 공유현금 슬롯을 배분한다.

    같은 진입시각의 경쟁 신호만 프리장 상대강도로 정렬한다. 이미 체결된 오전
    거래를 더 늦게 나타난 신호가 사후 탈락시키지 않는다. 청산된 슬롯은 이후
    신호에 재사용하며, 대소문자/공백을 무시한 섹터 키로 동시보유 상한을 건다.
    """
    unique: dict[tuple[str, str], dict] = {}
    invalid = 0
    for row in rows:
        entry_s, exit_s = row.get("entry_time", ""), row.get("exit_time", "")
        try:
            entry_dt = datetime.fromisoformat(entry_s)
            exit_dt = datetime.fromisoformat(exit_s)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if exit_dt < entry_dt:
            invalid += 1
            continue
        item = dict(row)
        item["_entry_dt"] = entry_dt
        item["_exit_dt"] = exit_dt
        key = (item["code"], entry_s)
        old = unique.get(key)
        # 같은 종목·진입시각이 여러 섹터에 중복되면 정규화 키/표시명으로 결정적 선택.
        if old is None or (sector_key(item.get("sector", "")), item.get("sector", "")) < (
                sector_key(old.get("sector", "")), old.get("sector", "")):
            unique[key] = item

    ranked = sorted(
        unique.values(),
        key=lambda r: (r["_entry_dt"], -r.get("signal_strength", 0.0), r["code"]),
    )
    active: list[dict] = []
    selected: list[dict] = []
    cash = 1.0
    skipped = invalid

    for row in ranked:
        entry_dt = row["_entry_dt"]
        still_open: list[dict] = []
        for pos in active:
            # 3분봉 timestamp만으로는 봉 내부의 청산/신규 진입 선후를 알 수 없다.
            # 같은 timestamp의 청산 자금은 보수적으로 다음 timestamp부터 재사용한다.
            if pos["_exit_dt"] < entry_dt:
                cash += pos["weight"] + pos["pnl"]
            else:
                still_open.append(pos)
        active = still_open

        active_codes = {p["code"] for p in active}
        sec_key = sector_key(row.get("sector", ""))
        sec_count = sum(1 for p in active if p["_sector_key"] == sec_key)
        if (row["code"] in active_codes
                or len(active) >= V2_PORTFOLIO_MAX_POSITIONS
                or sec_count >= V2_PORTFOLIO_MAX_PER_SECTOR
                or cash <= 0):
            skipped += 1
            continue

        weight = min(V2_PORTFOLIO_SLOT_WEIGHT, cash)
        cash -= weight
        allocation = dict(row)
        allocation["weight"] = weight
        allocation["pnl"] = weight * row["ret_net"]
        allocation["_sector_key"] = sec_key
        active.append(allocation)
        selected.append(allocation)

    for pos in active:
        cash += pos["weight"] + pos["pnl"]
    for row in selected:
        for private in ("_entry_dt", "_exit_dt", "_sector_key"):
            row.pop(private, None)
    return cash - 1.0, selected, skipped


def run_v4r_replay(
        paper_start: date, today: date, universe,
        removed: list[tuple[str, str, date]] = (),
        membership_windows: list[MembershipWindow] | None = None,
        ) -> list[dict]:
    """v4r 전체 리플레이(결정적·멱등) — 오버나이트 멀티데이라 gm_v3 처럼
    매일 [paper_start, act_to] 를 통째로 재계산한다.

    removed = 과거 유니버스에 있었다 제거된 종목: 제거일까지만 시뮬레이션
    (end=min(제거일, today)) — 웹앱 제거로 과거 손실이 소멸하는 생존편향 차단.
    EOR(범위 끝 미청산 = 보유 중 MTM)은 gm_v3 와 동일하게 진입 비용(편도)만
    차감하고 equity 에 반영, 실청산 집계에서는 제외한다.
    """
    cache = _cache_conn()
    if membership_windows is None:
        targets: dict[str, tuple[str, date]] = {}
        for code, name, _sector in universe:
            targets.setdefault(code, (name, today))
        for code, name, last_day in removed:
            targets.setdefault(code, (name, min(last_day, today)))
        windows = [(code, name, paper_start, end_d)
                   for code, (name, end_d) in targets.items()]
    else:
        windows = membership_windows
    out: list[dict] = []
    for code, name, act_from, act_to in windows:
        trades = backtest_symbol(cache, code, name, act_from, act_to,
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
                   cfg: GmV3Config | None = None,
                   membership_windows: list[MembershipWindow] | None = None,
                   ) -> list[dict]:
    """gm_v3 전체 리플레이(결정적) — act 윈도우 [paper_start, act_to].

    상태를 DB에 영속하지 않고 매일 데이터에서 재구성 → 멱등.
    removed = [(code, name, 마지막 등록일)] — 과거 유니버스에 있었다 제외된 종목.
    제외일까지 act 하고 그 시점 EOR 로 동결한다: 웹앱에서 종목을 지워도 과거
    손실 트레이드가 리플레이에서 소멸하지 않는다 (생존편향 차단, 리뷰 F4).
    코드 단위 dedup — 두 섹터에 걸친 종목이 두 번 리플레이되지 않게.
    cfg 로 룰 토글 변형(GM3_VARIANTS)을 주입한다 — None 이면 기본 gm_v3.
    """
    cfg = cfg if cfg is not None else GmV3Config()
    if membership_windows is None:
        targets: dict[str, tuple[str, date]] = {}
        for code, name, _sector in universe:
            targets.setdefault(code, (name, today))
        for code, name, last_day in removed:
            targets.setdefault(code, (name, min(last_day, today)))
        windows = [(code, name, paper_start, act_to)
                   for code, (name, act_to) in targets.items()]
    else:
        windows = membership_windows
    out: list[dict] = []
    skipped: list[str] = []
    for code, name, act_from, act_to in windows:
        bars = daily_bars(code)
        if len(bars) < 20:
            skipped.append(code)
            continue
        trades, _sigs = simulate(code, bars, cfg, fill_mode="next_open",
                                 act_from=act_from, act_to=act_to)
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


def bench_day(con: sqlite3.Connection, day: date, universe, *,
              prev_members: set[str] | None = None) -> tuple[float, int, int]:
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
    if prev_members is None:
        prev_log_day = con.execute(
            "SELECT MAX(day) FROM paper_universe_log WHERE day<?",
            (day.isoformat(),)).fetchone()[0]
        prev_members = set()
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
                  equity: float, note: str, now_iso: str, finalized: int, *,
                  data_complete: int = 0, data_quality_note: str = "") -> None:
    con.execute(
        "INSERT OR REPLACE INTO paper_daily "
        "(day, strategy, n_trades, day_ret, equity, note, recorded_at,"
        " regime, finalized, data_complete, data_quality_note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (day.isoformat(), strategy, n, day_ret, equity, note, now_iso,
         REGIME, finalized, data_complete, data_quality_note))


def _replace_data_quality(con: sqlite3.Connection, day: date,
                          coverage: list[SessionCoverage], now_iso: str) -> None:
    con.execute("DELETE FROM paper_data_quality WHERE day=?", (day.isoformat(),))
    con.executemany(
        "INSERT INTO paper_data_quality "
        "(day,code,bar_count,valid_bar_count,expected_count,premarket_bar_count,"
        "premarket_expected,premarket_known,premarket_first_minute,"
        "premarket_last_minute,premarket_max_gap_minutes,max_gap_minutes,"
        "first_minute,last_minute,status,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(day.isoformat(), row.code, row.bar_count, row.valid_bar_count,
          row.expected_count, row.premarket_bar_count,
          int(row.premarket_expected), int(row.premarket_known),
          row.premarket_first_minute, row.premarket_last_minute,
          row.premarket_max_gap_minutes, row.max_gap_minutes,
          row.first_minute, row.last_minute, row.status, now_iso)
         for row in coverage],
    )


def _assert_day_invariants(con: sqlite3.Connection, day: date,
                           strategies: set[str]) -> None:
    """커밋 전에 현재일까지의 집계·원장·equity 체인 불일치를 차단한다."""
    day_s = day.isoformat()
    ledger_axes = {
        "v2", "v2_leader", "v2_qv",
        *(name for name, _flags in GM3_VARIANTS),
        "v4r", "gm_v3_joined", "v4r_joined",
    }
    portfolio_axes = {
        "v2_portfolio", "v2_leader_portfolio", "v2_qv_portfolio",
    }
    for strategy in sorted(strategies):
        daily_rows = con.execute(
            "SELECT day,n_trades,day_ret,equity FROM paper_daily "
            "WHERE day<=? AND strategy=? AND regime=? ORDER BY day",
            (day_s, strategy, REGIME),
        ).fetchall()
        if not daily_rows or daily_rows[-1][0] != day_s:
            raise RuntimeError(f"paper invariant: daily row missing {day_s}/{strategy}")
        previous = 1.0
        for hist_day, n_trades, day_ret, equity in daily_rows:
            expected_equity = previous * (1 + day_ret)
            if abs(equity - expected_equity) > 1e-9:
                raise RuntimeError(
                    f"paper invariant: equity chain mismatch {hist_day}/{strategy} "
                    f"saved={equity} expected={expected_equity}")
            previous = equity

            if strategy in ledger_axes:
                ledger_count = con.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE strategy=? "
                    "AND closed_on=? AND COALESCE(detail,'') NOT LIKE '%EOR%'",
                    (strategy, hist_day),
                ).fetchone()[0]
                if ledger_count != n_trades:
                    raise RuntimeError(
                        f"paper invariant: trade count mismatch "
                        f"{hist_day}/{strategy} daily={n_trades} "
                        f"ledger={ledger_count}")
            elif strategy in portfolio_axes:
                allocation_count = con.execute(
                    "SELECT COUNT(*) FROM paper_portfolio_allocations "
                    "WHERE day=? AND strategy=?", (hist_day, strategy),
                ).fetchone()[0]
                if allocation_count != n_trades:
                    raise RuntimeError(
                        f"paper invariant: allocation count mismatch "
                        f"{hist_day}/{strategy} daily={n_trades} "
                        f"allocations={allocation_count}")


def _replace_portfolio_allocations(con: sqlite3.Connection, day: date,
                                   strategy: str, rows: list[dict],
                                   now_iso: str) -> None:
    con.execute(
        "DELETE FROM paper_portfolio_allocations WHERE day=? AND strategy=?",
        (day.isoformat(), strategy))
    con.executemany(
        "INSERT INTO paper_portfolio_allocations "
        "(day,strategy,code,name,sector,entry_time,exit_time,weight,ret_net,pnl,"
        " detail,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(day.isoformat(), strategy, r["code"], r.get("name"),
          r.get("sector", ""), r["entry_time"], r["exit_time"], r["weight"],
          r["ret_net"], r["pnl"], r.get("detail"), now_iso)
         for r in rows],
    )


def _removed_members(con: sqlite3.Connection, universe,
                     day: date) -> list[tuple[str, str, date]]:
    """과거 paper_universe_log 에 있었으나 현재 유니버스에 없는 종목과 마지막 등록일."""
    cur = {c for c, _n, _s in universe}
    rows = con.execute(
        "SELECT code, MAX(name), MAX(day) FROM paper_universe_log "
        "WHERE day<? GROUP BY code", (day.isoformat(),)).fetchall()
    return [(code, name or code, date.fromisoformat(last))
            for code, name, last in rows if code not in cur]


def _previous_trading_day(d: date) -> date:
    candidate = d - timedelta(days=1)
    while not _is_trading_day_cached(candidate):
        candidate -= timedelta(days=1)
    return candidate


def load_membership_windows(db_path: str, paper_start: date,
                            day: date) -> list[MembershipWindow] | None:
    """append-only 활성/비활성 이벤트로 보수적 일봉 replay 구간을 만든다.

    테이블이 아직 배포되지 않았으면 None을 반환해 corrected 축 기록을 건너뛴다.
    과거 상태를 현재 snapshot으로 추측하거나 결측일에 소급 생성하지 않는다.
    """
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='universe_membership_events'").fetchone()
        if not exists:
            return None
        rows = con.execute(
            "SELECT occurred_at,action,stock_code,stock_name "
            "FROM universe_membership_events WHERE occurred_at<? "
            "ORDER BY occurred_at,id",
            ((day + timedelta(days=1)).isoformat(),),
        ).fetchall()
    finally:
        con.close()

    active: dict[str, tuple[str, date]] = {}
    windows: list[MembershipWindow] = []
    for occurred_s, action, code, name in rows:
        occurred = datetime.fromisoformat(occurred_s)
        if action == "activate":
            effective = (occurred.date() if occurred.time() <= dtime(8, 0)
                         else add_trading_days(occurred.date(), 1))
            effective = max(effective, paper_start)
            if effective <= day and code not in active:
                active[code] = (name or code, effective)
        elif action == "deactivate" and code in active:
            old_name, active_from = active.pop(code)
            effective_end = (occurred.date() if occurred.time() >= dtime(20, 0)
                             else _previous_trading_day(occurred.date()))
            effective_end = min(effective_end, day)
            if active_from <= effective_end:
                windows.append((code, old_name, active_from, effective_end))

    for code, (name, active_from) in active.items():
        if active_from <= day:
            windows.append((code, name, active_from, day))
    return sorted(windows, key=lambda w: (w[2], w[0], w[3]))


def membership_universe_on(
        windows: list[MembershipWindow], day: date,
        ) -> list[tuple[str, str, str]]:
    """보수적 멤버십 구간과 정확히 같은 경계의 당일 벤치 유니버스."""
    members: dict[str, str] = {}
    for code, name, active_from, active_to in windows:
        if active_from <= day <= active_to:
            members[code] = name
    return [(code, members[code], "") for code in sorted(members)]


def _must_skip_for_missing_market_data(
        has_market_data: bool, universe, joined_universe) -> bool:
    """활성 관찰 대상이 하나라도 있으면 데이터 0건을 수익률 0으로 확정하지 않는다."""
    return not has_market_data and bool(universe or joined_universe)


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

    expired = materialize_expired_picks(str(settings.DB_PATH))
    if expired:
        logger.info("[paper][universe] 시간 경과 만료 {}개 상태/이탈 이벤트 확정", expired)
    # 장중 추가된 mentor 종목으로 같은 날 오전 거래를 사후 생성하지 않는다.
    # mentor 출처만 보수적으로 다음 거래일부터 일별 재생 유니버스에 포함한다.
    universe = load_universe(as_of_day=day)
    membership_windows = load_membership_windows(
        str(settings.DB_PATH), paper_start, day)
    if not universe and not membership_windows:
        con.close()
        raise SystemExit("라이브 유니버스 0종목 — 웹앱 픽 등록/만료 상태 확인 필요")
    if not universe:
        logger.info(
            "[paper][universe] 라이브 0종목 — 과거 멤버십 종료/벤치 carry만 기록")
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
    joined_universe = (
        membership_universe_on(membership_windows, day)
        if membership_windows is not None else [])
    cache_codes = sorted({
        *codes,
        *(code for code, _name, _sector in joined_universe),
        *(code for code, _name, _start, _end in (membership_windows or [])),
    })
    quality_codes = sorted({
        *codes,
        *(code for code, _name, _sector in joined_universe),
    })
    eligibility_days = [
        day - timedelta(days=offset) for offset in range(13)]
    eligibility_by_day = premarket_eligibility_for_days(
        con, eligibility_days, quality_codes, now)

    # 1) 당일 분봉 적재 (토스, 당일분은 tail 증분) — paper.db 트랜잭션 없음
    confirmed_empty_days = {
        date.fromisoformat(row[0]) for row in con.execute(
            "SELECT day FROM paper_session_status WHERE status='confirmed_empty'")}
    confirmed_empty_days.update(CONFIRMED_EMPTY_SESSION_OVERRIDES)
    ensure_day_cached(
        day, cache_codes, confirmed_empty_days=confirmed_empty_days,
        required_codes=set(quality_codes),
        eligibility_by_day=eligibility_by_day)
    # 활성 관찰 종목의 분봉 완결성을 별도 감사표에 남긴다. 과거 replay용으로
    # 캐시하는 이미 비활성 종목은 당일 확정 게이트의 분모에 넣지 않는다.
    cache_con = _cache_conn()
    try:
        coverage = session_coverages(
            cache_con, day, quality_codes,
            eligibility=eligibility_by_day.get(day))
    finally:
        cache_con.close()
    quality_ok, quality_note = session_quality(coverage)
    time_ready = day < now.date() or now.time() >= dtime(20, 5)
    data_complete = int(time_ready and quality_ok)
    _replace_data_quality(con, day, coverage, now_iso)
    con.commit()
    if time_ready and not quality_ok:
        logger.error("[paper][data-quality] {} 확정 차단: {}", day, quality_note)
    _daily_cache.clear()                    # 새 데이터 반영해 일봉 재합성

    # 1.5) 시장 데이터 0건이면 기록하지 않는다 — 새벽 사이클/수집 전면 실패가
    #      day_ret=0 유령 행을 만들어 체인·M2 가드를 오염시키는 것 방지 (리뷰 F2)
    has_market_data = any(
        b.day == day for c in set(cache_codes) for b in daily_bars(c))
    if _must_skip_for_missing_market_data(
            has_market_data, universe, joined_universe):
        override_note = CONFIRMED_EMPTY_SESSION_OVERRIDES.get(day)
        if time_ready and override_note:
            con.execute(
                "INSERT OR REPLACE INTO paper_session_status "
                "(day,status,attempts,note,recorded_at) VALUES (?,?,?,?,?)",
                (day.isoformat(), "confirmed_empty", 0,
                 f"{override_note};{quality_note}", now_iso))
            con.commit()
            con.close()
            logger.warning(
                "[paper][session] {} 명시적 실질 휴장 예외 — confirmed_empty", day)
            return {
                "day": day.isoformat(), "skipped": "confirmed_empty",
                "finalized": 1,
                "data_quality": {"complete": False, "note": quality_note},
            }
        con.close()
        logger.info("[paper] {} 시장 데이터 0건 — 기록 스킵 (장 시작 전/수집 실패)", day)
        return {"day": day.isoformat(), "skipped": "no_market_data"}
    con.execute("DELETE FROM paper_session_status WHERE day=?", (day.isoformat(),))

    summary: dict = {"day": day.isoformat(), "universe": len(universe)}

    # 2) 전략 계산 — 쓰기 전에 전부 계산해 쓰기 트랜잭션을 최소화
    v2_rows = run_v2_for_day(day, universe)
    # v2_qv: 백테스트에서 양 반기 플러스였던 두 거래량 특징을 모두 만족한
    # 별도 관찰축. 기존 v2 신호/운영축은 불변이다.
    v2_qv_rows = select_v2_qv(v2_rows)
    leader = _leader_sector(universe, day)
    leader_rows = ([r for r in v2_rows
                    if sector_key(r["sector"]) == sector_key(leader)]
                   if leader else [])
    # 주도섹터 필터 관찰성: 유니버스/트레이드 양쪽에서 채택·스킵을 명시 로그
    if leader:
        leader_key = sector_key(leader)
        n_uni_leader = sum(
            1 for _c, _n, s in universe if sector_key(s) == leader_key)
        skipped_secs = sorted({
            s for _c, _n, s in universe if sector_key(s) != leader_key})
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
    joined_rows = None
    if membership_windows is not None:
        joined_rows = {
            "gm_v3_joined": run_gm3_replay(
                paper_start, day, universe, cfg=GmV3Config(),
                membership_windows=membership_windows),
            "v4r_joined": run_v4r_replay(
                paper_start, day, universe,
                membership_windows=membership_windows),
        }
    b_ret, n_bench, n_excl = bench_day(con, day, universe)
    joined_bench = None
    if membership_windows:
        joined_prev_day = _previous_trading_day(day)
        joined_prev_members = {
            code for code, _name, _sector
            in membership_universe_on(membership_windows, joined_prev_day)
        }
        joined_bench = bench_day(
            con, day, joined_universe, prev_members=joined_prev_members)

    # 시간뿐 아니라 활성 유니버스의 분봉 완결성까지 통과해야 확정한다.
    finalized = data_complete
    quality_kwargs = {
        "data_complete": data_complete,
        "data_quality_note": quality_note,
    }
    written_strategies: set[str] = set()

    # 3) 기록 — 단일 짧은 트랜잭션
    for strat, rows in (
            ("v2", v2_rows),
            ("v2_leader", leader_rows),
            ("v2_qv", v2_qv_rows),
            ):
        con.execute("DELETE FROM paper_trades WHERE strategy=? AND closed_on=?",
                    (strat, day.isoformat()))
        _upsert_trades(con, strat, rows, now_iso)
        day_ret = 1.0
        for r in rows:
            day_ret *= (1 + r["ret_net"])
        eq = _serial_equity(con, strat, day)
        if strat == "v2_leader":
            note = f"leader={leader}"
        elif strat == "v2_qv":
            note = "dryup<=0.8x,breakout_volume>=1.5x"
        else:
            note = ""
        _upsert_daily(con, day, strat, len(rows), day_ret - 1, eq, note,
                      now_iso, finalized, **quality_kwargs)
        written_strategies.add(strat)
        summary[strat] = {"trades": len(rows), "day_ret": day_ret - 1, "equity": eq}

    #    v2 공유현금 NAV 관찰축 — 기존 직렬복리 축은 비교/호환을 위해 그대로 둔다.
    #    v2는 당일 전량 청산이라 일별 슬롯 배분만으로 현금·동시신호를 정확히 반영.
    for strat, rows in (("v2_portfolio", v2_rows),
                        ("v2_leader_portfolio", leader_rows),
                        ("v2_qv_portfolio", v2_qv_rows)):
        p_ret, selected, skipped = v2_portfolio_day(rows)
        eq = _prev_equity(con, strat, day) * (1 + p_ret)
        _replace_portfolio_allocations(con, day, strat, selected, now_iso)
        sectors = ",".join(sorted({r["sector"] for r in selected})) or "-"
        _upsert_daily(
            con, day, strat, len(selected), p_ret, eq,
            f"selected={len(selected)},skipped={skipped},sectors={sectors}",
            now_iso, finalized, **quality_kwargs)
        written_strategies.add(strat)
        summary[strat] = {
            "trades": len(selected), "day_ret": p_ret, "equity": eq,
            "skipped": skipped,
        }

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
                      now_iso, finalized, **quality_kwargs)
        written_strategies.add(strat)
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
                  now_iso, finalized, **quality_kwargs)
    written_strategies.add("v4r")
    summary["v4r"] = {"closed_today": len(v4r_closed_today),
                      "open_positions": len(v4r_open), "equity": eq_v}

    #    append-only 실제 편입 이벤트 기반 corrected 관찰축. 기존 축은 비교 연속성을
    #    위해 보존하고, corrected 축만 등록 전/비활성 구간을 제외한다.
    if joined_rows is not None and membership_windows:
        for strat, rows in joined_rows.items():
            con.execute("DELETE FROM paper_trades WHERE strategy=?", (strat,))
            _upsert_trades(con, strat, rows, now_iso)
            real_closed_today = [
                r for r in rows
                if not r["eor"] and str(r["closed_on"]) == day.isoformat()]
            open_mtm = [r for r in rows if r["eor"]]
            eq = _serial_equity(con, strat, day)
            prev_eq = _prev_equity(con, strat, day)
            _upsert_daily(
                con, day, strat, len(real_closed_today),
                eq / prev_eq - 1 if prev_eq else 0.0, eq,
                f"open_mtm={len(open_mtm)},windows={len(membership_windows)}",
                now_iso, finalized, **quality_kwargs)
            written_strategies.add(strat)
            summary[strat] = {
                "closed_today": len(real_closed_today),
                "open_positions": len(open_mtm),
                "equity": eq,
            }

    #    벤치마크 — 당일 유니버스 일수익을 직전 레짐 equity 에 체인
    eq_b = _prev_equity(con, "bench_bh", day) * (1 + b_ret)
    _upsert_daily(con, day, "bench_bh", n_bench, b_ret, eq_b,
                  f"stocks={n_bench},excluded={n_excl}", now_iso, finalized,
                  **quality_kwargs)
    written_strategies.add("bench_bh")
    summary["bench_bh"] = {"equity": eq_b, "day_ret": b_ret,
                           "stocks": n_bench, "excluded": n_excl}
    # v2_portfolio는 도입일부터 시작하므로 같은 시작점의 별도 벤치를 체인한다.
    eq_bp = _prev_equity(con, "bench_v2_portfolio", day) * (1 + b_ret)
    _upsert_daily(con, day, "bench_v2_portfolio", n_bench, b_ret, eq_bp,
                  f"stocks={n_bench},excluded={n_excl},matched=v2_portfolio",
                  now_iso, finalized, **quality_kwargs)
    written_strategies.add("bench_v2_portfolio")
    summary["bench_v2_portfolio"] = {
        "equity": eq_bp, "day_ret": b_ret, "stocks": n_bench,
        "excluded": n_excl,
    }
    eq_joined = None
    if (joined_rows is not None and membership_windows
            and joined_bench is not None):
        joined_ret, joined_n, joined_excl = joined_bench
        eq_joined = (
            _prev_equity(con, "bench_joined", day) * (1 + joined_ret))
        _upsert_daily(
            con, day, "bench_joined", joined_n, joined_ret, eq_joined,
            f"stocks={joined_n},excluded={joined_excl},matched=joined_axes",
            now_iso, finalized, **quality_kwargs)
        written_strategies.add("bench_joined")
        summary["bench_joined"] = {
            "equity": eq_joined, "day_ret": joined_ret, "stocks": joined_n,
            "excluded": joined_excl,
        }

    # 4) 알파(초과수익) 스냅샷
    for strat in (
            "v2", "v2_leader", "v2_qv",
            *(s for s, _f in GM3_VARIANTS), "v4r",
            ):
        summary[strat]["alpha_vs_bench"] = summary[strat]["equity"] - eq_b
    for strat in ("v2_portfolio", "v2_leader_portfolio", "v2_qv_portfolio"):
        summary[strat]["alpha_vs_bench"] = summary[strat]["equity"] - eq_bp
    if eq_joined is not None:
        for strat in ("gm_v3_joined", "v4r_joined"):
            summary[strat]["alpha_vs_bench"] = summary[strat]["equity"] - eq_joined
    summary["account_period_start"] = con.execute(
        "SELECT MIN(day) FROM paper_daily WHERE strategy='v2_portfolio'"
    ).fetchone()[0]
    summary["finalized"] = finalized
    summary["data_quality"] = {
        "complete": bool(data_complete),
        "note": quality_note,
    }

    try:
        _assert_day_invariants(con, day, written_strategies)
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise

    # 5) 텔레그램 팩트 알림 (확정분만). notify_events 자체가 예외를 삼키므로
    #    별도 방어 불필요 — 절대 record_day 를 깨지 않는다.
    notify_events(con, day, finalized, leader_rows, gm3_rows, summary)

    con.close()
    logger.info("[paper] {} 기록 완료(finalized={}): {}", day, finalized, summary)
    return summary


def record_upto(day: date) -> list[dict]:
    """빠진 거래일 소급 + 미확정 마지막 기록일 재확정 후 day 기록 (리뷰 F2·F3).

    - 마지막 기록일이 finalized=0 인 채 과거가 되었으면(크래시/분봉 불완전)
      먼저 재수집·재기록해 완전성 게이트를 다시 통과시킨다.
    - (마지막 기록일, day) 사이 결측 거래일은 오래된 날부터 순서대로 소급 기록
      — 벤치 체인에 구멍(그날 수익 0 처리)이 나지 않게 한다. gm_v3 는 전체
      리플레이라 어차피 포함되므로, 벤치만 빠지면 알파가 구조적으로 왜곡된다.
    - 소급일 유니버스 = 현재 라이브 (기기가 꺼져 있었다면 픽도 못 바꿨으므로
      사실상 동일 — ASSUMPTIONS 에 명시).
    """
    con = paper_conn()
    start_s = _meta_get(con, "paper_start")
    last_s = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    unfinal_s = con.execute(
        "SELECT MIN(day) FROM paper_daily WHERE regime=? "
        "AND (finalized=0 OR data_complete=0)",
        (REGIME,),
    ).fetchone()[0]
    confirmed_empty_s = {row[0] for row in con.execute(
        "SELECT day FROM paper_session_status WHERE status='confirmed_empty'")}
    con.close()

    if unfinal_s and last_s and unfinal_s < last_s:
        raise SystemExit(
            f"중간 미확정/미검증일({unfinal_s}) 뒤에 기록({last_s})이 존재함 — "
            "분봉 복구 후 paper_start부터 equity 체인 재구축 필요")
    unfinal = bool(last_s and unfinal_s == last_s)

    days: list[date] = []
    if start_s:
        anchor = (date.fromisoformat(last_s) if last_s
                  else date.fromisoformat(start_s) - timedelta(days=1))
        if last_s and unfinal and anchor < day:
            days.append(anchor)                     # 미확정 → 재확정
        d = anchor + timedelta(days=1)
        while d < day:
            if (_is_trading_day_cached(d)
                    and d.isoformat() not in confirmed_empty_s):
                days.append(d)                      # 결측 거래일 소급
            d += timedelta(days=1)
    days.append(day)

    out: list[dict] = []
    for d in days:
        if d != day:
            logger.info("[paper] 소급/재확정 기록: {}", d)
        result = record_day(d)
        out.append(result)
        if d != day and result.get("finalized") != 1:
            logger.error(
                "[paper] {} 데이터 완결성 미통과 — 이후 날짜 기록 중단", d)
            break
    return out


@lru_cache(maxsize=16)
def _is_trading_day_cached(d: date) -> bool:
    """pandas_market_calendars 조회 캐시 — 상주 루프가 매 사이클 재계산하지 않게."""
    return is_trading_day(d)


def report() -> None:
    con = paper_conn()
    start = _meta_get(con, "paper_start")
    print(f"paper_start={start}")
    last = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    if last:
        daily_columns = {
            row[1] for row in con.execute("PRAGMA table_info(paper_daily)")}
        has_quality = {
            "data_complete", "data_quality_note"}.issubset(daily_columns)
        if has_quality:
            latest = con.execute(
                "SELECT strategy,equity,finalized,data_complete,data_quality_note "
                "FROM paper_daily WHERE day=?", (last,),
            ).fetchall()
        else:  # 구버전 DB/단위 테스트 호환
            latest = [(*row, None, "") for row in con.execute(
                "SELECT strategy,equity,finalized FROM paper_daily WHERE day=?",
                (last,),
            ).fetchall()]
        rows = {strategy: equity for strategy, equity, *_rest in latest}
        finalized = min((row[2] for row in latest), default=0)
        quality_complete = min(
            (row[3] for row in latest if row[3] is not None), default=None)
        quality_note = next((row[4] for row in latest if row[4]), "")
        matched = rows.get("bench_v2_portfolio")
        account_start = con.execute(
            "SELECT MIN(day) FROM paper_daily WHERE strategy='v2_portfolio'"
        ).fetchone()[0]
        primary_name, primary_label = PRIMARY_ACCOUNT_PORTFOLIO
        primary_equity = rows.get(primary_name)
        if matched is not None and account_start and primary_equity is not None:
            status = "확정" if finalized else "데이터 미완/장중 잠정"
            print(
                f"\n[{last}] 총기간 봇 수익 "
                f"({account_start}~{last}, 공유현금 모의계좌, {status})"
            )
            print(
                f"  {primary_label:<14} "
                f"{fmt_account_return(primary_equity, matched)}"
            )
            print(f"  동시작 시장 참고치  총수익 {matched - 1.0:+.2%}")
            print("  ※ 시장 참고치는 등록종목 100% 보유로 봇과 노출 비매칭")
            print("  ※ 실주문 손익 아님 · 공유현금/동시보유 제한 반영")
            if quality_complete is not None:
                quality_label = "통과" if quality_complete else "미통과"
                print(f"  데이터 완결성 {quality_label}: {quality_note or '-'}")

            research_rows = [
                (label, rows[name])
                for name, label in ACCOUNT_PORTFOLIO_LABELS[1:]
                if name in rows
            ]
            if research_rows:
                print("  연구용 계좌형 관찰축 (대표 봇 수익 아님):")
                for label, equity in research_rows:
                    print(f"    {label:<12} 총수익 {equity - 1.0:+.2%}")

    print(f"\n{'day':<12}{'strategy':<13}{'n':>3}{'day_ret':>9}{'equity':>9}")
    for row in con.execute(
            "SELECT day, strategy, n_trades, day_ret, equity FROM paper_daily "
            "ORDER BY day, strategy"):
        print(f"{row[0]:<12}{row[1]:<13}{row[2]:>3}{row[3]*100:>8.2f}%{row[4]:>9.4f}")

    # 기존 직렬복리 축은 연구 연속성을 위해 보존하되 실제 계좌 총수익과 분리한다.
    if last:
        bench = rows.get("bench_bh")
        if bench:
            print(
                f"\n[{last}] 연구용 참고지표 "
                "(직렬복리·실제 계좌 총수익 아님):"
            )
            for s in (
                    "v2", "v2_leader", "v2_qv",
                    *(s for s, _f in GM3_VARIANTS), "v4r",
                    ):
                if s in rows:
                    print(f"  {s:<13} {fmt_outperf(rows[s], bench)}")
            joined_bench = rows.get("bench_joined")
            if joined_bench is not None:
                for s in ("gm_v3_joined", "v4r_joined"):
                    if s in rows:
                        print(f"  {s:<20} {fmt_outperf(rows[s], joined_bench)}"
                              " (실편입·동시작 벤치)")
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
