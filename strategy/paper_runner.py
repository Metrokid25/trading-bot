"""모의투자(paper) 일일 하네스 — 3전략 + 벤치마크 forward 기록.

운영 헌장(우선순위 2) 구현:
  매 거래일 16:00 이후 실행되어 아래 4개를 db/paper.db(WAL)에 기록한다.
    v2          — 프리장 급등→눌림 지지·다지기→아침고점 재돌파 (당일 스캘핑)
    v2_leader   — v2 + 주도섹터 필터(신호일 d-1 기준 최근 5거래일 수익률 1위 섹터만)
    gm_v3       — 멘토 룰엔진 R1~R12 (일봉 스윙, 다음날 시가 체결)
    bench_bh    — 동결 유니버스 동일가중 buy&hold (알파 판정 기준선)

명시적 체결/비용 가정 (paper_meta 에 스탬프):
  - 비용 0.25%/편도(왕복 0.5%). v2 트레이드는 ret-0.005, gm_v3 는 realized
    - 0.005×max_invested, 벤치마크는 진입 시 0.0025 1회 차감.
  - v2 체결 = 당일 3분봉 실측가(백테스트 로직 그대로), gm_v3 = 다음날 시가
    (R10 손절만 당일 스탑가), 벤치마크 진입 = 시작일 정규장 시가.
  - 애프터 급변 취소 / 프리장 갭 보류 규칙은 미반영(보수적 미확정분).
  - 자산곡선 = 청산 순 직렬 복리(포트폴리오 병렬 회계 아님 — 백테스트 평가와
    동일 방식, 벤치마크와 상대 비교 목적).

데이터: 토스 1분봉(당일분 매일 캐시 적재) + gm_v3 워밍업은 KIS 일봉 보충.
유니버스: universe_snapshot.json (git 동결 스냅샷) — trading.db 를 쓰지 않음.

사용 (반드시 -m 로 — strategy/signal.py 가 stdlib signal 을 가리므로 직접 실행 금지):
  ./.venv/Scripts/python.exe -m strategy.paper_runner --init 2026-07-06   # 1회
  ./.venv/Scripts/python.exe -m strategy.paper_runner                     # 당일 기록
  ./.venv/Scripts/python.exe -m strategy.paper_runner --day 2026-07-06    # 특정일
  ./.venv/Scripts/python.exe -m strategy.paper_runner --report            # 현황 조회
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import date, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from backtest.run_premarket_pullback import (  # noqa: E402
    _cache_conn, _ensure_cached, _load_bars, backtest_symbol,
)
from backtest.toss_client import TossClient  # noqa: E402
from core.time_utils import now_kst, to_db_iso  # noqa: E402
from strategy.gm_v3.config import GmV3Config  # noqa: E402
from strategy.gm_v3.data_source import (  # noqa: E402
    kis_backfill_daily, load_daily_from_toss,
)
from strategy.gm_v3.models import DailyBar  # noqa: E402
from strategy.gm_v3.paper import simulate  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_DB = PROJECT_ROOT / "db" / "paper.db"
SNAPSHOT = PROJECT_ROOT / "universe_snapshot.json"

COST_PER_SIDE = 0.0025          # 0.25%/편도 (왕복 0.5%)
GM3_WARMUP_DAYS = 90            # 지표 워밍업용 과거 일봉(달력 아님, 거래일 여유)

V2_PARAMS = dict(pre_surge=0.05, pullback_min=0.03, support_tol=0.005,
                 tp_levels=(0.05, 0.10, 0.15, 0.20, 0.25), stop_pct=0.04,
                 consol_bars=3)

ASSUMPTIONS = {
    "cost_per_side": COST_PER_SIDE,
    "v2_fill": "당일 3분봉 실측가 (백테스트 로직 동일)",
    "gm3_fill": "next_open (R10 손절만 당일 스탑가)",
    "gm3_open_positions": "미청산 포지션은 EOR(MTM) 행으로 equity에 반영, "
                          "청산 비용은 실제 청산 시에만 차감. n_trades 는 실청산만 집계",
    "bench_entry": "첫 거래일 정규장 시가, 진입 비용 1회 차감. 첫날 봉 없는 종목은 영구 제외",
    "equity": "청산순 직렬 복리 (포트폴리오 병렬 회계 아님)",
    "after_hours_rules": "애프터 급변 취소/프리장 갭 보류 미반영(보수적)",
    "v2_params": {k: (list(v) if isinstance(v, tuple) else v)
                  for k, v in V2_PARAMS.items()},
    "universe": "universe_snapshot.json 동결본",
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
    return con


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ---------------- 유니버스 (동결 스냅샷) ----------------

def load_universe() -> list[tuple[str, str, str]]:
    """[(code, name, sector), ...] — universe_snapshot.json 동결본."""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str]] = []
    for s in data["sectors"]:
        for st in s["stocks"]:
            out.append((st["code"], st["name"], s["sector_name"]))
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
                if force or (ds not in have and ds not in done):
                    _ensure_cached(con, client, code, d, d)
                    if force:   # 불완전 수집 — 다음 실행에서 다시 받도록 마커 제거
                        con.execute("DELETE FROM fetched WHERE symbol=? AND start=? AND end=?",
                                    (code, ds, ds))
                        con.commit()
                d += timedelta(days=1)
    con.close()


_daily_cache: dict[str, list[DailyBar]] = {}


def daily_bars(code: str) -> list[DailyBar]:
    """정규장 일봉(토스 캐시 합성 + 필요 시 KIS 워밍업 보충)."""
    if code in _daily_cache:
        return _daily_cache[code]
    bars = load_daily_from_toss(code)
    if bars:
        first = bars[0].day
        have = len(bars)
        if have < GM3_WARMUP_DAYS:
            back = asyncio.run(kis_backfill_daily(code, first, GM3_WARMUP_DAYS - have))
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
                        "detail": t.reason})
    cache.close()
    return out


def run_gm3_replay(paper_start: date, today: date, universe) -> list[dict]:
    """gm_v3 전체 리플레이(결정적) — act 윈도우 [paper_start, today].

    상태를 DB에 영속하지 않고 매일 데이터에서 재구성 → 멱등.
    """
    cfg = GmV3Config()
    out: list[dict] = []
    skipped: list[str] = []
    for code, name, _sector in universe:
        bars = daily_bars(code)
        if len(bars) < 20:
            skipped.append(code)
            continue
        trades, _sigs = simulate(code, bars, cfg, fill_mode="next_open",
                                 act_from=paper_start, act_to=today)
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


def bench_equity(paper_start: date, today: date, universe
                 ) -> tuple[float, float, int, int]:
    """동일가중 B&H: (오늘 equity, 전일 equity, 반영 종목수, 제외 종목수).

    진입 = 첫 거래일(유니버스 전체 중 paper_start 이후 최초 봉 일자) 정규장 시가.
    그 첫날 봉이 없는 종목(거래정지 등)은 영구 제외 — 중도 편입으로 분모가
    흔들리는 것을 막는다(M4).
    """
    per_code = {c: [b for b in daily_bars(c) if b.day >= paper_start]
                for c, _n, _s in universe}
    firsts = [bars[0].day for bars in per_code.values() if bars]
    if not firsts:
        return 1.0, 1.0, 0, len(universe)
    entry_day = min(firsts)                 # 실제 첫 거래일

    rets_today: list[float] = []
    rets_prev: list[float] = []
    excluded = 0
    for _code, bars in per_code.items():
        if not bars or bars[0].day != entry_day or bars[0].open <= 0:
            excluded += 1
            continue
        entry = bars[0].open
        upto = [b for b in bars if b.day <= today]
        if not upto:
            excluded += 1
            continue
        rets_today.append(upto[-1].close / entry - 1)
        prev = [b for b in upto if b.day < today]
        rets_prev.append((prev[-1].close / entry - 1) if prev else 0.0)
    if not rets_today:
        return 1.0, 1.0, 0, excluded
    eq = (1 + sum(rets_today) / len(rets_today)) * (1 - COST_PER_SIDE)
    if today == entry_day:
        eq_prev = 1.0                       # 첫날: 진입 비용이 day_ret 에 드러나게 (L1)
    else:
        eq_prev = (1 + sum(rets_prev) / len(rets_prev)) * (1 - COST_PER_SIDE)
    return eq, eq_prev, len(rets_today), excluded


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
                  equity: float, note: str, now_iso: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO paper_daily "
        "(day, strategy, n_trades, day_ret, equity, note, recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (day.isoformat(), strategy, n, day_ret, equity, note, now_iso))


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
    codes = [c for c, _n, _s in universe]

    # 1) 당일 분봉 적재 (토스)
    ensure_day_cached(day, codes)
    _daily_cache.clear()                    # 새 데이터 반영해 일봉 재합성

    now_iso = to_db_iso(now_kst())
    summary: dict = {"day": day.isoformat()}

    # 2) v2 / v2_leader — 당일 행 삭제 후 재기록 (재실행 시 유령 행 방지, L2)
    v2_rows = run_v2_for_day(day, universe)
    leader = _leader_sector(universe, day)
    leader_rows = [r for r in v2_rows if r["sector"] == leader] if leader else []
    for strat, rows in (("v2", v2_rows), ("v2_leader", leader_rows)):
        con.execute("DELETE FROM paper_trades WHERE strategy=? AND closed_on=?",
                    (strat, day.isoformat()))
        _upsert_trades(con, strat, rows, now_iso)
        day_ret = 1.0
        for r in rows:
            day_ret *= (1 + r["ret_net"])
        eq = _serial_equity(con, strat, day)
        note = f"leader={leader}" if strat == "v2_leader" else ""
        _upsert_daily(con, day, strat, len(rows), day_ret - 1, eq, note, now_iso)
        summary[strat] = {"trades": len(rows), "day_ret": day_ret - 1, "equity": eq}

    # 3) gm_v3 — 전체 리플레이 후 재기록 (멱등).
    #    EOR(미청산 MTM) 행은 equity 에는 반영하되 실청산 집계에서 제외 (H1).
    gm3_rows = run_gm3_replay(paper_start, day, universe)
    con.execute("DELETE FROM paper_trades WHERE strategy='gm_v3'")
    _upsert_trades(con, "gm_v3", gm3_rows, now_iso)
    real_closed_today = [r for r in gm3_rows
                         if not r["eor"] and str(r["closed_on"]) == day.isoformat()]
    open_mtm = [r for r in gm3_rows if r["eor"]]
    eq = _serial_equity(con, "gm_v3", day)
    # day_ret = 전일 기록 equity 대비 변화 (실현+MTM 통합, 이중집계 방지)
    prev_eq_row = con.execute(
        "SELECT equity FROM paper_daily WHERE strategy='gm_v3' AND day<? "
        "ORDER BY day DESC LIMIT 1", (day.isoformat(),)).fetchone()
    prev_eq = prev_eq_row[0] if prev_eq_row else 1.0
    _upsert_daily(con, day, "gm_v3", len(real_closed_today),
                  eq / prev_eq - 1 if prev_eq else 0.0, eq,
                  f"open_mtm={len(open_mtm)}", now_iso)
    summary["gm_v3"] = {"closed_today": len(real_closed_today),
                        "open_positions": len(open_mtm), "equity": eq}

    # 4) 벤치마크
    eq_b, eq_b_prev, n_bench, n_excl = bench_equity(paper_start, day, universe)
    _upsert_daily(con, day, "bench_bh", n_bench,
                  (eq_b / eq_b_prev - 1) if eq_b_prev else 0.0, eq_b,
                  f"stocks={n_bench},excluded={n_excl}", now_iso)
    summary["bench_bh"] = {"equity": eq_b, "stocks": n_bench, "excluded": n_excl}

    # 5) 알파(초과수익) 스냅샷
    for strat in ("v2", "v2_leader", "gm_v3"):
        summary[strat]["alpha_vs_bench"] = summary[strat]["equity"] - eq_b

    con.commit()
    con.close()
    logger.info("[paper] {} 기록 완료: {}", day, summary)
    return summary


def report() -> None:
    con = paper_conn()
    start = _meta_get(con, "paper_start")
    print(f"paper_start={start}")
    print(f"{'day':<12}{'strategy':<11}{'n':>3}{'day_ret':>9}{'equity':>9}")
    for row in con.execute(
            "SELECT day, strategy, n_trades, day_ret, equity FROM paper_daily "
            "ORDER BY day, strategy"):
        print(f"{row[0]:<12}{row[1]:<11}{row[2]:>3}{row[3]*100:>8.2f}%{row[4]:>9.4f}")
    # 최신일 기준 알파
    last = con.execute("SELECT MAX(day) FROM paper_daily").fetchone()[0]
    if last:
        rows = dict(con.execute(
            "SELECT strategy, equity FROM paper_daily WHERE day=?", (last,)).fetchall())
        bench = rows.get("bench_bh")
        if bench:
            print(f"\n[{last}] 벤치마크 대비 초과수익:")
            for s in ("v2", "v2_leader", "gm_v3"):
                if s in rows:
                    print(f"  {s:<11} {(rows[s]-bench)*100:+.2f}%p (eq {rows[s]:.4f} vs bench {bench:.4f})")
    con.close()


def init(paper_start: date) -> None:
    con = paper_conn()
    now_iso = to_db_iso(now_kst())
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('paper_start', ?)",
                (paper_start.isoformat(),))
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('assumptions', ?)",
                (json.dumps(ASSUMPTIONS, ensure_ascii=False),))
    con.execute("INSERT OR REPLACE INTO paper_meta VALUES ('initialized_at', ?)",
                (now_iso,))
    con.commit()
    con.close()
    print(f"[paper] 초기화 완료: paper_start={paper_start}, 가정 스탬프 기록")


async def paper_job(day: date | None = None) -> None:
    """main_tracker 16:00 잡에서 호출되는 진입점 (best-effort)."""
    d = day or now_kst().date()
    try:
        await asyncio.to_thread(record_day, d)
    except SystemExit as exc:
        logger.warning("[paper] 스킵: {}", exc)
    except Exception as exc:
        logger.error("[paper] 기록 실패 {}: {}", d, exc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", metavar="YYYY-MM-DD", help="paper_start 설정(1회)")
    ap.add_argument("--day", metavar="YYYY-MM-DD", help="기록할 날짜(기본 오늘)")
    ap.add_argument("--report", action="store_true", help="현황 조회")
    args = ap.parse_args()

    if args.init:
        init(date.fromisoformat(args.init))
        return
    if args.report:
        report()
        return
    d = date.fromisoformat(args.day) if args.day else now_kst().date()
    record_day(d)


if __name__ == "__main__":
    main()
