"""로컬 종목 등록 웹 대시보드 (FastAPI).

main.py(섹터 알림 봇)·main_tracker.py(데이터 수집)와 격리된 별도 프로세스.
KIS 시세/매매·텔레그램 발송은 건드리지 않고, 종목 검색(StockMaster)과
등록(SectorStore.upsert_sector)만 한다. 기본 localhost 전용으로 띄운다.

실행:
    .venv/Scripts/python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.time_utils import now_kst
from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore
from data.stock_master import StockMaster

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = SectorStore()
    await store.open()
    master = StockMaster()
    app.state.store = store
    app.state.master = master
    try:
        yield
    finally:
        await store.close()


app = FastAPI(title="trading-bot 종목 등록", lifespan=lifespan)


# ----- 의존성 (테스트에서 override 가능) -----
def get_store(request: Request) -> SectorStore:
    return request.app.state.store


def get_master(request: Request) -> StockMaster:
    return request.app.state.master


# ----- 요청/응답 모델 -----
class StockIn(BaseModel):
    code: str
    name: str | None = None


class RegisterIn(BaseModel):
    sector_name: str = Field(min_length=1)
    pick_date: str | None = None
    stocks: list[StockIn] = Field(min_length=1)


# ----- API -----
@app.get("/api/search")
async def search(
    q: str = "",
    limit: int = 8,
    master: StockMaster = Depends(get_master),
) -> list[dict]:
    """종목 자동완성 후보. q는 한글/영문/6자리 코드."""
    results = await master.search(q, limit=limit)
    return [{"code": code, "name": name} for code, name in results]


@app.get("/api/picks")
async def list_picks(store: SectorStore = Depends(get_store)) -> list[dict]:
    """활성 픽 + 종목 현황."""
    picks = await store.get_active_picks()
    out: list[dict] = []
    for pick in picks:
        stocks = await store.get_stocks_by_pick(pick.id) if pick.id else []
        out.append(
            {
                "pick_id": pick.id,
                "pick_date": pick.pick_date,
                "expires_at": pick.expires_at.isoformat(),
                "stocks": [
                    {
                        "code": s.stock_code,
                        "name": s.stock_name,
                        "sector": s.sector_name,
                    }
                    for s in stocks
                ],
            }
        )
    return out


@app.post("/api/picks")
async def register(
    body: RegisterIn,
    store: SectorStore = Depends(get_store),
    master: StockMaster = Depends(get_master),
) -> dict:
    """섹터 + 종목 등록. 텔레그램 /p 와 동일 경로(resolve → upsert_sector)."""
    pick_date = body.pick_date or now_kst().strftime("%Y-%m-%d")

    sector_stocks: list[SectorStock] = []
    seen: set[str] = set()
    order = 0
    for item in body.stocks:
        resolved = await master.resolve(item.code)
        if resolved is None:
            raise HTTPException(status_code=400, detail=f"종목 식별 실패: {item.code}")
        code, name = resolved
        if code in seen:
            continue
        seen.add(code)
        order += 1
        sector_stocks.append(
            SectorStock(
                pick_id=0,  # upsert_sector가 실제 pick_id 기록
                sector_name=body.sector_name,
                stock_code=code,
                stock_name=name or item.name or code,
                added_order=order,
            )
        )

    if not sector_stocks:
        raise HTTPException(status_code=400, detail="등록할 종목이 없습니다")

    pick_template = SectorPick.create(pick_date, raw_input="[web]", expires_days=7)
    result = await store.upsert_sector(
        body.sector_name, sector_stocks, pick_template, record_pick_event=True
    )
    return {
        "pick_id": result.pick_id,
        "is_new_pick": result.is_new_pick,
        "added": result.added_count,
        "total": result.total_count,
        "skipped": [s.stock_name for s in result.skipped_stocks],
    }


# ----- 정적 프론트 -----
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
