from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

import webapp.server as webapp_server
from config.settings import settings
from data.sector_store import SectorStore
from webapp.server import app, get_http, get_kis, get_master, get_store

# 등록·삭제 라우트 공유 비밀번호 (테스트용)
WEB_KEY = "test-shared-key"
KEY_HDR = {"X-Web-Key": WEB_KEY}


@pytest.fixture(autouse=True)
def _web_key(monkeypatch):
    """모든 테스트에서 공유 비밀번호를 설정 상태로 둔다 (미설정=전부 거부라서)."""
    monkeypatch.setattr(settings, "WEB_SHARED_KEY", WEB_KEY)


def _markets_handler(request: httpx.Request) -> httpx.Response:
    """Yahoo 외부 호출을 가짜 응답으로 대체."""
    return httpx.Response(200, json={
        "chart": {"result": [{"meta": {
            "regularMarketPrice": 25266.94,
            "chartPreviousClose": 25358.60,
        }}]}
    })


class FakeKis:
    """네트워크 없는 결정적 시세 (KIS 실호출 회피).

    NXT 폴백 경로 검증용 시나리오:
    - 005930: 통합(UN) 시세/분봉 정상 — UN 데이터가 그대로 나가야 한다.
    - 000660: UN 시세는 예외, UN 분봉은 빈 목록 — KRX(J)로 폴백해야 한다.
    """

    _Q = {
        "005930": (75000, 1.5, 12_000_000, 900_000_000_000),
        "000660": (130000, -2.3, 3_000_000, 390_000_000_000),
    }

    _IDX = {"0001": (8471.02, 267.18, 3.26), "1001": (909.31, 17.79, 2.0)}

    async def get_quote(self, code: str, market_code: str = "J"):
        if market_code == "UN" and code == "000660":
            raise RuntimeError("UN not supported")
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
        return {
            "code": code, "value": value, "change": change, "change_rate": rate,
            "up_count": 831, "upper_count": 4, "flat_count": 5,
            "down_count": 77, "lower_count": 0,
        }

    async def get_index_minute_chart(self, code: str, interval_sec: int = 300):
        if code not in self._IDX:
            raise RuntimeError("no data")
        # KIS는 최신→과거 순 + 이월 봉(전 거래일)이 섞일 수 있다.
        return {
            "summary": {
                "bstp_nmix_prpr": "7560.10",
                "bstp_nmix_prdy_vrss": "268.19",
                "bstp_nmix_prdy_ctrt": "3.68",
                "prdy_nmix": "7291.91",
            },
            "bars": [
                {"stck_bsop_date": "20260710", "stck_cntg_hour": "093500",
                 "bstp_nmix_prpr": "7560.10"},
                {"stck_bsop_date": "20260710", "stck_cntg_hour": "093000",
                 "bstp_nmix_prpr": "7552.49"},
                {"stck_bsop_date": "20260709", "stck_cntg_hour": "152500",
                 "bstp_nmix_prpr": "7291.91"},
            ],
        }

    async def get_market_investor_flow(self, market: str):
        # 단위 백만원 — 서버가 억원으로 환산해야 한다
        if market == "KOSPI":
            return {"individual": -1_283_514, "foreign": -348_321, "institution": 1_700_413}
        if market == "KOSDAQ":
            return {"individual": -308_470, "foreign": -113_949, "institution": 422_546}
        raise RuntimeError("unknown market")

    async def get_daily_candles(self, code: str, start: str, end: str, period: str = "D"):
        # KIS는 최신→과거 순으로 준다. 서버가 오름차순 정렬하는지 검증하려고 역순 제공.
        return [
            {"stck_bsop_date": "20260102", "stck_oprc": "100", "stck_hgpr": "110",
             "stck_lwpr": "95", "stck_clpr": "105", "acml_vol": "1000"},
            {"stck_bsop_date": "20260101", "stck_oprc": "90", "stck_hgpr": "100",
             "stck_lwpr": "88", "stck_clpr": "98", "acml_vol": "800"},
        ]

    async def get_minute_candles(self, code: str, market_code: str = "J"):
        if market_code == "UN":
            if code == "000660":
                return []  # UN 미지원 → 서버가 J로 폴백해야 함
            return [
                {"stck_cntg_hour": "100100", "stck_oprc": "100", "stck_hgpr": "101",
                 "stck_lwpr": "99", "stck_prpr": "100", "cntg_vol": "50"},
                {"stck_cntg_hour": "100000", "stck_oprc": "99", "stck_hgpr": "100",
                 "stck_lwpr": "98", "stck_prpr": "99", "cntg_vol": "40"},
                # NXT 프리장 봉 — UN에서만 온다
                {"stck_cntg_hour": "084500", "stck_oprc": "98", "stck_hgpr": "99",
                 "stck_lwpr": "97", "stck_prpr": "98", "cntg_vol": "10"},
            ]
        return [
            {"stck_cntg_hour": "100100", "stck_oprc": "100", "stck_hgpr": "101",
             "stck_lwpr": "99", "stck_prpr": "100", "cntg_vol": "50"},
            {"stck_cntg_hour": "100000", "stck_oprc": "99", "stck_hgpr": "100",
             "stck_lwpr": "98", "stck_prpr": "99", "cntg_vol": "40"},
        ]


class FakeMaster:
    """네트워크 없는 결정적 종목 마스터 (KRX 다운로드 회피)."""

    _M = {
        "005930": "삼성전자",
        "000660": "SK하이닉스",
        "042700": "한미반도체",
        "069500": "KODEX 200",
        "0167A0": "SOL AI반도체TOP2플러스",
    }

    def __init__(self):
        self.ensure_loaded_calls = 0
        self.loaded = False

    async def ensure_loaded(self):
        self.ensure_loaded_calls += 1
        self.loaded = True

    def instrument_type(self, code: str):
        return "etf" if self.loaded and code in {"069500", "0167A0"} else "stock"

    async def search(self, query: str, limit: int = 8):
        await self.ensure_loaded()
        q = query.strip()
        if not q:
            return []
        out = [(c, n) for c, n in self._M.items() if q == c or q in n]
        return out[:limit]

    async def resolve(self, query: str):
        await self.ensure_loaded()
        q = query.strip()
        if q in self._M:
            return q, self._M[q]
        for code, name in self._M.items():
            if q == name:
                return code, name
        return None


@pytest_asyncio.fixture
async def client(tmp_path):
    webapp_server._clear_runtime_caches()  # 모듈 전역 캐시가 테스트 간 새지 않게
    store = SectorStore(str(tmp_path / "web.db"))
    await store.open()
    master = FakeMaster()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_master] = lambda: master
    app.dependency_overrides[get_kis] = lambda: FakeKis()
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(_markets_handler))
    app.dependency_overrides[get_http] = lambda: mock_http

    transport = ASGITransport(app=app)
    # 기본 헤더에 공유 키 포함 — 인증 실패 케이스는 요청 단위 headers로 덮어쓴다
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=KEY_HDR
    ) as c:
        c.store = store  # author 스탬프 검증용 직접 조회 핸들
        yield c

    app.dependency_overrides.clear()
    await store.close()
    await mock_http.aclose()


@pytest.mark.asyncio
async def test_search_returns_candidates(client):
    res = await client.get("/api/search", params={"q": "삼성"})
    assert res.status_code == 200
    data = res.json()
    assert {"code": "005930", "name": "삼성전자", "type": "stock"} in data


@pytest.mark.asyncio
async def test_index_exposes_keyboard_accessible_search(client):
    res = await client.get("/")
    assert res.status_code == 200
    html = res.text
    assert 'role="combobox"' in html
    assert 'role="listbox"' in html
    assert 'role="option"' in html
    assert 'e.key === "ArrowDown"' in html
    assert 'e.key === "Enter"' in html


@pytest.mark.asyncio
async def test_search_and_register_etf(client):
    search = await client.get("/api/search", params={"q": "KODEX"})
    assert search.status_code == 200
    assert search.json() == [
        {"code": "069500", "name": "KODEX 200", "type": "etf"}
    ]

    registered = await client.post(
        "/api/picks",
        json={"sector_name": "지수ETF", "stocks": [{"code": "069500"}]},
    )
    assert registered.status_code == 200
    app.dependency_overrides[get_master]().loaded = False  # 재시작 직후 기존 ETF 픽 상황
    picks = (await client.get("/api/picks")).json()
    etf = next(s for p in picks for s in p["stocks"] if s["code"] == "069500")
    assert etf["name"] == "KODEX 200"
    assert etf["type"] == "etf"


@pytest.mark.asyncio
async def test_search_and_register_alphanumeric_etf(client):
    search = await client.get("/api/search", params={"q": "0167A0"})
    assert search.status_code == 200
    assert search.json() == [
        {"code": "0167A0", "name": "SOL AI반도체TOP2플러스", "type": "etf"}
    ]

    registered = await client.post(
        "/api/picks",
        json={"sector_name": "AI반도체ETF", "stocks": [{"code": "0167A0"}]},
    )
    assert registered.status_code == 200
    picks = (await client.get("/api/picks")).json()
    etf = next(s for p in picks for s in p["stocks"] if s["code"] == "0167A0")
    assert etf["name"] == "SOL AI반도체TOP2플러스"
    assert etf["type"] == "etf"


@pytest.mark.asyncio
async def test_list_picks_ensures_master_loaded(client):
    master = app.dependency_overrides[get_master]()
    res = await client.get("/api/picks")
    assert res.status_code == 200
    assert master.ensure_loaded_calls == 1


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
    # 시장 폭 (상승/상한/보합/하락/하한)
    assert kospi["up"] == 831
    assert kospi["upper"] == 4
    assert kospi["down"] == 77


@pytest.mark.asyncio
async def test_markets_returns_dashboard_groups(client):
    res = await client.get("/api/markets")
    assert res.status_code == 200
    data = res.json()
    assert [d["name"] for d in data] == [
        "나스닥F", "다우F", "S&PF", "나스닥", "다우", "S&P500", "환율", "WTI", "한국ETF",
    ]
    groups = {d["name"]: d["group"] for d in data}
    assert groups["나스닥F"] == "us_futures"
    assert groups["나스닥"] == "us"
    assert groups["WTI"] == "fx"
    nasdaq_f = data[0]
    assert nasdaq_f["value"] == 25266.94
    assert nasdaq_f["change_rate"] < 0  # 25266.94 < 25358.60


@pytest.mark.asyncio
async def test_index_chart_filters_today_and_sorts_ascending(client):
    """이월 봉(전 거래일) 제외 + 과거→최신 정렬 + 요약값."""
    res = await client.get("/api/index-chart", params={"code": "0001"})
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "코스피"
    assert data["value"] == 7560.10
    assert data["prev_close"] == 7291.91
    assert data["date"] == "20260710"
    # 20260709 봉은 제외, 당일 봉만 오름차순
    assert data["bars"] == [
        {"t": "093000", "c": 7552.49},
        {"t": "093500", "c": 7560.10},
    ]


@pytest.mark.asyncio
async def test_index_chart_unknown_code_returns_400(client):
    """지원 지수(0001/1001) 외 코드는 400 — 임의 코드로 KIS 호출·캐시 증식 방지."""
    res = await client.get("/api/index-chart", params={"code": "9999"})
    assert res.status_code == 400


class _BrokenKis(FakeKis):
    async def get_index_minute_chart(self, code: str, interval_sec: int = 300):
        raise RuntimeError("KIS down")


@pytest.mark.asyncio
async def test_index_chart_kis_failure_returns_empty_not_500(client):
    app.dependency_overrides[get_kis] = lambda: _BrokenKis()
    res = await client.get("/api/index-chart", params={"code": "0001"})
    assert res.status_code == 200
    data = res.json()
    assert data["bars"] == []
    assert data["value"] is None


@pytest.mark.asyncio
async def test_flows_converted_to_eok_won(client):
    """KIS 백만원 단위를 억원으로 환산해 내려준다."""
    res = await client.get("/api/flows")
    assert res.status_code == 200
    data = res.json()
    assert data["kospi"] == {
        "individual": -12835, "foreign": -3483, "institution": 17004,
    }
    assert data["kosdaq"]["institution"] == 4225


class _CountingKis(FakeKis):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def get_quote(self, code: str, market_code: str = "J"):
        self.calls.append((code, market_code))
        return await super().get_quote(code, market_code)


@pytest.mark.asyncio
async def test_quote_market_memo_avoids_repeated_un_probing(client):
    """UN 실패 종목은 시장코드를 기억해 다음 폴링부터 J 직행(2배 호출 방지)."""
    kis = _CountingKis()
    app.dependency_overrides[get_kis] = lambda: kis
    await client.get("/api/quotes", params={"codes": "000660"})  # UN 예외 → J 폴백 + 메모
    webapp_server._quote_cache.clear()  # 시세 캐시만 비우고 메모는 유지
    await client.get("/api/quotes", params={"codes": "000660"})
    assert kis.calls == [("000660", "UN"), ("000660", "J"), ("000660", "J")]


@pytest.mark.asyncio
async def test_quotes_returns_price_and_change(client):
    res = await client.get("/api/quotes", params={"codes": "005930,000660,999999"})
    assert res.status_code == 200
    data = res.json()
    assert data["005930"] == {
        "price": 75000, "change_rate": 1.5,
        "volume": 12_000_000, "value": 900_000_000_000,
    }
    # 000660은 UN 시세가 예외 → KRX(J) 폴백으로 정상 응답해야 한다
    assert data["000660"]["change_rate"] == -2.3
    assert data["999999"] is None  # 조회 실패 종목은 null


@pytest.mark.asyncio
async def test_candles_daily_sorted_ascending(client):
    res = await client.get("/api/candles", params={"code": "005930", "tf": "daily"})
    assert res.status_code == 200
    data = res.json()
    assert data["tf"] == "daily"
    assert [c["t"] for c in data["candles"]] == ["20260101", "20260102"]
    assert data["candles"][0] == {
        "t": "20260101", "o": 90, "h": 100, "l": 88, "c": 98, "v": 800,
    }


@pytest.mark.asyncio
async def test_candles_minute_ascending_with_nxt(client):
    """통합(UN) 분봉 사용 — NXT 프리장 봉(084500) 포함 + 과거→최신 정렬."""
    res = await client.get("/api/candles", params={"code": "005930", "tf": "minute"})
    assert res.status_code == 200
    data = res.json()
    # KIS 최신→과거를 서버가 뒤집어 과거→최신으로
    assert [c["t"] for c in data["candles"]] == ["084500", "100000", "100100"]


@pytest.mark.asyncio
async def test_candles_minute_falls_back_to_krx_when_un_empty(client):
    """UN 분봉이 비어 있으면 KRX(J) 분봉으로 폴백한다."""
    res = await client.get("/api/candles", params={"code": "000660", "tf": "minute"})
    assert res.status_code == 200
    data = res.json()
    assert [c["t"] for c in data["candles"]] == ["100000", "100100"]


@pytest.mark.asyncio
async def test_candles_missing_code_returns_400(client):
    res = await client.get("/api/candles", params={"code": ""})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_candles_batch_returns_per_code(client):
    res = await client.get("/api/candles-batch", params={"codes": "005930,000660", "tf": "daily"})
    assert res.status_code == 200
    data = res.json()
    assert set(data["candles"].keys()) == {"005930", "000660"}
    assert [c["t"] for c in data["candles"]["005930"]] == ["20260101", "20260102"]


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


# ----- 공유 비밀번호 (등록·삭제 라우트 보호) -----

_REG = {"sector_name": "반도체", "stocks": [{"code": "005930"}]}


@pytest.mark.asyncio
async def test_register_without_key_returns_401(client):
    res = await client.post("/api/picks", json=_REG, headers={"X-Web-Key": ""})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_register_with_wrong_key_returns_401(client):
    res = await client.post("/api/picks", json=_REG, headers={"X-Web-Key": "wrong-key"})
    assert res.status_code == 401
    assert "올바르지 않습니다" in res.json()["detail"]


@pytest.mark.asyncio
async def test_mutations_rejected_when_key_unset(client, monkeypatch):
    """서버에 WEB_SHARED_KEY 미설정이면 올바른 키를 보내도 전부 거부(안전 기본값)."""
    monkeypatch.setattr(settings, "WEB_SHARED_KEY", "")
    res = await client.post("/api/picks", json=_REG)
    assert res.status_code == 401
    assert "설정되지 않았습니다" in res.json()["detail"]


@pytest.mark.asyncio
async def test_remove_routes_require_key(client):
    r1 = await client.post(
        "/api/picks/remove-stock",
        json={"sector_name": "반도체", "stock_code": "005930"},
        headers={"X-Web-Key": ""},
    )
    r2 = await client.post(
        "/api/picks/remove-sector",
        json={"sector_name": "반도체"},
        headers={"X-Web-Key": ""},
    )
    assert r1.status_code == 401
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_read_routes_open_without_key(client):
    """조회(GET)는 키 없이 허용 — 보호 대상은 등록·삭제뿐."""
    assert (await client.get("/api/picks", headers={"X-Web-Key": ""})).status_code == 200
    res = await client.get("/api/search", params={"q": "삼성"}, headers={"X-Web-Key": ""})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_register_with_header_fully_absent_returns_401(client):
    """헤더 자체가 없는 요청(빈 문자열 아님)도 401 — 실제 브라우저 외 클라이언트 케이스."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as bare:
        res = await bare.post("/api/picks", json=_REG)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_server_key_rejected_with_clear_message(client, monkeypatch):
    """한글 키는 HTTP 헤더로 전송 불가 → 서버가 명확한 메시지로 거부해야 한다."""
    monkeypatch.setattr(settings, "WEB_SHARED_KEY", "한글비밀번호")
    res = await client.post("/api/picks", json=_REG)
    assert res.status_code == 401
    assert "영문" in res.json()["detail"]


@pytest.mark.asyncio
async def test_server_key_surrounding_whitespace_tolerated(client, monkeypatch):
    """.env에 공백 낀 키가 저장돼도(따옴표 값 등) 서버가 strip 후 비교한다."""
    monkeypatch.setattr(settings, "WEB_SHARED_KEY", f"  {WEB_KEY}  ")
    res = await client.post("/api/picks", json=_REG)
    assert res.status_code == 200


# ----- 웹 등록 픽 유효기간 (유니버스 상시 유지) -----

@pytest.mark.asyncio
async def test_register_uses_long_expiry(client):
    """웹 등록 픽은 1년 유효 — 7일 만료로 유니버스가 증발하지 않게."""
    from core.time_utils import now_kst

    res = await client.post("/api/picks", json=_REG)
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    # DB 왕복 후 naive datetime — 날짜 기준으로 비교 (tz 무관)
    assert (picks[0].expires_at.date() - now_kst().date()).days >= 360


@pytest.mark.asyncio
async def test_register_to_existing_sector_extends_expiry(client):
    """기존 활성 섹터에 종목 추가 시 낡은 만료시각도 1년 이상으로 갱신."""
    from datetime import timedelta

    from core.time_utils import now_kst, to_db_iso

    res = await client.post("/api/picks", json=_REG)
    pick_id = res.json()["pick_id"]
    # 만료 임박 상태를 인위로 만든 뒤 같은 섹터에 종목 추가
    soon = to_db_iso(now_kst() + timedelta(days=1))
    await client.store._db.execute(
        "UPDATE sector_picks SET expires_at=? WHERE id=?", (soon, pick_id))
    res = await client.post(
        "/api/picks", json={"sector_name": "반도체", "stocks": [{"code": "000660"}]})
    assert res.status_code == 200
    assert res.json()["pick_id"] == pick_id  # 같은 활성 픽에 추가됐는지
    picks = await client.store.get_active_picks()
    assert (picks[0].expires_at.date() - now_kst().date()).days >= 360


# ----- 등록자(author) 스탬프 -----

@pytest.mark.asyncio
async def test_register_stamps_default_author(client):
    res = await client.post("/api/picks", json=_REG)
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    assert picks[0].raw_input == "[web:황파파]"


@pytest.mark.asyncio
async def test_register_stamps_custom_author(client):
    res = await client.post("/api/picks", json={**_REG, "author": "테스터"})
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    assert picks[0].raw_input == "[web:테스터]"


@pytest.mark.asyncio
async def test_register_author_sanitized(client):
    """대괄호·공백은 스탬프 형식([web:이름]) 보호를 위해 정리, 빈 이름은 기본값."""
    res = await client.post("/api/picks", json={**_REG, "author": " [테스터] "})
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    assert picks[0].raw_input == "[web:테스터]"

    res = await client.post(
        "/api/picks",
        json={"sector_name": "2차전지", "stocks": [{"code": "042700"}], "author": "  "},
    )
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    by_input = {p.raw_input for p in picks}
    assert "[web:황파파]" in by_input


@pytest.mark.asyncio
async def test_register_author_control_chars_stripped(client):
    """개행 등 비인쇄 문자는 제거 — raw_input 한 줄 스탬프 형식 보호."""
    res = await client.post("/api/picks", json={**_REG, "author": "테\n스터"})
    assert res.status_code == 200
    picks = await client.store.get_active_picks()
    assert picks[0].raw_input == "[web:테스터]"


@pytest.mark.asyncio
async def test_list_picks_exposes_registered_by(client):
    """등록자 표시 — GET /api/picks가 registered_by를 내려준다."""
    await client.post("/api/picks", json={**_REG, "author": "테스터"})
    res = await client.get("/api/picks")
    assert res.status_code == 200
    assert res.json()[0]["registered_by"] == "테스터"
