"""마이그레이션/이벤트-기록 도입 이전에 등록된 픽을 추적 파이프라인에 편입한다.

배경:
    webapp/텔레그램 등록 경로에 `record_pick_event=True` 가 들어오기 전에 등록된 픽은
    `sector_pick_events` 행이 없다. 이벤트가 없으면 DailyTracker.ensure_tracking_rows 가
    추적 대상으로 잡지 못해 `pick_daily_tracking` 이 영영 비고, 분봉/일봉 수집이 누락된다.
    (분봉은 KIS 가 당일만 제공 → 매 거래일 영구 손실.)

처리:
    1) 활성 종목을 가진 `(pick_id, sector_name)` 그룹 중 이벤트가 없는 것에 대해
       정식 경로와 동일한 `SectorStore._record_sector_pick_event` 로 이벤트를 만든다.
    2) 활성 이벤트 **전체**에 대해 `DailyTracker.ensure_tracking_rows` 로 D+0~D+20
       추적행을 멱등 생성한다(파이프라인 `ensure_all_tracking_rows` 와 동일 로직).

특성:
    - 멱등: 이미 이벤트가 있는 그룹은 다시 만들지 않는다.
    - 자가치유: 이벤트는 있으나 추적행이 비어 있는 그룹도 (2)단계에서 채워진다.
      (1단계는 성공/2단계는 실패했던 부분 실패 케이스를 재실행으로 복구.)
    - pick_date 오름차순 처리 → 동일 섹터 재픽업 gap 계산이 정식 경로와 일치.
    - registered_at 은 픽의 원래 created_at 을 보존(픽 시점 메타 유지).
    - 실행 전 DB 백업(VACUUM INTO 로 일관성 보장).

주의:
    누적 프로세스(main_tracker.py)가 동시에 돌고 있지 않을 때 실행한다.

사용:
    ./.venv/Scripts/python.exe scripts/backfill_pick_events.py            # 적용
    ./.venv/Scripts/python.exe scripts/backfill_pick_events.py --dry-run  # 미리보기
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import date
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가 (scripts/ 하위에서 직접 실행 대비).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.daily_tracker import DailyTracker  # noqa: E402
from core.pipeline_runner import ensure_all_tracking_rows  # noqa: E402
from core.time_utils import now_kst  # noqa: E402
from data.sector_store import SectorStore  # noqa: E402

_BUSY_TIMEOUT_MS = 30_000

# 이벤트가 없는, 활성 종목 보유 (pick_id, sector_name) 그룹.
# pick_date 오름차순 → 같은 섹터 재픽업 gap 계산이 정식 경로와 동일해진다.
_MISSING_EVENT_SQL = """
SELECT ss.pick_id, ss.sector_name, sp.pick_date, sp.created_at, COUNT(*) AS n_stocks
FROM sector_stocks ss
JOIN sector_picks sp ON sp.id = ss.pick_id
WHERE ss.tracking_status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM sector_pick_events spe
      WHERE spe.pick_id = ss.pick_id AND spe.sector_name = ss.sector_name
  )
GROUP BY ss.pick_id, ss.sector_name
ORDER BY sp.pick_date ASC, sp.created_at ASC, ss.pick_id ASC
"""

# 이벤트는 있으나 추적행이 하나도 없는 활성 이벤트(부분 실패 복구 대상).
_ORPHAN_EVENT_SQL = """
SELECT DISTINCT spe.event_id, spe.sector_name, spe.pick_date
FROM sector_pick_events spe
JOIN sector_stocks ss
    ON ss.pick_id = spe.pick_id AND ss.sector_name = spe.sector_name
WHERE ss.tracking_status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM pick_daily_tracking pdt WHERE pdt.event_id = spe.event_id
  )
ORDER BY spe.event_id
"""


def _query(db_path: str, sql: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _backup(db_path: str) -> str:
    """VACUUM INTO 로 일관된 스냅샷 백업을 만든다(라이브 파일 복사보다 안전)."""
    src = Path(db_path)
    # 마이크로초까지 포함 — VACUUM INTO 는 대상 파일이 이미 있으면 거부하므로
    # 같은 초 재실행 충돌을 피한다.
    stamp = now_kst().strftime("%Y%m%d_%H%M%S_%f")
    dst = src.with_name(f"{src.name}.backup_{stamp}")
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()
    return str(dst)


async def backfill(db_path: str, *, dry_run: bool) -> int:
    missing = _query(db_path, _MISSING_EVENT_SQL)
    orphans = _query(db_path, _ORPHAN_EVENT_SQL)

    if not missing and not orphans:
        print("[backfill] 누락 그룹·고아 이벤트 없음 — 할 일 없음.")
        return 0

    if missing:
        print(f"[backfill] 이벤트 누락 그룹 {len(missing)}개:")
        for pick_id, sector_name, pick_date, _created, n_stocks in missing:
            print(f"  - pick_id={pick_id} sector={sector_name!r} "
                  f"pick_date={pick_date} stocks={n_stocks}")
    if orphans:
        print(f"[backfill] 추적행 없는 기존 이벤트 {len(orphans)}개(복구 대상):")
        for event_id, sector_name, pick_date in orphans:
            print(f"  - event_id={event_id} sector={sector_name!r} pick_date={pick_date}")

    if dry_run:
        print("[backfill] --dry-run: 변경 없음.")
        return 0

    backup_path = _backup(db_path)
    print(f"[backfill] DB 백업: {backup_path}")

    store = SectorStore(db_path)
    await store.open()
    try:
        await store._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # 1단계: 이벤트 누락 그룹에 이벤트 생성 (정식 경로와 동일하게 트랜잭션으로 감쌈).
        for pick_id, sector_name, pick_date, created_at, _n in missing:
            await store._db.execute("BEGIN IMMEDIATE")
            try:
                event_id = await store._record_sector_pick_event(
                    sector_name, created_at, date.fromisoformat(pick_date), pick_id
                )
                await store._db.execute("COMMIT")
            except Exception:
                await store._db.execute("ROLLBACK")
                raise
            print(f"[backfill] pick_id={pick_id} sector={sector_name!r} → event_id={event_id}")

        # 2단계: 활성 이벤트 전체에 멱등 추적행 생성 (신규 + 고아 자동 치유).
        tracker = DailyTracker(db_path, None)  # ensure_tracking_rows 는 kis 미사용
        created = await ensure_all_tracking_rows(tracker)
    finally:
        await store.close()

    # 검증: ensure_all_tracking_rows 는 per-event 예외를 삼키므로(락 경합 등) 성공처럼
    # 보일 수 있다. 추적행이 여전히 비어 있는 활성 이벤트가 남았는지 다시 확인해
    # 침묵 실패를 드러낸다.
    remaining = _query(db_path, _ORPHAN_EVENT_SQL)
    if remaining:
        print(f"[backfill] 경고: 추적행이 비어 있는 활성 이벤트 {len(remaining)}개 남음 — 재실행 필요:")
        for event_id, sector_name, _pd in remaining:
            print(f"  - event_id={event_id} sector={sector_name!r}")
        print(f"[backfill] 부분 완료: 이벤트 {len(missing)}개 생성, 추적행 {created}개 신규. "
              "미완료 이벤트 존재 — 재실행으로 자가치유 가능.")
        raise SystemExit(1)

    print(f"[backfill] 완료: 이벤트 {len(missing)}개 생성, 추적행 {created}개 신규 생성.")
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(settings.DB_PATH), help="DB 경로")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 대상만 출력")
    args = parser.parse_args()
    asyncio.run(backfill(args.db, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
