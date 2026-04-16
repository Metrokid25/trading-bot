"""외국인/기관 수급 필터.

KIS `inquire-investor` (TR FHKST01010900) → 최근 30영업일치 일자별 순매수.
거래대금 필드 단위는 '백만원'.

게이트 규칙(v3):
  외국인 OR 기관 5일 누적 순매수 >= 500(백만원, = 5억원) → PASS
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from loguru import logger

from config.constants import FLOW_LOOKBACK_DAYS, FLOW_THRESHOLD_MWON
from core.kis_api import KISClient


@dataclass
class DailyFlow:
    code: str
    date: date
    foreign_mwon: int    # 외국인 순매수 거래대금 (백만원)
    institution_mwon: int  # 기관 순매수 거래대금 (백만원)


def _parse_int(s: str | int | None) -> int:
    try:
        return int(s) if s not in (None, "") else 0
    except (ValueError, TypeError):
        return 0


async def get_recent_flow(kis: KISClient, code: str, days: int = FLOW_LOOKBACK_DAYS) -> list[DailyFlow]:
    rows = await kis.get_investor_trend(code)
    out: list[DailyFlow] = []
    for r in rows[:days]:
        d_s = r.get("stck_bsop_date", "")
        if not d_s:
            continue
        try:
            d = datetime.strptime(d_s, "%Y%m%d").date()
        except ValueError:
            continue
        out.append(DailyFlow(
            code=code,
            date=d,
            foreign_mwon=_parse_int(r.get("frgn_ntby_tr_pbmn")),
            institution_mwon=_parse_int(r.get("orgn_ntby_tr_pbmn")),
        ))
    return out


async def flow_passed(
    kis: KISClient,
    code: str,
    lookback: int = FLOW_LOOKBACK_DAYS,
    threshold_mwon: int = FLOW_THRESHOLD_MWON,
) -> tuple[bool, int, int]:
    """외국인 OR 기관 lookback일 누적 순매수 >= threshold 면 통과.

    반환: (passed, foreign_sum_mwon, institution_sum_mwon)
    """
    flows = await get_recent_flow(kis, code, lookback)
    f_sum = sum(f.foreign_mwon for f in flows)
    i_sum = sum(f.institution_mwon for f in flows)
    passed = max(f_sum, i_sum) >= threshold_mwon
    logger.info(
        f"[FLOW] {code} {lookback}일 누적: 외국인={f_sum:+,}백만 기관={i_sum:+,}백만 "
        f"→ {'PASS' if passed else 'FAIL'} (기준 {threshold_mwon:+,}백만)"
    )
    return passed, f_sum, i_sum
