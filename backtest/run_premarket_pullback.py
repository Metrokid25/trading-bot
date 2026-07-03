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
                 *, pre_surge: float, drop: float, tp: float, sl: float,
                 **_ignore) -> Trade | None:
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


def _gate_and_resample(day_bars: list[Bar], prev_close: int,
                       pre_surge: float) -> tuple[float, list[Bar]] | None:
    """v2/v3 공용 전처리: 프리장 급등 게이트 통과 시 (프리고가, 정규장 3분봉)."""
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
    return float(pre_high), bars3


def evaluate_day_v2(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                    *, pre_surge: float, pullback_min: float, support_tol: float,
                    tp_levels: tuple[float, ...], stop_pct: float,
                    consol_bars: int = 3, **_ignore) -> Trade | None:
    """저점 지지 + 다지기 + 재폭등 돌파 진입 + 분할 익절 청산 (당일 스캘핑).

    ① 프리장 급등(전일종가 대비 +pre_surge%) 게이트.
    ② 아침 고점에서 pullback_min% 이상 눌림 발생.
    ③ 저점 지지: 눌림 저점을 support_tol% 넘게 깨지 않고 consol_bars봉 이상
       다짐(깨지면 저점 하향 + 다지기 카운트 리셋).
    ④ 재폭등: 눌림 직전 아침고점을 종가로 재돌파 → 진입.
       다지기 봉수를 채우기 전에 재돌파가 먼저 나오면 추격하지 않고 구조를
       버린 뒤 다음 눌림을 기다린다(추격 금지).
    청산: tp_levels 각 구간에서 1/N 분할 익절(+10%≈정적VI 근사). 첫 익절 후
          본절(진입가) 회귀 시 잔량 전량 청산. 손절 진입가 -stop_pct%.
    consol_bars=0 이면 구(舊) v2 와 동일(다지기 무조건 통과).
    """
    gated = _gate_and_resample(day_bars, prev_close, pre_surge)
    if gated is None:
        return None
    pre_high, bars3 = gated

    day_high = float(pre_high)        # 아침 고점(프리장 고점부터 누적)
    breakout_level: float | None = None   # 눌림 직전 고점 = 재돌파 기준선
    pullback_low: float | None = None     # 지지 저점
    hold = 0                              # 지지 유지(다지기) 봉수
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
                hold = 0
            continue
        # ③ 지지 이탈 → 저점 하향 + 다지기 리셋
        if b.low < pullback_low * (1 - support_tol):
            pullback_low = float(b.low)
            hold = 0
            continue
        # ④ 재폭등: 아침고점 종가 재돌파 (직전까지 다지기 consol_bars봉 이상)
        if b.close > breakout_level:
            if hold >= consol_bars:
                entry, entry_i = float(b.close), i
                break
            # 다지기 미완 재돌파 = 기회 소멸 → 새 눌림 대기 (추격 금지)
            pullback_low, breakout_level = None, None
            continue
        pullback_low = min(pullback_low, b.low)
        hold += 1
    if entry is None or entry <= 0:
        return None

    exit_avg, reason, _ = _split_exit(bars3, entry_i, entry, tp_levels, stop_pct)
    return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                 int(entry), int(round(exit_avg)), reason, (exit_avg - entry) / entry)


def _split_exit(bars3: list[Bar], entry_i: int, entry: float,
                tp_levels: tuple[float, ...], stop_pct: float,
                stop_floor: float = 0.0,
                trail_pct: float = 0.0) -> tuple[float, str, int]:
    """v2/v3/acc 공용 분할 익절 청산. (평균청산가, 사유, 익절구간수) 반환.

    stop_floor: 구조적 손절선(지지 저점 이탈가). 0 이면 미사용.
    스승님: 지지선이 "무너지면 가차없이 정리해야 하겠지요" (article 89144)
    trail_pct: 첫 익절 후 고점 대비 -trail_pct 로 손절선 상향(꼭지 근처 잔량
    청산, 사유 'TR'). 0 이면 미사용 — v2/v3 는 기본 끔.
    """
    n = len(tp_levels)
    tps = [entry * (1 + lv) for lv in tp_levels]
    fills: list[float] = []
    stop = max(entry * (1 - stop_pct), stop_floor)  # 더 타이트한 쪽
    armed = False
    tp_i = 0
    stop_kind = "SL"
    exit_kind = "EOD"
    for b in bars3[entry_i + 1:]:
        if b.low <= stop:
            fills += [stop] * (n - len(fills))
            exit_kind = stop_kind
            break
        while tp_i < n and b.high >= tps[tp_i]:
            fills.append(tps[tp_i])
            tp_i += 1
            if not armed:
                armed, stop, stop_kind = True, float(entry), "BE"
        if len(fills) >= n:
            break
        if armed and trail_pct > 0:
            trail = b.high * (1 - trail_pct)
            if trail > stop:
                stop, stop_kind = trail, "TR"
    if len(fills) < n:
        fills += [bars3[-1].close] * (n - len(fills))
    reason = f"{tp_i}TP" + (f"/{exit_kind}" if tp_i < n else "")
    return sum(fills) / n, reason, tp_i


def evaluate_day_acc(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                     *, pre_surge: float, pullback_min: float, support_tol: float,
                     entry_bands: tuple[float, ...], stop_pct: float,
                     tp_levels: tuple[float, ...], trail_pct: float = 0.0,
                     consol_bars: int = 0, **_ignore) -> Trade | None:
    """저점 분할 매집 + 재폭등 분할 익절 (형 실매매 방식 — PC 세션 구현).

    v2와 게이트·눌림·청산은 같고 진입만 뒤집는다:
      v2 = 재돌파에서 1회 매수(어깨) / acc = 지지선 근처에서 분할 매집(무릎 아래).
    v3(무릎 진입)와도 다르다 — v3 는 반등 확인 후 시장가 1회, acc 는 확인 전
    지지선에 지정가 여러 장을 깔아 평단을 낮춘다(미체결 리스크와 맞바꿈).

    ① 프리장 급등 게이트.
    ② 눌림 발생 + 지지 저점 형성.
    ③ 다지기 확인: 저점이 consol_bars 봉 동안 -support_tol 넘게 깨지지 않아야
       매집 개시(떨어지는 칼날 회피). 다지는 중 깨지면 새 저점으로 카운트 리셋.
    ④ 매집: 지지선 기준 entry_bands(%) 레벨에 분할 지정가. 봉 저가가 닿으면 체결.
    ⑤ 손절: 매집 중 지지선 -stop_pct 이탈 → 체결분 전량.
    ⑥ 청산: 재돌파 후 평균진입가 기준 분할 익절. 첫 익절 후 trail_pct>0 이면
       트레일링 스탑(고점 대비 -trail_pct)으로 잔량을 꼭지 근처에서 청산.
    """
    gated = _gate_and_resample(day_bars, prev_close, pre_surge)
    if gated is None:
        return None
    pre_high, bars3 = gated

    day_high = float(pre_high)
    breakout_level: float | None = None
    pullback_low: float | None = None
    buy_levels: list[float] = []     # 미체결 매수 레벨(높은→낮은)
    fills: list[float] = []          # 체결가(각 1/N)
    stop_line: float | None = None
    entry_done_i = -1                # 재돌파(매집 종료) 봉 인덱스
    consol = 0                       # 지지 다지기 카운트(저점 유지 봉 수)

    for i, b in enumerate(bars3):
        prev_high = day_high
        day_high = max(day_high, b.high)

        # ② 눌림 최초 등록
        if pullback_low is None:
            if b.low <= prev_high * (1 - pullback_min):
                pullback_low = float(b.low)
                breakout_level = prev_high
            continue

        # --- 지난 봉까지 세팅된 주문/손절선만 이번 봉에서 작동(룩어헤드 금지) ---

        # ④ 매집: 봉 저가가 닿은 미체결 레벨 체결. 손절 판정보다 먼저 —
        #    지정가는 전부 손절선 위라 하락 경로상 손절 전에 반드시 먼저 닿는다.
        if buy_levels:
            still: list[float] = []
            for lv in buy_levels:
                if b.low <= lv:
                    fills.append(lv)
                else:
                    still.append(lv)
            buy_levels = still

        # ⑤ 손절: 보유 중 지지선 이탈(같은 봉 체결분 포함 — 보수적으로 손절 우선)
        if fills and stop_line is not None and b.low <= stop_line:
            avg_entry = sum(fills) / len(fills)
            return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                         int(round(avg_entry)), int(round(stop_line)), "SL",
                         (stop_line - avg_entry) / avg_entry)

        # ⑥ 재돌파 → 매집 종료, 청산 단계로
        if breakout_level is not None and fills and b.close > breakout_level:
            entry_done_i = i
            break

        # --- 현재 봉을 반영한 구조 갱신(세팅된 주문은 다음 봉부터 유효) ---

        # ③ 다지기: 지지 유지면 카운트++, 저점 깨지면 새 저점으로 리셋
        if b.low < pullback_low * (1 - support_tol):
            pullback_low = float(b.low)
            consol = 0
            if not fills:                 # 아직 매집 전이면 매수 레벨도 리셋
                buy_levels = []
                stop_line = None
        else:
            pullback_low = min(pullback_low, b.low)
            consol += 1

        # ③ 다지기 확인(consol_bars 봉 유지) 후 매수 레벨 1회 세팅(지지 기준)
        if consol >= consol_bars and not buy_levels and not fills:
            support = pullback_low
            buy_levels = sorted((support * (1 + x / 100) for x in entry_bands), reverse=True)
            stop_line = support * (1 - stop_pct)

    if not fills:
        return None  # 한 번도 매집 못함

    avg_entry = sum(fills) / len(fills)
    if entry_done_i < 0:                      # 재돌파 없이 끝 → 종가 청산
        exit_px = float(bars3[-1].close)
        return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                     int(round(avg_entry)), int(round(exit_px)), "EOD",
                     (exit_px - avg_entry) / avg_entry)

    exit_avg, reason, _ = _split_exit(bars3, entry_done_i, avg_entry, tp_levels,
                                      stop_pct, trail_pct=trail_pct)
    return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                 int(round(avg_entry)), int(round(exit_avg)), reason,
                 (exit_avg - avg_entry) / avg_entry)


def evaluate_day_v3(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                    *, pre_surge: float, pullback_min: float, support_tol: float,
                    tp_levels: tuple[float, ...], stop_pct: float,
                    waist_ratio: float, consol_bars: int,
                    vol_dryup_max: float, vol_confirm_ratio: float,
                    pullback_frac: float = 0.0, max_surge: float = 0.0,
                    entry_until: time | None = None,
                    **_ignore) -> Trade | None:
    """아카이브(굿머닝 카페) 근거 기반 매수타점 — 진바닥 확인 후 '무릎' 진입.

    매수 규칙과 근거 (mentor.db article_id):
      ① 프리장 급등 게이트 (v2 동일).
      ② 눌림 발생: 추격 금지, 눌림을 공략 —
         "눌림에서 받아라. 아니면 음봉공략을 한다"(37232),
         "오르는 것이 보이고 나서 따라잡으면 돈이 안된다 …
          눌림목의 끝자락을 공략을 해야 돈이 됩니다"(162616).
      ③ 허리 필터: 눌림 저점이 급등폭(전일종가→프리고점)의 waist_ratio 지점을
         깨면 무효 — "폭등이 시작된 날 허리지점 … 허리가 꺽이지 않으면
         다시 간다"(74834).
      ④ 진바닥·다지기 확인:
         - 저점 안 깨고 consol_bars 봉 이상 다짐. 새 저저점이 나오면 리셋 —
           "파동의 끝이 항상 직전 저점보다 높아야 한다, 이것이 핵심"(65844),
           "음봉이 두 세개 나오면서 … 지지되면 매수에 가담해도 된다"(85534).
         - 다지기 구간 거래량이 하락 구간보다 마름 —
           "거래량이 억눌리면서 … 음봉은 그렇게 위협적이지 않다"(49434),
           "거래량이 줄어들면서 주가하락이 하방경직성을 보이는 때가
            분할매수로 대응할 수 있는 포인트"(29606), (128259 동지).
      ⑤ 진입(무릎): 확인 후 다지기 박스 고가를 종가로 상향 돌파하는 양봉 +
         거래량 실림에 진입 —
         "진바닥을 확인하고 다시 치고 올라올 때 매수해야 한다,
          이것이 무릎에서 사는것이다"(29102),
         "진바닥을 확인하고 꼬였던 수급이 개선되는 시점 무릎에서 매수하는
          것이 정석적인 매수타이밍"(30600),
         "수급이 개선되면 … 거래량이 터져줘야 한다"(30602),
         "이 거래량이 양봉가래량이다 이것이 중요하지요"(114614).
         단 진입 종가가 아침고점(재돌파 기준선) 이상이면 추격이므로 스킵(162616).
      청산: v2 와 동일한 분할 익절 + 구조적 손절(지지 저점 이탈, 89144).

    선택 필터 (2026-07-03 깔때기 진단 기반):
      - pullback_frac>0: 눌림 등록을 고정 pullback_min% 대신 급등폭 비례
        되돌림(등록시점 고점~전일종가 상승폭의 frac)으로. 허리(waist)도 같은
        상승폭 기준으로 계산 — 고정 3% 눌림이 작은 급등(+6.4% 미만)에서
        허리와 수학적으로 양립 불가한 결함 수정. 허리 비례 사고는 74834.
      - max_surge>0: 프리장 급등 상한(탈진 컷). 6월 23종목에서 +12%↑ 일관
        실패(승률 17%)로 검증된 필터.
      - entry_until: 진입 마감 시각. 늦은 진입은 첫 익절까지 시간 부족
        (6월 진입 11건 중 5건이 12시 이후 → 전부 0TP 부근 청산). 데이터 근거.
    """
    gated = _gate_and_resample(day_bars, prev_close, pre_surge)
    if gated is None:
        return None
    pre_high, bars3 = gated
    if max_surge > 0 and (pre_high - prev_close) / prev_close > max_surge:
        return None                       # 탈진 컷

    waist = prev_close + waist_ratio * (pre_high - prev_close)  # ③ 허리(74834)
    day_high = float(pre_high)
    breakout_level: float | None = None   # 눌림 직전 고점(추격 금지 상한, 162616)
    pullback_low: float | None = None
    down_vols: list[int] = []      # 하락(저점 갱신) 구간 거래량
    consol_vols: list[int] = []    # 다지기 구간 거래량
    consol_high: float | None = None
    entry = None
    entry_i = -1
    for i, b in enumerate(bars3):
        prev_high = day_high
        day_high = max(day_high, b.high)
        # ② 눌림 최초 등록: 고정 pullback_min% 또는 급등폭 비례(pullback_frac)
        if pullback_low is None:
            if pullback_frac > 0:
                trigger = prev_high - pullback_frac * (prev_high - prev_close)
            else:
                trigger = prev_high * (1 - pullback_min)
            if b.low <= trigger:
                pullback_low = float(b.low)
                breakout_level = prev_high
                down_vols = [b.volume]
                if pullback_frac > 0:   # 허리도 같은 상승폭 기준으로 재계산
                    waist = prev_close + waist_ratio * (breakout_level - prev_close)
            continue
        # ③ 허리 이탈 → 급등 구조 무효 (74834)
        if waist_ratio > 0 and b.low < waist:
            return None
        # 저저점 갱신 → 진바닥 아님, 다지기 리셋 (65844)
        if b.low < pullback_low * (1 - support_tol):
            pullback_low = float(b.low)
            down_vols += consol_vols + [b.volume]
            consol_vols, consol_high = [], None
            continue
        # ⑤ 진입 판정: 직전 다지기 박스 기준 (현재봉 반영 전)
        if consol_high is not None and len(consol_vols) >= consol_bars:
            consol_avg = sum(consol_vols) / len(consol_vols)
            down_avg = sum(down_vols) / len(down_vols)
            if (consol_avg > 0                                        # 거래 죽은 날 제외
                    and consol_avg <= vol_dryup_max * down_avg        # ④ 마름
                    and b.close > b.open                              # 양봉(114614)
                    and b.close > consol_high                         # 되치기(29102)
                    and b.volume >= vol_confirm_ratio * consol_avg    # 실림(30602)
                    and b.close < breakout_level                      # 추격금지(162616)
                    and (entry_until is None or b.ts.time() <= entry_until)):
                entry, entry_i = float(b.close), i
                break
        # ④ 다지기 누적
        consol_vols.append(b.volume)
        consol_high = b.high if consol_high is None else max(consol_high, b.high)
    if entry is None or entry <= 0:
        return None

    stop_floor = pullback_low * (1 - support_tol)  # 지지 이탈 = 정리 (89144)
    exit_avg, reason, _ = _split_exit(bars3, entry_i, entry, tp_levels, stop_pct,
                                      stop_floor)
    return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                 int(entry), int(round(exit_avg)), reason, (exit_avg - entry) / entry)


def _ladder_exit_v4(bars3: list[Bar], start_i: int, avg_cost: float,
                    tp_levels: tuple[float, ...], stop_pct: float, stop_floor: float,
                    prev_vol: int, vol_exit_ratio: float,
                    vol_exit_after: time) -> tuple[float, str]:
    """v4 청산: 분할 익절 + 본절 + 구조적 손절 + 거래량 130% 조기 청산(49434).

    "오후장에서 거래량이 전일 거래량의 130%를 넘어서고 양봉을 만들지 못한다,
     이런경우 차익실현에 대한 매물출회 압력이 확대되고 있는중이다"(49434)
    → 오후(vol_exit_after~)에 당일 누적 거래량 ≥ vol_exit_ratio × 전일 총량이고
      그 봉이 음봉이면 잔량을 그 봉 종가에 청산(reason 'VOL').
    """
    n = len(tp_levels)
    tps = [avg_cost * (1 + lv) for lv in tp_levels]
    fills: list[float] = []
    stop = max(avg_cost * (1 - stop_pct), stop_floor)
    armed = False
    tp_i = 0
    stop_kind = "SL"
    exit_kind = "EOD"
    cum_vol = sum(b.volume for b in bars3[:start_i + 1])
    for b in bars3[start_i + 1:]:
        cum_vol += b.volume
        if b.low <= stop:
            fills += [stop] * (n - len(fills))
            exit_kind = stop_kind
            break
        while tp_i < n and b.high >= tps[tp_i]:
            fills.append(tps[tp_i])
            tp_i += 1
            if not armed:
                armed, stop, stop_kind = True, float(avg_cost), "BE"
        if len(fills) >= n:
            break
        if (vol_exit_ratio > 0 and prev_vol > 0 and b.ts.time() >= vol_exit_after
                and cum_vol >= vol_exit_ratio * prev_vol and b.close < b.open):
            fills += [float(b.close)] * (n - len(fills))
            exit_kind = "VOL"
            break
    if len(fills) < n:
        fills += [float(bars3[-1].close)] * (n - len(fills))
    reason = f"{tp_i}TP" + (f"/{exit_kind}" if tp_i < n else "")
    return sum(fills) / n, reason


def evaluate_day_v4(symbol: str, name: str, day_bars: list[Bar], prev_close: int,
                    *, pre_surge: float, pullback_min: float, support_tol: float,
                    tp_levels: tuple[float, ...], stop_pct: float,
                    waist_ratio: float, consol_bars: int,
                    vol_dryup_max: float, vol_confirm_ratio: float,
                    pullback_frac: float = 0.0, max_surge: float = 0.0,
                    entry_until: time | None = None,
                    scout_frac: float = 0.2, wick_min: float = 0.5,
                    vol_exit_ratio: float = 1.3, prev_vol: int = 0,
                    **_ignore) -> Trade | None:
    """v3 + 아카이브 조합 확장: 선발대 2단 진입 + 바닥 신호 + 거래량 조기 청산.

    v3 대비 추가 규칙과 근거 (mentor.db article_id):
      ⑥ 선발대(scout) 진입: 다지기 중 바닥 신호가 나오면 scout_frac 만큼 먼저
         진입, 무릎 확인 시 잔량 진입 —
         "선발대로 나서는 분할매수 비중은 통상 20% 미만이 적당"(53601),
         "왜 매수하지 않고 선발대를 보내나? 기술적 반등일수 잇기 때문"(69581),
         "선발대를 보내서 향후 주가 방향을 체크 합니다, 연후 수급이 들어오면
          추매하도록 하겟습니다"(54546).
         바닥 신호 = 쌍바닥(저점 존 2회 이상 터치 후 지지) 또는 아래꼬리봉:
         "저점 인근에서 쌍바닥을 한번더 만들어야 상승추세의 동력을 얻게된다"(92522),
         "쌍바닥을 만든 이후 아래꼬리를 달아서 올릴 때 … 관심을 가질수 있는
          자리"(69581), "바닥을 다지고 아래꼬리를 단다, 이런 경우 내일은 반등을
          모색할수 있다"(68486), "아래꼬리가 매매공방이 일어나고 있는데 매수세가
          강하다"(68828).
         (아래꼬리 '길게'의 정량값 wick_min=0.5 는 임의 — 아카이브는 방향만)
      ⑦ 거래량 조기 청산: _ladder_exit_v4 참조(49434). EOD 본전 물림 축소 목적.
      선발대 후 지지 이탈 시 선발대만 구조적 손절(89144) — 무릎 미확인 손실은
      scout_frac 비중으로 제한. 수익률은 투입 비중 가중(미투입분 0%).
    """
    gated = _gate_and_resample(day_bars, prev_close, pre_surge)
    if gated is None:
        return None
    pre_high, bars3 = gated
    if max_surge > 0 and (pre_high - prev_close) / prev_close > max_surge:
        return None

    waist = prev_close + waist_ratio * (pre_high - prev_close)
    day_high = float(pre_high)
    breakout_level: float | None = None
    pullback_low: float | None = None
    down_vols: list[int] = []
    consol_vols: list[int] = []
    consol_high: float | None = None
    touches = 0                      # 저점 존 터치 횟수(쌍바닥 판정, 반등 후 재시험만 카운트)
    left_zone = False                # 마지막 터치 후 존을 벗어났는가
    scout_px: float | None = None    # 선발대 체결가
    scout_i = -1
    entry_px: float | None = None    # 본대(무릎) 체결가
    entry_i = -1
    scout_stopped = False
    for i, b in enumerate(bars3):
        prev_high = day_high
        day_high = max(day_high, b.high)
        if pullback_low is None:
            if pullback_frac > 0:
                trigger = prev_high - pullback_frac * (prev_high - prev_close)
            else:
                trigger = prev_high * (1 - pullback_min)
            if b.low <= trigger:
                pullback_low = float(b.low)
                breakout_level = prev_high
                down_vols = [b.volume]
                touches, left_zone = 1, False
                if pullback_frac > 0:
                    waist = prev_close + waist_ratio * (breakout_level - prev_close)
            continue
        if waist_ratio > 0 and b.low < waist:
            if scout_px is not None:              # 구조(허리) 붕괴 → 선발대 정리
                scout_stopped = True              # (선발대는 허리 붕괴에서만 손절;
            break                                 #  잔노이즈 이탈은 흥정 지속, 53601)
        if b.low < pullback_low * (1 - support_tol):
            pullback_low = float(b.low)           # 지지 이탈 → 저점 하향 재무장
            down_vols += consol_vols + [b.volume]  # (선발대는 유지)
            consol_vols, consol_high = [], None
            touches, left_zone = 1, False
            continue
        in_zone = b.low <= pullback_low * (1 + support_tol)
        if in_zone:
            if left_zone:               # 반등 후 재시험만 새 터치로 인정(92522)
                touches += 1
            left_zone = False
        else:
            left_zone = True
        # ⑤ 본대(무릎) 진입 판정 — v3 과 동일 조건
        if consol_high is not None and len(consol_vols) >= consol_bars:
            consol_avg = sum(consol_vols) / len(consol_vols)
            down_avg = sum(down_vols) / len(down_vols)
            if (consol_avg > 0
                    and consol_avg <= vol_dryup_max * down_avg
                    and b.close > b.open
                    and b.close > consol_high
                    and b.volume >= vol_confirm_ratio * consol_avg
                    and b.close < breakout_level
                    and (entry_until is None or b.ts.time() <= entry_until)):
                entry_px, entry_i = float(b.close), i
                break
        # ⑥ 선발대: 거래량 마름(29606) + [쌍바닥 재시험(92522) 또는 아래꼬리(69581)]
        if scout_px is None and scout_frac > 0 and len(consol_vols) >= 2:
            consol_avg_s = sum(consol_vols) / len(consol_vols)
            down_avg_s = sum(down_vols) / len(down_vols)
            dryup_ok = 0 < consol_avg_s <= vol_dryup_max * down_avg_s
            rng = b.high - b.low
            hammer = (in_zone and rng > 0
                      and (min(b.open, b.close) - b.low) >= wick_min * rng)
            dbl_bottom = touches >= 2 and b.close >= b.open
            if (dryup_ok and (hammer or dbl_bottom)
                    and (entry_until is None or b.ts.time() <= entry_until)):
                scout_px, scout_i = float(b.close), i
        consol_vols.append(b.volume)
        consol_high = b.high if consol_high is None else max(consol_high, b.high)

    if entry_px is None and scout_px is None:
        return None
    stop_floor = (pullback_low or 0.0) * (1 - support_tol)

    if scout_stopped:                             # 선발대만 물리고 허리 붕괴
        ret = scout_frac * (waist - scout_px) / scout_px
        return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                     int(scout_px), int(round(waist)), "0TP/SL(s)", ret)

    if entry_px is not None and scout_px is not None:      # 선발대+본대
        w_s, w_m = scout_frac, 1 - scout_frac
        avg_cost = w_s * scout_px + w_m * entry_px
        invested = 1.0
        start_i = entry_i
        tag = ""
    elif entry_px is not None:                              # 본대 단독(v3 동일)
        avg_cost, invested, start_i, tag = entry_px, 1.0, entry_i, ""
    else:                                                   # 선발대 단독(무릎 미확인)
        avg_cost, invested, start_i, tag = scout_px, scout_frac, scout_i, "(s)"

    exit_avg, reason = _ladder_exit_v4(bars3, start_i, avg_cost, tp_levels,
                                       stop_pct, stop_floor, prev_vol,
                                       vol_exit_ratio, time(12, 0))
    ret = invested * (exit_avg - avg_cost) / avg_cost
    return Trade(symbol, name, bars3[0].ts.date(), prev_close, int(pre_high),
                 int(round(avg_cost)), int(round(exit_avg)), reason + tag, ret)


_EVALUATORS = {"v1": evaluate_day, "v2": evaluate_day_v2, "v3": evaluate_day_v3,
               "v4": evaluate_day_v4, "acc": evaluate_day_acc}


def _prev_stats(days: dict[date, list[Bar]], ordered: list[date], i: int) -> tuple[int, int]:
    """직전 거래일 정규장의 (마지막 종가, 총 거래량)."""
    for pd in reversed(ordered[:i]):
        prev_reg = [b for b in days[pd] if REG_OPEN <= b.ts.time() <= REG_CLOSE]
        if prev_reg:
            return prev_reg[-1].close, sum(b.volume for b in prev_reg)
    return 0, 0


def _prev_close(days: dict[date, list[Bar]], ordered: list[date], i: int) -> int:
    """전일 종가 = 직전 거래일의 정규장 마지막 종가."""
    return _prev_stats(days, ordered, i)[0]


def backtest_symbol(con, symbol, name, start, end, *, mode="v2", **params) -> list[Trade]:
    evaluator = _EVALUATORS[mode]
    days = _by_day(_load_bars(con, symbol))
    ordered = sorted(days)
    trades: list[Trade] = []
    for i, d in enumerate(ordered):
        if not (start <= d <= end):
            continue
        pc, pv = _prev_stats(days, ordered, i)
        t = evaluator(symbol, name, days[d], pc, prev_vol=pv, **params)
        if t:
            trades.append(t)
    return trades


def _leader_filter(con, trades: list[Trade], universe: list[tuple[str, str]],
                   start: date, end: date, pre_surge: float,
                   max_surge: float = 0.0) -> list[Trade]:
    """섹터 대장주 필터: 그날 섹터 내 프리장 급등 1등 종목의 트레이드만 유지.

    (형 제안 필터 — 섹터 쏠림에서 팔로워 제거. 섹터 태그 없는 종목은 통과)
    """
    scon = sqlite3.connect(settings.DB_PATH)
    sector = dict(scon.execute(
        "SELECT stock_code, sector_name FROM sector_stocks WHERE tracking_status='active'"))
    scon.close()

    best: dict[tuple[date, str], tuple[float, str]] = {}   # (day, sector) → (surge, code)
    for code, _name in universe:
        if code not in sector:
            continue
        days = _by_day(_load_bars(con, code))
        ordered = sorted(days)
        for i, d in enumerate(ordered):
            if not (start <= d <= end):
                continue
            pc = _prev_close(days, ordered, i)
            if pc <= 0:
                continue
            pre = [b for b in days[d] if PRE_START <= b.ts.time() < PRE_END and b.volume > 0]
            if not pre:
                continue
            surge = (max(b.high for b in pre) - pc) / pc
            if surge < pre_surge:
                continue
            if max_surge > 0 and surge > max_surge:
                continue                # 과열 종목은 대장 후보에서도 제외
            key = (d, sector[code])
            if key not in best or surge > best[key][0]:
                best[key] = (surge, code)
    leaders = {(d, c) for (d, _s), (_sg, c) in best.items()}
    return [t for t in trades
            if t.symbol not in sector or (t.day, t.symbol) in leaders]


# ---------------- 종목 선별(그날 강한 놈 위주) ----------------

def _select_top_n(trades: list[Trade], n: int) -> list[Trade]:
    """같은 날 진입 신호 중 강도(프리장 급등률) 상위 n종목만 채택. n<=0 이면 전체.

    그날 여러 종목이 신호를 줄 때 다 사지 않고 가장 센 놈만 골라, 약세날 동시
    손절(=MDD 주범)을 줄이고 강한 종목에 집중한다.
    """
    if n <= 0:
        return trades
    by_day: dict[date, list[Trade]] = {}
    for t in trades:
        by_day.setdefault(t.day, []).append(t)
    out: list[Trade] = []
    for day in sorted(by_day):
        ranked = sorted(
            by_day[day],
            key=lambda t: (t.pre_high - t.prev_close) / t.prev_close if t.prev_close else 0.0,
            reverse=True,
        )
        out.extend(ranked[:n])
    return out


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
    ap.add_argument("--mode", choices=["v1", "v2", "v3", "v4", "acc"], default="v2",
                    help="v1=눌림 무조건매수 / v2=지지+재폭등 돌파매수(기본) / "
                         "v3=진바닥 확인 후 무릎 진입(아카이브 근거) / "
                         "v4=v3+선발대·바닥신호·거래량 조기청산(아카이브 조합) / "
                         "acc=지지선 분할매집 지정가(형 실매매 방식)")
    ap.add_argument("--pullback-min", type=float, default=3.0, help="[v2/v3] 아침고점 대비 눌림 최소(퍼센트)")
    ap.add_argument("--support-tol", type=float, default=0.5, help="[v2/v3] 지지 이탈 허용(퍼센트)")
    ap.add_argument("--tp-levels", default="5,10,15,20,25",
                    help="[v2/v3] 분할 익절 구간 CSV(퍼센트). 둘째 구간 10 은 정적VI 근사")
    ap.add_argument("--stop", type=float, default=4.0, help="[v2/v3] 손절(진입가 대비 퍼센트)")
    ap.add_argument("--waist", type=float, default=0.5,
                    help="[v3] 허리 필터: 급등폭 대비 이탈 무효 비율(0=끄기). 근거 74834")
    ap.add_argument("--consol-bars", type=int, default=3,
                    help="[v2/v3] 지지 확인 최소 다지기 3분봉 수(0=v2 구버전 동작). "
                         "근거 85534(음봉 두세개)/52205")
    ap.add_argument("--vol-dryup", type=float, default=1.0,
                    help="[v3] 다지기/하락 구간 평균거래량 비율 상한. 근거 29606/49434")
    ap.add_argument("--vol-confirm", type=float, default=2.0,
                    help="[v3] 진입봉 거래량 ≥ 다지기 평균 × 이 값. 근거 30602/114614")
    ap.add_argument("--pullback-frac", type=float, default=0.0,
                    help="[v3] 눌림 등록을 급등폭 비례 되돌림으로(예 0.33). 0=고정 --pullback-min")
    ap.add_argument("--max-surge", type=float, default=0.0,
                    help="[v3] 프리장 급등 상한 퍼센트(탈진 컷, 예 12). 0=끄기")
    ap.add_argument("--entry-until", default="",
                    help="[v3] 진입 마감 시각 HH:MM(이후 진입 금지). 빈값=끄기")
    ap.add_argument("--leader-only", action="store_true",
                    help="섹터 대장주(그날 섹터 내 프리장 급등 1등)만 유지")
    ap.add_argument("--scout-frac", type=float, default=0.2,
                    help="[v4] 선발대 비중(0=끄기). 근거 53601(20%% 미만)")
    ap.add_argument("--wick-min", type=float, default=0.5,
                    help="[v4] 아래꼬리 최소 비율(봉 전체 대비). 근거 69581/68486(방향), 값은 임의")
    ap.add_argument("--vol-exit", type=float, default=1.3,
                    help="[v4] 오후 누적거래량/전일 비율 조기청산(0=끄기). 근거 49434")
    ap.add_argument("--entry-bands", default="1,0,-1",
                    help="[acc] 지지선 기준 분할 매수 레벨 CSV(퍼센트). "
                         "예 '1,0,-1'=지지+1%%/지지/지지-1%%")
    ap.add_argument("--trail", type=float, default=5.0,
                    help="[acc] 트레일링 스탑(첫 익절 후 고점 대비 퍼센트, 0=끄기)")
    ap.add_argument("--top-n", type=int, default=0,
                    help="그날 강도(프리장 급등률) 상위 N종목만 진입. 0=전체(선별 안 함)")
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
        if not tp_levels:
            raise SystemExit("--tp-levels 가 비었음 — 예: --tp-levels 5,10,15")
        params = dict(pre_surge=args.pre_surge / 100, pullback_min=args.pullback_min / 100,
                      support_tol=args.support_tol / 100, tp_levels=tp_levels,
                      stop_pct=args.stop / 100, consol_bars=args.consol_bars)
        rule = (f"프리장+{args.pre_surge}% / 눌림-{args.pullback_min}% / "
                f"다지기{args.consol_bars}봉 / 분할익절 {args.tp_levels}% / 손절-{args.stop}%")
        if args.mode in ("v3", "v4"):
            entry_until = time.fromisoformat(args.entry_until) if args.entry_until else None
            params.update(waist_ratio=args.waist,
                          vol_dryup_max=args.vol_dryup,
                          vol_confirm_ratio=args.vol_confirm,
                          pullback_frac=args.pullback_frac,
                          max_surge=args.max_surge / 100,
                          entry_until=entry_until)
            rule += (f" / 허리{args.waist} / 마름≤{args.vol_dryup} / "
                     f"실림≥{args.vol_confirm}x")
            if args.pullback_frac:
                rule += f" / 눌림=급등의{args.pullback_frac}"
            if args.max_surge:
                rule += f" / 과열컷{args.max_surge}%"
            if args.entry_until:
                rule += f" / 진입~{args.entry_until}"
        if args.mode == "v4":
            params.update(scout_frac=args.scout_frac, wick_min=args.wick_min,
                          vol_exit_ratio=args.vol_exit)
            rule += (f" / 선발대{args.scout_frac} / 꼬리≥{args.wick_min} / "
                     f"VOL청산{args.vol_exit}x")
        if args.mode == "acc":
            entry_bands = tuple(float(x) for x in args.entry_bands.split(",") if x.strip())
            if not entry_bands:
                raise SystemExit("--entry-bands 가 비었음 — 예: --entry-bands 1,0,-1")
            params.update(entry_bands=entry_bands, trail_pct=args.trail / 100)
            rule += f" / 매집[{args.entry_bands}]% / 트레일-{args.trail}%"

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
    if args.leader_only:
        n_before = len(all_trades)
        all_trades = _leader_filter(con, all_trades, universe, start, end,
                                    args.pre_surge / 100, args.max_surge / 100)
        print(f"[leader-only] 팔로워 제거: {n_before} → {len(all_trades)}건")
    con.close()

    if args.top_n > 0:
        n_before = len(all_trades)
        all_trades = _select_top_n(all_trades, args.top_n)
        print(f"[top-n] 강도 상위 {args.top_n}종목/일: {n_before} → {len(all_trades)}건")

    _report(all_trades)


if __name__ == "__main__":
    main()
