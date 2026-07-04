"""gm_v3 일봉 데이터 소스 — 토스 캐시 합성(1순위) + KIS 보충 + 합성 패딩(최후).

우선순위 (2026-07-04 형 결정):
  1. db/toss_candles.db 1분봉 → 정규장 일봉 합성 (실데이터, 2026-03-27~ 보유)
  2. KIS get_daily_candles 로 그 이전 과거 일봉 보충 (읽기 전용, 60일선용)
  3. 둘 다 부족하면 합성 패딩 — TODO(real-data): 노트북 축적 DB 동기화 또는
     KIS 보충으로 교체. 사용 구간은 러너 리포트에 명시된다.

주의: PC 의 Phase 2.5 축적 테이블(pick_daily_tracking 등)은 0행이라 소스로
사용 불가(2026-07-04 실측). 노트북 DB 동기화 시 1순위로 승격 검토.
"""
from __future__ import annotations

import sqlite3
from datetime import date as Date, datetime, time, timedelta
from pathlib import Path

from strategy.gm_v3.models import DailyBar
from strategy.gm_v3.synth import make_random_walk

TOSS_CACHE_DB = Path(__file__).resolve().parent.parent.parent / "db" / "toss_candles.db"
_PRE_OPEN = time(8, 0)          # NXT 프리마켓 시작
_REG_OPEN, _REG_CLOSE = time(9, 0), time(15, 30)


def load_daily_from_toss(code: str, cache_db: Path = TOSS_CACHE_DB, *,
                         include_premarket: bool = False) -> list[DailyBar]:
    """토스 1분봉 캐시에서 일봉 합성. 캐시에 없는 종목은 빈 리스트.

    include_premarket=True 면 NXT 프리마켓(08:00~)부터 포함 — 시가 = 프리장
    첫 체결가, 고저가/거래량에 프리장 반영(종가는 동일하게 15:30).
    체결 가정 주의: 페이퍼의 '다음날 시가'가 프리장 08:00 체결이 된다.
    """
    if not Path(cache_db).exists():
        return []
    con = sqlite3.connect(cache_db)
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE symbol=? ORDER BY ts", (code,)).fetchall()
    con.close()

    start_t = _PRE_OPEN if include_premarket else _REG_OPEN
    days: dict[Date, list] = {}
    for ts, o, h, l, c, v in rows:
        dt = datetime.fromisoformat(ts)
        if not (start_t <= dt.time() <= _REG_CLOSE):
            continue
        if v <= 0 and dt.time() < _REG_OPEN:
            continue                     # 프리장 무체결 호가봉 제외
        d = dt.date()
        g = days.get(d)
        if g is None:
            days[d] = [o, h, l, c, v]
        else:
            g[1] = max(g[1], h)
            g[2] = min(g[2], l)
            g[3] = c
            g[4] += v
    return [DailyBar(d, o, h, l, c, v)
            for d, (o, h, l, c, v) in sorted(days.items())
            if v > 0]


async def kis_backfill_daily(code: str, before: Date, n_days: int
                             ) -> list[DailyBar]:
    """KIS 일봉으로 before 이전 과거 n_days(달력 기준 여유 포함) 보충.

    시세 API 는 항상 REAL 서버(프로젝트 불변식) — kis_api 를 읽기 전용으로만
    사용한다. 인증/네트워크 실패 시 빈 리스트 반환(러너가 gap 으로 보고).
    """
    try:
        from core.kis_api import KISClient
        kis = KISClient()
        start = (before - timedelta(days=int(n_days * 1.6) + 10)).strftime("%Y%m%d")
        end = (before - timedelta(days=1)).strftime("%Y%m%d")
        rows = await kis.get_daily_candles(code, start, end)
    except Exception:
        return []
    out: list[DailyBar] = []
    for r in rows:
        try:
            d = datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date()
            out.append(DailyBar(d, float(r["stck_oprc"]), float(r["stck_hgpr"]),
                                float(r["stck_lwpr"]), float(r["stck_clpr"]),
                                float(r["acml_vol"])))
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda b: b.day)
    return [b for b in out if b.day < before]


def synth_pad(bars: list[DailyBar], n: int, seed: int) -> list[DailyBar]:
    """실데이터 앞에 합성 일봉 n개 패딩 (지표 워밍업용 더미).

    TODO(real-data): KIS 보충 또는 노트북 축적 DB로 교체할 것.
    """
    if n <= 0 or not bars:
        return bars
    first = bars[0]
    pad = make_random_walk(seed=seed, n=n, base_price=first.close,
                           start=first.day - timedelta(days=int(n * 1.6) + 5))
    pad = [b for b in pad if b.day < first.day][-n:]
    return pad + bars
