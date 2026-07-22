from __future__ import annotations

import asyncio
import io
import json
import zipfile

import httpx
import pytest

from data.stock_master import StockMaster


def _master_row(code: str, name: str, group: str) -> str:
    prefix = f"{code:<9}{'KR7000000000':<12}{name:<35}"
    return prefix + " " + group + ("0" * 225)


def _master_zip(*rows: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("kospi_code.mst", "\n".join(rows).encode("cp949"))
    return buf.getvalue()


def test_parse_kis_master_includes_only_etf_group():
    payload = _master_zip(
        _master_row("005930", "삼성전자", "ST"),
        _master_row("069500", "KODEX 200", "EF"),
        _master_row("229200", "KODEX 코스닥150", "EF"),
        _master_row("0167A0", "SOL AI반도체TOP2플러스", "EF"),
    )

    assert StockMaster._parse_kis_etf_master(payload) == {
        "069500": "KODEX 200",
        "229200": "KODEX 코스닥150",
        "0167A0": "SOL AI반도체TOP2플러스",
    }


@pytest.mark.asyncio
async def test_refresh_merges_stocks_and_etfs(tmp_path, monkeypatch):
    stock_html = """
    <tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>
    """
    etf_zip = _master_zip(
        _master_row("069500", "KODEX 200", "EF"),
        _master_row("0167A0", "SOL AI반도체TOP2플러스", "EF"),
    )

    async def fake_get(_client, url, **_kwargs):
        if url.endswith("kospi_code.mst.zip"):
            return httpx.Response(200, content=etf_zip, request=httpx.Request("GET", url))
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(tmp_path / "master.json")

    assert await master.refresh() == 3
    assert await master.search("KODEX") == [("069500", "KODEX 200")]
    assert await master.search("0167a0") == [("0167A0", "SOL AI반도체TOP2플러스")]
    assert master.instrument_type("069500") == "etf"
    assert master.instrument_type("0167A0") == "etf"
    assert master.instrument_type("005930") == "stock"

    saved = json.loads((tmp_path / "master.json").read_text(encoding="utf-8"))
    assert saved["version"] == 3
    assert saved["types"]["069500"] == "etf"
    assert saved["types"]["0167A0"] == "etf"


@pytest.mark.asyncio
async def test_v2_cache_refreshes_to_include_alphanumeric_etf(tmp_path, monkeypatch):
    cache = tmp_path / "master.json"
    cache.write_text(
        json.dumps(
            {
                "version": 2,
                "by_code": {"005930": "삼성전자", "069500": "KODEX 200"},
                "types": {"005930": "stock", "069500": "etf"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"
    etf_zip = _master_zip(
        _master_row("069500", "KODEX 200", "EF"),
        _master_row("0167A0", "SOL AI반도체TOP2플러스", "EF"),
    )

    async def fake_get(_client, url, **_kwargs):
        if url.endswith("kospi_code.mst.zip"):
            return httpx.Response(200, content=etf_zip, request=httpx.Request("GET", url))
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(cache)

    assert master._loaded is False
    assert await master.search("0167A0") == [
        ("0167A0", "SOL AI반도체TOP2플러스")
    ]
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["version"] == 3
    assert saved["types"]["0167A0"] == "etf"


@pytest.mark.asyncio
async def test_v2_cache_is_not_promoted_when_etf_refresh_fails(tmp_path, monkeypatch):
    cache = tmp_path / "master.json"
    original = {
        "version": 2,
        "by_code": {"005930": "삼성전자", "069500": "KODEX 200"},
        "types": {"005930": "stock", "069500": "etf"},
    }
    cache.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"
    calls: list[str] = []

    async def fake_get(_client, url, **_kwargs):
        calls.append(url)
        if url.endswith("kospi_code.mst.zip"):
            raise httpx.ConnectError("ETF source offline")
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(cache)

    assert await master.search("0167A0") == []
    assert master._loaded is False
    assert json.loads(cache.read_text(encoding="utf-8")) == original

    assert await master.search("0167A0") == []
    assert len(calls) == 6  # 새 요청에서 KRX 2개 + KIS ETF를 다시 시도


@pytest.mark.asyncio
async def test_refresh_keeps_cached_etf_when_etf_download_fails(tmp_path, monkeypatch):
    cache = tmp_path / "master.json"
    cache.write_text(
        json.dumps(
            {
                "version": 3,
                "by_code": {"069500": "KODEX 200"},
                "types": {"069500": "etf"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"

    async def fake_get(_client, url, **_kwargs):
        if url.endswith("kospi_code.mst.zip"):
            raise httpx.ConnectError("offline")
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(cache)

    assert await master.refresh() == 2
    assert await master.resolve("069500") == ("069500", "KODEX 200")
    assert master.instrument_type("069500") == "etf"


@pytest.mark.asyncio
async def test_first_etf_failure_is_not_saved_as_complete_cache(tmp_path, monkeypatch):
    cache = tmp_path / "master.json"
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"

    async def fake_get(_client, url, **_kwargs):
        if url.endswith("kospi_code.mst.zip"):
            raise httpx.ConnectError("offline")
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(cache)

    assert await master.refresh() == 1
    assert not cache.exists()
    assert master._loaded is False
    assert await master.search("삼성") == [("005930", "삼성전자")]


@pytest.mark.asyncio
async def test_partial_krx_failure_does_not_replace_existing_master(tmp_path, monkeypatch):
    cache = tmp_path / "master.json"
    cache.write_text(
        json.dumps(
            {
                "version": 3,
                "by_code": {"005930": "삼성전자", "035720": "카카오", "069500": "KODEX 200"},
                "types": {"005930": "stock", "035720": "stock", "069500": "etf"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    etf_zip = _master_zip(_master_row("069500", "KODEX 200", "EF"))

    async def fake_get(_client, url, **_kwargs):
        if url.endswith("kospi_code.mst.zip"):
            return httpx.Response(200, content=etf_zip, request=httpx.Request("GET", url))
        if "kosdaqMkt" in url:
            raise httpx.ConnectError("kosdaq offline")
        html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"
        return httpx.Response(
            200, content=html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(cache)

    assert await master.refresh() == 0
    assert await master.resolve("035720") == ("035720", "카카오")
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert "035720" in saved["by_code"]


@pytest.mark.asyncio
async def test_concurrent_searches_share_one_initial_refresh(tmp_path, monkeypatch):
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"
    etf_zip = _master_zip(_master_row("069500", "KODEX 200", "EF"))
    calls: list[str] = []

    async def fake_get(_client, url, **_kwargs):
        calls.append(url)
        await asyncio.sleep(0.01)
        if url.endswith("kospi_code.mst.zip"):
            return httpx.Response(200, content=etf_zip, request=httpx.Request("GET", url))
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(tmp_path / "master.json")

    results = await asyncio.gather(
        master.search("삼성"), master.search("KODEX"), master.search("069500")
    )

    assert results == [
        [("005930", "삼성전자")],
        [("069500", "KODEX 200")],
        [("069500", "KODEX 200")],
    ]
    assert len(calls) == 3  # KRX 2개 시장 + KIS ETF 마스터 1회씩


@pytest.mark.asyncio
async def test_concurrent_searches_share_failed_etf_refresh(tmp_path, monkeypatch):
    stock_html = "<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>"
    calls: list[str] = []

    async def fake_get(_client, url, **_kwargs):
        calls.append(url)
        await asyncio.sleep(0.01)
        if url.endswith("kospi_code.mst.zip"):
            raise httpx.ConnectError("ETF source offline")
        return httpx.Response(
            200, content=stock_html.encode("euc-kr"), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    master = StockMaster(tmp_path / "master.json")

    results = await asyncio.gather(
        master.search("삼성"), master.search("삼성"), master.search("삼성")
    )

    assert results == [[("005930", "삼성전자")]] * 3
    assert len(calls) == 3  # 실패 결과도 동시 대기자끼리 공유
    assert master._loaded is False  # 나중에 새로 들어온 요청은 ETF를 재시도
    assert await master.search("삼성") == [("005930", "삼성전자")]
    assert len(calls) == 6  # 새 요청은 KRX 2개 + KIS ETF를 다시 시도
