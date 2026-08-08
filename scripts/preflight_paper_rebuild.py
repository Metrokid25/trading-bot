"""Paper 전체 체인 재구축 전 historical 분봉/NXT 근거를 읽기 전용 점검한다.

운영 DB를 수정하지 않는다. 종료코드 0은 전 code-day 근거·분봉 완결성 통과,
2는 registry 누락 또는 불완전 세션 존재를 뜻한다.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.paper_runner import (  # noqa: E402
    CONFIRMED_EMPTY_SESSION_OVERRIDES, _load_nxt_registry,
    session_coverages_many, session_quality,
)


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", type=Path, default=ROOT / "db" / "paper.db")
    parser.add_argument(
        "--cache-db", type=Path, default=ROOT / "db" / "toss_candles.db")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()

    paper = _readonly(args.paper_db)
    query = "SELECT day,code FROM paper_universe_log"
    params: list[str] = []
    clauses = []
    if args.start:
        clauses.append("day>=?")
        params.append(args.start.isoformat())
    if args.end:
        clauses.append("day<=?")
        params.append(args.end.isoformat())
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY day,code ORDER BY day,code"
    memberships: dict[date, set[str]] = defaultdict(set)
    for day_s, code in paper.execute(query, params):
        memberships[date.fromisoformat(day_s)].add(code)
    paper.close()
    if not memberships:
        print("PRECHECK BLOCK: paper_universe_log 범위가 비어 있음")
        return 2

    effective_from, effective_to, selected_codes, detail = _load_nxt_registry()
    missing = sorted({
        (day, code) for day, codes in memberships.items() for code in codes
        if not effective_from <= day <= effective_to
    })
    if missing:
        sample = ",".join(f"{day}/{code}" for day, code in missing[:12])
        print(f"PRECHECK BLOCK: NXT registry 누락 {len(missing)} code-day: {sample}")
        return 2

    days = sorted(memberships)
    codes = sorted({code for values in memberships.values() for code in values})
    eligibility = {
        day: {code: code in selected_codes for code in memberships[day]}
        for day in days}
    cache = _readonly(args.cache_db)
    coverage = session_coverages_many(
        cache, days, codes, eligibility_by_day=eligibility)
    cache.close()

    blocked = []
    for day in days:
        if day in CONFIRMED_EMPTY_SESSION_OVERRIDES:
            continue
        active = [row for row in coverage[day] if row.code in memberships[day]]
        ok, note = session_quality(active)
        if not ok:
            blocked.append((day, note))
    if blocked:
        for day, note in blocked[:12]:
            print(f"PRECHECK BLOCK: {day} {note}")
        print(f"PRECHECK BLOCK: 불완전 세션 {len(blocked)}일")
        return 2

    print(
        f"PRECHECK PASS: days={len(days)},codes={len(codes)},"
        f"registry={detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
