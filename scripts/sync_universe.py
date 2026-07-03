"""유니버스(active sector_stocks) 스냅샷 export/import — PC ↔ 노트북 동기화.

db/trading.db 는 gitignore 라 기기 간 유니버스가 어긋난다(2026-07-03 실측:
노트북 50종목 9섹터 vs PC 21종목 1섹터). 이 스크립트로 스냅샷을 git 에 실어
"pull만 하면 최신" 원칙(CLAUDE.md)을 유니버스에도 적용한다.

사용:
  노트북(정식 DB)에서:  ./.venv/Scripts/python.exe scripts/sync_universe.py --export
                        → universe_snapshot.json 생성 → commit + push
  PC 에서:              git pull 후
                        ./.venv/Scripts/python.exe scripts/sync_universe.py --import
                        → 웹앱 등록과 동일 경로(upsert_sector)로 멱등 반영

주의: import 는 스냅샷에 있는 종목을 "추가"만 한다(스킵/보관 처리는 안 함).
      로컬에만 있는 여분 픽 정리는 별도 판단(웹앱 또는 cleanup 스크립트).
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.time_utils import now_kst, to_db_iso  # noqa: E402
from data.sector_models import SectorPick, SectorStock  # noqa: E402
from data.sector_store import SectorStore  # noqa: E402

SNAPSHOT = Path(__file__).resolve().parent.parent / "universe_snapshot.json"


def export_snapshot() -> None:
    con = sqlite3.connect(settings.DB_PATH)
    rows = con.execute(
        "SELECT ss.sector_name, ss.stock_code, ss.stock_name, MIN(sp.pick_date) "
        "FROM sector_stocks ss JOIN sector_picks sp ON sp.id = ss.pick_id "
        "WHERE ss.tracking_status='active' AND sp.status='active' "
        "GROUP BY ss.sector_name, ss.stock_code "
        "ORDER BY ss.sector_name, MIN(ss.added_order)"
    ).fetchall()
    con.close()

    sectors: dict[str, dict] = {}
    for sector, code, name, pick_date in rows:
        s = sectors.setdefault(sector, {"sector_name": sector,
                                        "pick_date": pick_date, "stocks": []})
        s["pick_date"] = min(s["pick_date"], pick_date)
        s["stocks"].append({"code": code, "name": name})

    snapshot = {"exported_at": to_db_iso(now_kst()),
                "sectors": list(sectors.values())}
    SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    total = sum(len(s["stocks"]) for s in sectors.values())
    print(f"[export] {len(sectors)}섹터 {total}종목 → {SNAPSHOT.name} "
          f"(commit + push 해야 다른 기기로 전달됨)")


async def import_snapshot() -> None:
    if not SNAPSHOT.exists():
        raise SystemExit(f"{SNAPSHOT.name} 없음 — 소스 기기에서 --export 후 push, "
                         f"이 기기에서 git pull 먼저.")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    print(f"[import] 스냅샷 exported_at={snapshot.get('exported_at')}")

    # 같은 섹터의 "다른" 활성 픽에 이미 있는 종목도 스킵 — upsert_sector 는
    # 대상 픽 내부만 dedup 하므로, 여기서 안 거르면 픽 간 중복 행이 생겨
    # 트래커가 같은 종목을 이중 수집한다.
    con = sqlite3.connect(settings.DB_PATH)
    existing = set(con.execute(
        "SELECT sector_name, stock_code FROM sector_stocks "
        "WHERE tracking_status='active'"))
    con.close()

    store = SectorStore()
    await store.open()
    try:
        for sec in snapshot["sectors"]:
            stocks = [SectorStock(pick_id=0, sector_name=sec["sector_name"],
                                  stock_code=st["code"], stock_name=st["name"],
                                  added_order=i)
                      for i, st in enumerate(sec["stocks"], 1)
                      if (sec["sector_name"], st["code"]) not in existing]
            if not stocks:
                print(f"[{sec['sector_name']}] 전부 이미 활성 — 스킵")
                continue
            tpl = SectorPick.create(sec["pick_date"], raw_input="[universe-sync]",
                                    expires_days=7)
            res = await store.upsert_sector(sec["sector_name"], stocks, tpl,
                                            record_pick_event=True)
            print(f"[{sec['sector_name']}] pick_id={res.pick_id} "
                  f"new={res.is_new_pick} 추가 {res.added_count} / "
                  f"총 {res.total_count} 스킵 {len(res.skipped_stocks)}")
    finally:
        await store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", action="store_true", dest="do_export",
                   help="이 기기의 active 유니버스를 universe_snapshot.json 으로 내보내기")
    g.add_argument("--import", action="store_true", dest="do_import",
                   help="universe_snapshot.json 을 이 기기 DB에 멱등 반영")
    args = ap.parse_args()
    if args.do_export:
        export_snapshot()
    else:
        asyncio.run(import_snapshot())


if __name__ == "__main__":
    main()
