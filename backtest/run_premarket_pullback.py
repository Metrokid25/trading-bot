"""프리장 급등 → 본장 눌림 → 재폭등 전략의 6월 백테스트 (토스 1분봉).

가설(형):
  ① 프리장(08:00~08:50) 급등 → ② 본장 09:00~09:30 폭락 → ③ 저점 지지
  → ④ 진입 후 재폭등에 매도.

데이터: 토스 Open API 1분봉(프리/정규/애프터 통합). db/toss_candles.db 에 캐시.
대상: trading.db 의 active sector_stocks (웹앱 등록 종목).

이 스크립트는 운영 DB(trading.db)에 쓰지 않는다 — 캐시는 별도 db/toss_candles.db.

사용:
  ./.venv/Scripts/python.exe backtest/run_premarket_pullback.py
  ... --start 2026-06-01 --end 2026-06-27 --pre-surge 5 --drop 3 --tp 5 --sl 3
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from backtest.toss_client import KST, Bar, TossClient  # noqa: E402

CACHE_DB = Path(__file__).resolve().parent.parent / "db" / "toss_candles.db"

PRE_START, PRE_END = time(8, 0), time(9, 0)        # 프리장 [08:00, 09:00)
REG_OPEN, REG_CLOSE = time(9, 0), time(15, 30)     # 정규장 [09:00, 15:30]
WIN_END = time(9, 30)                               # 눌림 윈도우 끝 [09:00, 09:30)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    name: str
    day: date
    prev_close: int
    pre_high: int
    entry: int
    exit: int
    reason: str       # 'TP' | 'SL' | 'EOD'
    ret: float


# ---------------- 캐시 ----------------

def _cache_conn() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_DB)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute(
        "CREATE TABLE IF NOT EXISTS candles ("
        "symbol TEXT, ts TEXT, open INT, high INT, low INT, close INT, volume INT,"
        "PRIMARY KEY(symbol, ts))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS fetched (symbol TEXT, start TEXT, end TEXT,"
        "PRIMARY KEY(symbol, start, end))"
    )
    return con


def _ensure_cached(con: sqlite3.Connection, client: TossClient,
                  symbol: str, start: date, end: date) -> None:
    key = (symbol, start.isoformat(), end.isoformat())
    if con.execute("SELECT 1 FROM fetched WHERE symbol=? AND start=? AND end=?", key).fetchone():
        return
    print(f"  [fetch] {symbol} {start}~{end} ...", end="", flush=True)
    bars = client.fetch_1m_range(symbol, start, end)
    con.executemany(
        "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)",
        [(symbol, b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars],
    )
    con.execute("INSERT OR IGNORE INTO fetched VALUES (?,?,?)", key)
    con.commit()
    print(f" {len(bars)} bars")


def _load_bars(con: sqlite3.Connection, symbol: str) -> list[Bar]:
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? ORDER BY ts",
        (symbol,),
    ).fetchall()
    out = []
    for ts, o, h, l, c, v in rows:
        dt = datetime.fromisoformat(ts)
        out.append(Bar(ts=dt.astimezone(KST), open=o, high=h, low=l, close=c, volume=v))
    return out


# ---------------- 전략 ----------------

def _by_day(bars: list[Bar]) -> dict[date, list[Bar]]:
    days: dict[date, list[Bar]] = {}
    for b in bars:
        days.setdefault(b.ts.date(), []).append(b)
    return days


def evaluate_day(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                 *, pre_surge: float, drop: float, tp: float, sl: float) -> Trade | None:
    pre = [b for b in day_bars if PRE_START <= b.ts.time() < PRE_END]
    reg = [b for b in day_bars if REG_OPEN <= b.ts.time() <= REG_CLOSE]
    if not reg or prev_close <= 0:
        return None

    # ① 프리장 급등: 실체결(vol>0) 프리장 고가가 전일종가 대비 +pre_surge% 이상
    pre_traded = [b for b in pre if b.volume > 0]
    if not pre_traded:
        return None
    pre_high = max(b.high for b in pre_traded)
    if (pre_high - prev_close) / prev_close < pre_surge:
        return None

    # ② 본장 09:00~09:30 폭락: 윈도우 시가 대비 저점 -drop% 이상 하락
    window = [b for b in reg if b.ts.time() < WIN_END]
    if not window:
        return None
    w_open = window[0].open
    w_low = min(b.low for b in window)
    if w_open <= 0 or (w_open - w_low) / w_open < drop:
        return None

    # ③④ 진입(09:29 종가) → 재폭등 청산(TP/SL/EOD)
    entry = window[-1].close
    if entry <= 0:
        return None
    after = [b for b in reg if b.ts.time() >= WIN_END]
    tp_px, sl_px = round(entry * (1 + tp)), round(entry * (1 - sl))
    exit_px, reason = reg[-1].close, "EOD"
    for b in after:
        if b.low <= sl_px:           # 같은 봉에 둘 다 닿으면 보수적으로 손절 우선
            exit_px, reason = sl_px, "SL"
            break
        if b.high >= tp_px:
            exit_px, reason = tp_px, "TP"
            break

    return Trade(symbol, name, day_bars[0].ts.date(), prev_close, pre_high,
                 entry, exit_px, reason, (exit_px - entry) / entry)


def _resample_3m(bars: list[Bar]) -> list[Bar]:
    """1분봉 → 3분봉 (벽시계 :00/:03/:06 정렬). bars 는 시간 오름차순."""
    buckets: dict[datetime, list] = {}
    for b in bars:
        key = b.ts.replace(minute=(b.ts.minute // 3) * 3, second=0, microsecond=0)
        g = buckets.get(key)
        if g is None:
            buckets[key] = [b.open, b.high, b.low, b.close, b.volume]
        else:
            g[1] = max(g[1], b.high)
            g[2] = min(g[2], b.low)
            g[3] = b.close
            g[4] += b.volume
    return [Bar(ts=k, open=o, high=h, low=l, close=c, volume=v)
            for k, (o, h, l, c, v) in sorted(buckets.items())]


def evaluate_day_v2(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                    *, pre_surge: float, pullback_min: float, support_tol: float,
                    tp_levels: tuple[float, ...], stop_pct: float, **_ignore) -> Trade | None:
    """저점 지지 + 재폭등 돌파 진입 + 분할 익절 청산 (당일 스캘핑).

    ① 프리장 급등(전일종가 대비 +pre_surge%) 게이트.
    ② 아침 고점에서 pullback_min% 이상 눌림 발생.
    ③ 저점 지지(눌림 저점을 support_tol% 넘게 깨지 않음).
    ④ 재폭등: 눌림 직전 아침고점을 종가로 재돌파 → 진입.
    청산: tp_levels 각 구간에서 1/N 분할 익절(+10%≈정적VI 근사). 첫 익절 후
          본절(진입가) 회귀 시 잔량 전량 청산. 손절 진입가 -stop_pct%.
    """
    pre = [b for b in day_bars if PRE_START <= b.ts.time() < PRE_END and b.volume > 0]
    reg = [b for b in day_bars if REG_OPEN <= b.ts.time() <= REG_CLOSE]
    if not pre or not reg or prev_close <= 0:
        return None
    pre_high = max(b.high for b in pre)
    if (pre_high - prev_close) / prev_close < pre_surge:
        return None

    bars3 = _resample_3m(reg)
    if len(bars3) < 6:
        return None

    day_high = float(pre_high)        # 아침 고점(프리장 고점부터 누적)
    breakout_level: float | None = None   # 눌림 직전 고점 = 재돌파 기준선
    pullback_low: float | None = None     # 지지 저점(구조적 손절선)
    entry = None
    entry_i = -1
    for i, b in enumerate(bars3):
        prev_high = day_high              # 현재봉 반영 전 고점
        day_high = max(day_high, b.high)
        # ② 눌림 최초 등록: 직전 고점 대비 pullback_min% 하락
        if pullback_low is None:
            if b.low <= prev_high * (1 - pullback_min):
                pullback_low = float(b.low)
                breakout_level = prev_high
            continue
        # ③ 다지기: 지지 저점 추적(이탈 시 저점 하향)
        pullback_low = b.low if b.low < pullback_low * (1 - support_tol) else min(pullback_low, b.low)
        # ④ 재폭등: 눌림 직전 고점을 종가로 재돌파 → 진입
        if breakout_level is not None and b.close > breakout_level:
            entry, entry_i = b.close, i
            break
    if entry is None or entry <= 0:
        return None

    # ----- 분할 익절 청산 -----
    n = len(tp_levels)                       # 분할 수 (기본 5)
    tps = [entry * (1 + lv) for lv in tp_levels]
    fills: list[float] = []                  # 체결가(각 1/n)
    stop = entry * (1 - stop_pct)            # 초기 손절 -stop_pct%
    armed = False                            # 첫 익절 후 본절 보호 가동
    tp_i = 0
    stop_kind = "SL"                         # 손절 라벨(첫 익절 후 'BE')
    exit_kind = "EOD"                         # 잔량 청산 사유
    for b in bars3[entry_i + 1:]:
        if b.low <= stop:                    # 손절/본절 이탈 → 잔량 전량
            fills += [stop] * (n - len(fills))
            exit_kind = stop_kind
            break
        while tp_i < n and b.high >= tps[tp_i]:   # 한 봉에 여러 구간 통과 가능
            fills.append(tps[tp_i])
            tp_i += 1
            if not armed:                    # 첫 익절 → 손절을 본절로 상향
                armed, stop, stop_kind = True, float(entry), "BE"
        if len(fills) >= n:
            break
    if len(fills) < n:                       # 미체결 잔량 → 종가 청산
        fills += [bars3[-1].close] * (n - len(fills))

    exit_avg = sum(fills) / n
    reason = f"{tp_i}TP" + (f"/{exit_kind}" if tp_i < n else "")
    return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                 int(entry), int(round(exit_avg)), reason, (exit_avg - entry) / entry)


_EVALUATORS = {"v1": evaluate_day, "v2": evaluate_day_v2}


def backtest_symbol(con, symbol, name, start, end, *, mode="v2", **params) -> list[Trade]:
    evaluator = _EVALUATORS[mode]
    days = _by_day(_load_bars(con, symbol))
    ordered = sorted(days)
    trades: list[Trade] = []
    for i, d in enumerate(ordered):
        if not (start <= d <= end):
            continue
        # 전일 종가 = 직전 거래일의 정규장 마지막 종가
        prev_close = 0
        for pd in reversed(ordered[:i]):
            prev_reg = [b for b in days[pd] if REG_OPEN <= b.ts.time() <= REG_CLOSE]
            if prev_reg:
                prev_close = prev_reg[-1].close
                break
        t = evaluator(symbol, name, days[d], prev_close, **params)
        if t:
            trades.append(t)
    return trades


# ---------------- 리포트 ----------------

def _stock_universe(codes: list[str] | None = None) -> list[tuple[str, str]]:
    con = sqlite3.connect(settings.DB_PATH)
    rows = con.execute(
        "SELECT DISTINCT stock_code, stock_name FROM sector_stocks "
        "WHERE tracking_status='active' ORDER BY stock_code"
    ).fetchall()
    con.close()
    uni = [(r[0], r[1]) for r in rows]
    if codes:
        want = set(codes)
        picked = [(c, n) for c, n in uni if c in want]
        # DB 에 없는 코드도 코드 자체로 처리 가능하게.
        known = {c for c, _ in picked}
        picked += [(c, c) for c in codes if c not in known]
        return picked
    return uni


def _report(trades: list[Trade]) -> None:
    if not trades:
        print("\n진입 신호 0건 — 임계치를 완화하거나(예: --pre-surge 3 --drop 2) 기간을 넓혀봐.")
        return
    trades.sort(key=lambda t: (t.day, t.symbol))
    print(f"\n{'날짜':<11}{'종목':<14}{'전일종가':>9}{'프리고':>9}{'진입':>8}{'청산':>8}{'사유':>5}{'수익률':>9}")
    print("-" * 80)
    for t in trades:
        print(f"{t.day.isoformat():<11}{t.name[:12]:<14}{t.prev_close:>9}{t.pre_high:>9}"
              f"{t.entry:>8}{t.exit:>8}{t.reason:>5}{t.ret*100:>8.2f}%")

    n = len(trades)
    wins = [t for t in trades if t.ret > 0]
    avg = sum(t.ret for t in trades) / n
    # 동일가중 순차 복리 + MDD
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= (1 + t.ret)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    print("-" * 80)
    print(f"트레이드 {n}건 | 승률 {len(wins)/n*100:.1f}% | 평균손익 {avg*100:+.2f}% "
          f"| 누적(복리) {(eq-1)*100:+.1f}% | MDD {mdd*100:.1f}%")
    by_reason = {}
    for t in trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    print(f"청산 사유: " + ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items())))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-06-27")
    ap.add_argument("--pre-surge", type=float, default=5.0, help="프리장 급등 임계(퍼센트)")
    ap.add_argument("--drop", type=float, default=3.0, help="[v1] 9:00~9:30 폭락 임계(퍼센트)")
    ap.add_argument("--tp", type=float, default=5.0, help="[v1] 익절(퍼센트)")
    ap.add_argument("--sl", type=float, default=3.0, help="[v1] 손절(퍼센트)")
    ap.add_argument("--mode", choices=["v1", "v2"], default="v2",
                    help="v1=눌림 무조건매수 / v2=지지+재폭등 돌파매수(기본)")
    ap.add_argument("--pullback-min", type=float, default=3.0, help="[v2] 아침고점 대비 눌림 최소(퍼센트)")
    ap.add_argument("--support-tol", type=float, default=0.5, help="[v2] 지지 이탈 허용(퍼센트)")
    ap.add_argument("--tp-levels", default="5,10,15,20,25",
                    help="[v2] 분할 익절 구간 CSV(퍼센트). 둘째 구간 10 은 정적VI 근사")
    ap.add_argument("--stop", type=float, default=4.0, help="[v2] 손절(진입가 대비 퍼센트)")
    ap.add_argument("--codes", help="대상 종목코드 CSV (지정 시 이 종목만; 미지정이면 등록 전체)")
    args = ap.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    fetch_start = start - timedelta(days=5)  # 전일종가 확보용 여유
    if args.mode == "v1":
        params = dict(pre_surge=args.pre_surge / 100, drop=args.drop / 100,
                      tp=args.tp / 100, sl=args.sl / 100)
        rule = f"프리장+{args.pre_surge}% / 폭락-{args.drop}% / TP+{args.tp}% / SL-{args.sl}%"
    else:
        tp_levels = tuple(float(x) / 100 for x in args.tp_levels.split(",") if x.strip())
        params = dict(pre_surge=args.pre_surge / 100, pullback_min=args.pullback_min / 100,
                      support_tol=args.support_tol / 100, tp_levels=tp_levels,
                      stop_pct=args.stop / 100)
        rule = (f"프리장+{args.pre_surge}% / 눌림-{args.pullback_min}% / "
                f"분할익절 {args.tp_levels}% / 손절-{args.stop}%")

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    universe = _stock_universe(codes)
    if not universe:
        raise SystemExit("active sector_stocks 가 없음 — 웹앱에서 종목 등록 후 실행.")
    print(f"[{args.mode}] 대상 {len(universe)}종목 | 기간 {start}~{end} | {rule}")

    con = _cache_conn()
    all_trades: list[Trade] = []
    with TossClient() as client:
        for code, name in universe:
            _ensure_cached(con, client, code, fetch_start, end)
            all_trades.extend(backtest_symbol(con, code, name, start, end, mode=args.mode, **params))
    con.close()

    _report(all_trades)


if __name__ == "__main__":
    main()
