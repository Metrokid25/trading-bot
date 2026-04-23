"""KST 시간 유틸리티."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """현재 시각을 KST-aware datetime으로 반환."""
    return datetime.now(tz=_KST)


def to_db_iso(dt: datetime) -> str:
    """datetime → KST 기준 naive ISO 문자열 (DB 저장용).

    aware datetime은 KST로 변환 후 tzinfo를 제거한 isoformat을 반환.
    naive datetime은 그대로 isoformat (하위호환).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(_KST).replace(tzinfo=None)
    return dt.isoformat()
