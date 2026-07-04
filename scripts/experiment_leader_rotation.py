"""주도 섹터/대장주 로테이션 walk-forward 검증 (mentor.db 근거 → OHLCV 번역).

아카이브 근거:
  28650  업종 선택 후 그 안에서 대장주를 찾아야 수익이 좌우된다
  149194 주도 업종은 3~4일 단위로 돌아가며 순환매
  160817 다음 주도 = 눌리고 있다가 하방경직/눌림 탈출을 보이는 업종
  102729 업종 내 순환매 — 추격하면 물린다, 돌아올 때까지 기다려라
  58191  성숙 국면엔 대장주 집중

전부 사전 정보만 사용(신호일 d 기준 d-1까지의 일봉으로 섹터/대장 선정):
  A) 주도섹터(최근 5일 수익률 1위)의 v2 신호 전부
  B) 주도섹터의 '대장'(섹터 내 5일 상대강도 1위)만
  C) 주도섹터 3일 lookback 버전(빠른 순환매, 149194)
  D) '쉬는 주도섹터'(10일 모멘텀 상위 3 + 직전 2일 마이너스)의 v2 신호 — 160817
  E) 전 섹터에서 '그 섹터 대장' 종목의 v2 신호만 (섹터 무관, 대장만)
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, r"C:\trading-bot")

from backtest.run_premarket_pullback import (  # noqa: E402
    REG_CLOSE, REG_OPEN, _cache_conn, _load_bars, _stock_universe,
    backtest_symbol,
)
from config import settings  # noqa: E402

PARAMS = dict(pre_surge=0.05, pullback_min=0.03, support_tol=0.005,
              tp_levels=(0.05, 0.10, 0.15, 0.20, 0.25), stop_pct=0.04,
              consol_bars=3)
# 사용: python leader_rotation_test.py [START] [END]  (기본 1~3월)
_argv = sys.argv[1:]
START = date.fromisoformat(_argv[0]) if len(_argv) > 0 else date(2026, 1, 2)
END = date.fromisoformat(_argv[1]) if len(_argv) > 1 else date(2026, 3, 31)


def stats(label, trades):
    if not trades:
        print(f"  {label:<34} 0건")
        return
    ts = sorted(trades, key=lambda t: (t.day, t.symbol))
    n = len(ts)
    wins = sum(1 for t in ts if t.ret > 0)
    avg = sum(t.ret for t in ts) / n
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in ts:
        eq *= 1 + t.ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    print(f"  {label:<34} {n:>3}건 | 승률 {wins/n*100:4.1f}% | 평균 {avg*100:+5.2f}% "
          f"| 누적 {(eq-1)*100:+6.1f}% | MDD {mdd*100:5.1f}%")


# ---- 섹터 매핑 ----
scon = sqlite3.connect(settings.DB_PATH)
sector_members = defaultdict(set)   # sector -> {code}
for sec, code in scon.execute(
        "SELECT DISTINCT sector_name, stock_code FROM sector_stocks "
        "WHERE tracking_status='active'"):
    sector_members[sec].add(code)
scon.close()

# ---- 일봉(정규장 종가) 구축 + v2 트레이드 ----
con = _cache_conn()
universe = _stock_universe()
daily_close = defaultdict(dict)     # code -> {day: close}
all_trades = []
for code, name in universe:
    bars = _load_bars(con, code)
    for b in bars:
        if REG_OPEN <= b.ts.time() <= REG_CLOSE:
            daily_close[code][b.ts.date()] = b.close   # 마지막 봉이 종가로 남음
    all_trades += backtest_symbol(con, code, name, START, END, mode="v2", **PARAMS)
con.close()
all_trades.sort(key=lambda t: (t.day, t.symbol))

trading_days = sorted({d for m in daily_close.values() for d in m})
day_idx = {d: i for i, d in enumerate(trading_days)}


def stock_ret(code, day, lookback):
    """day 직전 lookback 거래일 수익률 (day 미포함, 사전 정보만)."""
    i = day_idx[day]
    if i < lookback + 1:
        return None
    d_from, d_to = trading_days[i - lookback - 1], trading_days[i - 1]
    c0, c1 = daily_close[code].get(d_from), daily_close[code].get(d_to)
    if not c0 or not c1:
        return None
    return c1 / c0 - 1


def sector_score(sec, day, lookback):
    rets = [r for c in sector_members[sec]
            if (r := stock_ret(c, day, lookback)) is not None]
    return sum(rets) / len(rets) if rets else None


def leading_sectors(day, lookback, top=1):
    scored = [(s, sec) for sec in sector_members
              if (s := sector_score(sec, day, lookback)) is not None]
    scored.sort(reverse=True)
    return [sec for _s, sec in scored[:top]], scored


def sector_leader(sec, day, lookback):
    best, best_c = None, None
    for c in sector_members[sec]:
        r = stock_ret(c, day, lookback)
        if r is not None and (best is None or r > best):
            best, best_c = r, c
    return best_c


by_day = defaultdict(list)
for t in all_trades:
    by_day[t.day].append(t)

sel = {k: [] for k in "ABCDE"}
weekly_log = []
for day in sorted(by_day):
    cands = by_day[day]
    # A/B: 5일 주도섹터
    top5, _ = leading_sectors(day, 5, top=1)
    if top5:
        lead_sec = top5[0]
        members = sector_members[lead_sec]
        sel["A"] += [t for t in cands if t.symbol in members]
        leader = sector_leader(lead_sec, day, 5)
        sel["B"] += [t for t in cands if t.symbol == leader]
        weekly_log.append((day, lead_sec, leader))
    # C: 3일 주도섹터(빠른 순환매)
    top3, _ = leading_sectors(day, 3, top=1)
    if top3:
        sel["C"] += [t for t in cands if t.symbol in sector_members[top3[0]]]
    # D: 쉬는 주도섹터 — 10일 모멘텀 상위3 & 직전 2일 마이너스
    top10, scored10 = leading_sectors(day, 10, top=3)
    resting = [sec for sec in top10
               if (s2 := sector_score(sec, day, 2)) is not None and s2 < 0]
    for sec in resting:
        sel["D"] += [t for t in cands if t.symbol in sector_members[sec]]
    # E: 섹터 무관, 각 섹터 대장(5일 상대강도 1위)의 신호만
    leaders = {sector_leader(sec, day, 5) for sec in sector_members}
    sel["E"] += [t for t in cands if t.symbol in leaders]

print(f"=== 주도섹터/대장 로테이션 walk-forward ({START}~{END}, v2 신호 필터) ===")
stats("[비교] 전체 신호", all_trades)
stats("A) 5일 주도섹터 전부", sel["A"])
stats("B) 5일 주도섹터의 대장만", sel["B"])
stats("C) 3일 주도섹터 전부 (149194)", sel["C"])
stats("D) 쉬는 주도섹터 (160817)", sel["D"])
stats("E) 각 섹터 대장들만 (28650)", sel["E"])

print("\n=== A) 5일 주도섹터 — 월별 분해 (레짐 안정성) ===")
for m in sorted({t.day.month for t in all_trades}):
    stats(f"  {m}월", [t for t in sel["A"] if t.day.month == m])
print("\n=== A 상세 트레이드 ===")
nm = dict(universe)
for t in sorted(sel["A"], key=lambda t: t.day):
    print(f"  {t.day} {nm.get(t.symbol, t.symbol):<10} {t.ret*100:+6.2f}% {t.reason}")

# 주도섹터 로테이션 눈으로 확인 (신호 있던 날만)
print("\n=== 신호일의 5일 주도섹터/대장 로테이션 ===")
prev = None
for day, sec, leader in weekly_log:
    if sec != prev:
        nm = dict(universe).get(leader, leader)
        print(f"  {day} 부터: {sec} (대장 {nm})")
        prev = sec
