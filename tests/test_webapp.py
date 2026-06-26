from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from data.sector_store import SectorStore
from webapp.server import app, get_kis, get_master, get_store


class FakeKis:
    """네트워크 없는 결정적 시세 (KIS 실호출 회피)."""

    _Q = {
        "005930": (75000, 1.5, 12_000_000, 900_000_000_000),
        "000660": (130000, -2.3, 3_000_000, 390_000_000_000),
    }

    _IDX = {"0001": (8471.02, 267.18, 3.26), "1001": (909.31, 17.79, 2.0)}

    async def get_quote(self, code: str):
        if code not in self._Q:
            raise RuntimeError("no data")
        price, rate, vol, val = self._Q[code]
        return {
            "code": code, "price": price, "change_rate": rate,
            "volume": vol, "value": val,
        }

    async def get_index(self, code: str):
        if code not in self._IDX:
            raise RuntimeError("no data")
        value, change, rate = self._IDX[code]
        return {"code": code, "value": value, "change": change, "change_rate": rate}


class FakeMaster:
    """네트워크 없는 결정적 종목 마스터 (KRX 다운로드 회피)."""

    _M = {"005930": "삼성전자", "000660": "SK하이닉스", "042700": "한미반도체"}

    async def search(self, query: str, limit: int = 8):
        q = query.strip()
        if not q:
            return []
        out = [(c, n) for c, n in self._M.items() if q == c or q in n]
        return out[:limit]

    async def resolve(self, query: str):
        q = query.strip()
        if q in self._M:
            return q, self._M[q]
        for code, name in self._M.items():
            if q == name:
                return code, name
        return None


@pytest_asyncio.fixture
async def client(tmp_path):
    store = SectorStore(str(tmp_path / "web.db"))
    await store.open()
    master = FakeMaster()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_master] = lambda: master
    app.dependency_overrides[get_kis] = lambda: FakeKis()

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    await store.close()


@pytest.mark.asyncio
async def test_search_returns_candidates(client):
    res = await client.get("/api/search", params={"q": "삼성"})
    assert res.status_code == 200
    data = res.json()
    assert {"code": "005930", "name": "삼성전자"} in data


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(client):
    res = await client.get("/api/search", params={"q": ""})
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_indices_returns_kospi_kosdaq(client):
    res = await client.get("/api/indices")
    assert res.status_code == 200
    data = res.json()
    names = [d["name"] for d in data]
    assert names == ["코스피", "코스닥"]
    kospi = data[0]
    assert kospi["value"] == 8471.02
    assert kospi["change_rate"] == 3.26


@pytest.mark.asyncio
async def test_quotes_returns_price_and_change(client):
    res = await client.get("/api/quotes", params={"codes": "005930,000660,999999"})
    assert res.status_code == 200
    data = res.json()
    assert data["005930"] == {
        "price": 75000, "change_rate": 1.5,
        "volume": 12_000_000, "value": 900_000_000_000,
    }
    assert data["000660"]["change_rate"] == -2.3
    assert data["999999"] is None  # 조회 실패 종목은 null


@pytest.mark.asyncio
async def test_register_and_list_roundtrip(client):
    res = await client.post(
        "/api/picks",
        json={
            "sector_name": "반도체",
            "stocks": [{"code": "005930"}, {"code": "000660"}],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["added"] == 2
    assert body["total"] == 2
    assert body["is_new_pick"] is True

    res = await client.get("/api/picks")
    assert res.status_code == 200
    picks = res.json()
    codes = {s["code"] for p in picks for s in p["stocks"]}
    sectors = {s["sector"] for p in picks for s in p["stocks"]}
    assert codes == {"005930", "000660"}
    assert sectors == {"반도체"}


@pytest.mark.asyncio
async def test_remove_stock(client):
    await client.post(
        "/api/picks",
        json={"sector_name": "반도체", "stocks": [{"code": "005930"}, {"code": "000660"}]},
    )
    res = await client.post(
        "/api/picks/remove-stock",
        json={"sector_name": "반도체", "stock_code": "005930"},
    )
    assert res.status_code == 200
    assert res.json()["removed_from_picks"]

    picks = (await client.get("/api/picks")).json()
    codes = {s["code"] for p in picks for s in p["stocks"]}
    assert codes == {"000660"}  # 005930 제거됨


@pytest.mark.asyncio
async def test_remove_stock_unknown_returns_404(client):
    res = await client.post(
        "/api/picks/remove-stock",
        json={"sector_name": "없는섹터", "stock_code": "005930"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_remove_sector(client):
    await client.post(
        "/api/picks",
        json={"sector_name": "반도체", "stocks": [{"code": "005930"}, {"code": "000660"}]},
    )
    await client.post(
        "/api/picks",
        json={"sector_name": "2차전지", "stocks": [{"code": "042700"}]},
    )
    res = await client.post(
        "/api/picks/remove-sector",
        json={"sector_name": "반도체"},
    )
    assert res.status_code == 200
    assert res.json()["affected_picks"]

    picks = (await client.get("/api/picks")).json()
    sectors = {s["sector"] for p in picks for s in p["stocks"]}
    assert sectors == {"2차전지"}  # 반도체 섹터 통째로 제거됨


@pytest.mark.asyncio
async def test_register_dedupes_same_code(client):
    res = await client.post(
        "/api/picks",
        json={
            "sector_name": "반도체",
            "stocks": [{"code": "005930"}, {"code": "005930"}],
        },
    )
    assert res.status_code == 200
    assert res.json()["added"] == 1


@pytest.mark.asyncio
async def test_register_unknown_code_returns_400(client):
    res = await client.post(
        "/api/picks",
        json={"sector_name": "반도체", "stocks": [{"code": "999999"}]},
    )
    assert res.status_code == 400
    assert "식별 실패" in res.json()["detail"]


@pytest.mark.asyncio
async def test_register_empty_stocks_rejected_by_validation(client):
    res = await client.post(
        "/api/picks",
        json={"sector_name": "반도체", "stocks": []},
    )
    # pydantic min_length=1 → 422
    assert res.status_code == 422
