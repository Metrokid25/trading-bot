"""만료(expired)된 섹터 픽 복구 — 웹앱 유니버스 롤백 도구.

배경 (2026-07-10): 웹 등록 픽의 유효기간이 7일이라, 등록해둔 유니버스
(9섹터 72종목)가 7/10 장중 일괄 만료돼 유니버스가 증발했다. 이 스크립트는
'expired' 픽을 다시 활성화하고 만료를 1년 연장한다.

- 복구 대상은 status='expired' 이면서 **웹 등록('[web:…]') 또는 유니버스 동기화
  ('[universe-sync]') 픽**만이다. 텔레그램 /p 픽은 7일 수명이 의도된 설계라
  건드리지 않는다 (--all-sources 로 해제 가능).
- 웹앱 "섹터 삭제"로 지운 것은 'archived'라서 복구하지 않는다 (의도적 삭제 존중).
  진단을 돕기 위해 최근 archived 목록은 참고로만 출력한다.
- 픽 등록/유니버스는 미니PC trading.db 가 기준 — **미니PC에서 실행할 것.**

사용 (미니PC):
    ./.venv/Scripts/python.exe scripts/restore_expired_picks.py            # 미리보기
    ./.venv/Scripts/python.exe scripts/restore_expired_picks.py --apply   # 실제 복구
    옵션: --days-back 3     최근 N일 내 만료분만 (기본 3일 — 이번 사고 범위)
          --extend-days 365  복구/연장 후 유효기간(일)
          --all-sources      텔레그램 등 모든 픽 소스 포함 (기본: 웹/동기화만)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from core.time_utils import now_kst, to_db_iso  # noqa: E402

# 웹 등록 + 유니버스 동기화 픽만 대상 (텔레그램 /p 의 7일 수명은 의도된 설계)
_SOURCE_FILTER = "(sp.raw_input LIKE '[web:%' OR sp.raw_input = '[universe-sync]')"


def _print_rows(rows: list[tuple], head: str) -> None:
    print(head)
    for pid, pick_date, expires_at, raw, sectors, n_stocks in rows:
        print(f"  pick_id={pid} 등록일={pick_date} 만료={str(expires_at)[:16]} "
              f"섹터=[{sectors or '-'}] {n_stocks}종목 ({raw})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="실제로 복구 (기본: 미리보기)")
    ap.add_argument("--days-back", type=int, default=3,
                    help="최근 N일 내 만료된 픽만 복구 (기본 3)")
    ap.add_argument("--extend-days", type=int, default=365,
                    help="복구/연장 후 유효기간(일, 기본 365)")
    ap.add_argument("--all-sources", action="store_true",
                    help="텔레그램 등 모든 픽 소스 포함 (기본: 웹/동기화 픽만)")
    ap.add_argument("--db", default=str(settings.DB_PATH), help="trading.db 경로")
    args = ap.parse_args()

    now = now_kst()
    cutoff = to_db_iso(now - timedelta(days=args.days_back))
    new_expires = to_db_iso(now + timedelta(days=args.extend_days))
    src = "1=1" if args.all_sources else _SOURCE_FILTER

    base_select = (
        "SELECT sp.id, sp.pick_date, sp.expires_at, sp.raw_input, "
        "       GROUP_CONCAT(DISTINCT ss.sector_name), COUNT(ss.id) "
        "FROM sector_picks sp "
        "LEFT JOIN sector_stocks ss ON ss.pick_id = sp.id "
        f"WHERE sp.status = ? AND {src} AND {{extra}} "
        "GROUP BY sp.id ORDER BY sp.id"
    )

    con = sqlite3.connect(args.db)
    # 상주 프로세스(웹앱/수집)와 동시 실행 대비 — 저장소 표준 30초 대기
    con.execute("PRAGMA busy_timeout=30000")
    try:
        expired = con.execute(
            base_select.format(extra="sp.expires_at >= ?"),
            ("expired", cutoff)).fetchall()
        # 현재 활성인데 만료가 목표보다 짧은 픽 (오늘 새로 등록한 7일짜리 등)
        active_short = con.execute(
            base_select.format(extra="sp.expires_at < ?"),
            ("active", new_expires)).fetchall()

        mode = "[복구 실행]" if args.apply else "[미리보기]"
        print(f"{mode} 기준시각 {to_db_iso(now)[:16]} → 새 만료 {new_expires[:10]}")

        if expired:
            _print_rows(expired, f"\n① 재활성화 대상 (expired, 최근 {args.days_back}일): {len(expired)}건")
            # 같은 섹터에 이미 활성 픽이 있으면 복구 시 웹앱에 카드가 중복 표시됨
            active_sectors = {r[0] for r in con.execute(
                "SELECT DISTINCT ss.sector_name FROM sector_stocks ss "
                "JOIN sector_picks sp ON sp.id = ss.pick_id "
                "WHERE sp.status='active'")}
            dup = sorted({s for *_r, sectors, _n in expired if sectors
                          for s in str(sectors).split(",") if s in active_sectors})
            if dup:
                print(f"  ⚠️ 이미 활성 픽이 있는 섹터와 중복: {dup} — 복구 후 같은 섹터 카드가"
                      " 2개로 보일 수 있음 (필요 시 오래된 쪽을 웹앱에서 정리)")
        else:
            print(f"\n① 재활성화 대상 없음 (최근 {args.days_back}일 내 만료된 대상 픽 0건)")

        if active_short:
            _print_rows(active_short, f"\n② 만료 연장 대상 (active, 만료 < {new_expires[:10]}): {len(active_short)}건")
        else:
            print("\n② 만료 연장 대상 없음 (활성 픽 전부 이미 충분히 김)")

        # 진단 참고: 최근 archived(웹앱 섹터 삭제) — 복구하지 않음
        archived = con.execute(
            base_select.format(extra="sp.expires_at >= ?"),
            ("archived", cutoff)).fetchall()
        if archived:
            _print_rows(archived, f"\n(참고) 최근 archived {len(archived)}건 — 의도적 삭제로 보고 복구 안 함:")

        if not expired and not active_short:
            return
        if not args.apply:
            print("\n실제 반영하려면 --apply 를 붙여 다시 실행하세요.")
            return

        con.execute("BEGIN IMMEDIATE")
        # 미리보기와 동일 조건 재검증 (status 가드) — 실행 사이 상태 변화에 안전
        con.executemany(
            "UPDATE sector_picks SET status='active', expires_at=? "
            "WHERE id=? AND status='expired'",
            [(new_expires, r[0]) for r in expired],
        )
        con.executemany(
            "UPDATE sector_picks SET expires_at=? "
            "WHERE id=? AND status='active' AND expires_at < ?",
            [(new_expires, r[0], new_expires) for r in active_short],
        )
        con.commit()
        print(f"\n복구 완료: 재활성화 {len(expired)}건 + 만료 연장 {len(active_short)}건."
              " 웹앱 새로고침으로 확인하세요.")
        print("paper_runner 상주 루프는 다음 사이클(≤30분)에 자동 반영됩니다.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
