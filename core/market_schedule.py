"""KST 시간대별 상주 루프 타임테이블 (아카이브봇 --market-schedule 이식).

구간 정의 (경계는 이상~미만):
  23:00~06:00  중단   — 다음 06:00 까지 남은 초만큼 대기
  06:00~07:00  30분
  07:00~08:00  10분
  08:00~16:00   5분   (NXT 프리장 08:00 포함 ~ 정규장 마감)
  16:00~18:00  10분
  18:00~23:00  30분

사용:
  active, wait_s, label = next_action(now_kst())
  active=True  → 작업 1회 실행 후 wait_s 초 대기
  active=False → wait_s 초(다음 06:00까지) 대기 후 재판정
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

# (시작, 끝, 간격초, 라벨) — 이상~미만
WINDOWS: list[tuple[dtime, dtime, int, str]] = [
    (dtime(6, 0), dtime(7, 0), 1800, "dawn"),
    (dtime(7, 0), dtime(8, 0), 600, "pre-open"),
    (dtime(8, 0), dtime(16, 0), 300, "market"),
    (dtime(16, 0), dtime(18, 0), 600, "post-close"),
    (dtime(18, 0), dtime(23, 0), 1800, "evening"),
]

_HALT_START = dtime(23, 0)
_RESUME = dtime(6, 0)


def next_action(now: datetime) -> tuple[bool, float, str]:
    """(active, wait_seconds, label) 반환.

    active=True: 해당 구간 라벨과 반복 간격(초).
    active=False: 중단 구간 — 다음 06:00 까지 남은 초.
    """
    t = now.time()
    for start, end, interval, label in WINDOWS:
        if start <= t < end:
            return True, float(interval), label
    # 중단 구간 (23:00~24:00 또는 00:00~06:00)
    target = now.replace(hour=_RESUME.hour, minute=_RESUME.minute,
                         second=0, microsecond=0)
    if t >= _HALT_START:
        target += timedelta(days=1)
    return False, max((target - now).total_seconds(), 1.0), "halt"
