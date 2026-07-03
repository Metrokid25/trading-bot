"""스승님 6월 글(mentor.db) 기반 종목을 sector_stocks 에 등록.

2026-07-03 노트북에서 1회 실행됨(유니버스 23→50종목 9섹터). db/trading.db 는
gitignore 라 다른 기기에서는 이 스크립트를 다시 실행해야 같은 유니버스가 된다.
등록은 웹앱 POST /api/picks 와 동일 경로(StockMaster.resolve → upsert_sector),
이미 있는 종목은 upsert 가 스킵하므로 재실행해도 안전(멱등).

사용: ./.venv/Scripts/python.exe scripts/register_mentor_june_picks.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.time_utils import now_kst  # noqa: E402
from data.sector_models import SectorPick, SectorStock  # noqa: E402
from data.sector_store import SectorStore  # noqa: E402
from data.stock_master import StockMaster  # noqa: E402

# 섹터 → 종목명 (6월 mentor.db 추출, 오타 보정: 이스페타시스→이수페타시스)
PLAN = {
    "기판": ["LG이노텍", "이수페타시스", "티엘비", "대덕", "코리아써키트"],
    "광통신": ["대한광통신", "광전자", "티엠씨", "빛과전자", "파이버프로"],
    "반도체": ["SK스퀘어", "삼성전자", "한미반도체", "피에스케이홀딩스",
               "브이엠", "이오테크닉스", "원익IPS", "하나마이크론"],
    "조선엔진": ["한화엔진", "STX엔진", "HD현대마린엔진"],
    "원자력": ["두산에너빌리티", "비에이치아이", "우리기술"],
    "AI솔루션": ["마음AI", "플리토", "오브젠"],
}


async def main():
    master = StockMaster()
    store = SectorStore()
    await store.open()
    pick_date = now_kst().strftime("%Y-%m-%d")
    try:
        for sector, stock_names in PLAN.items():
            stocks = []
            for i, nm in enumerate(stock_names, 1):
                r = await master.resolve(nm)
                if r is None:
                    print(f"  !! 식별 실패: {nm} — 건너뜀")
                    continue
                code, name = r
                stocks.append(SectorStock(
                    pick_id=0, sector_name=sector, stock_code=code,
                    stock_name=name or nm, added_order=i,
                ))
            if not stocks:
                continue
            tpl = SectorPick.create(pick_date, raw_input="[mentor-june-mining]", expires_days=7)
            res = await store.upsert_sector(sector, stocks, tpl, record_pick_event=True)
            print(f"[{sector}] pick_id={res.pick_id} new={res.is_new_pick} "
                  f"추가 {res.added_count} / 총 {res.total_count} "
                  f"스킵 {[s.stock_name for s in res.skipped_stocks]}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
