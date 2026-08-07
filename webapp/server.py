"""로컬 종목 등록 웹 대시보드 (FastAPI).

main.py(섹터 알림 봇)·main_tracker.py(데이터 수집)와 격리된 별도 프로세스.
KIS 시세/매매·텔레그램 발송은 건드리지 않고, 종목 검색(StockMaster)과
등록(SectorStore.upsert_sector)만 한다. 기본 localhost 전용으로 띄운다.

실행:
    .venv/Scripts/python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from config.settings import settings
from core.kis_api import KISClient
from core.time_utils import now_kst
from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore, normalize_sector_name
from data.stock_master import StockMaster

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 등록자 표시 기본값 — UI에서 이름을 지우고 등록해도 이 값으로 스탬프된다.
DEFAULT_AUTHOR = "황파파"

# 웹 등록 픽 유효기간(일). 텔레그램 /p 의 7일과 달리 웹 등록 유니버스는
# "섹터 삭제 전까지 상시 유지"가 운영 규칙 — 2026-07-10 유니버스가 7일 만료로
# 장중 증발(72→8종목)한 사고 재발 방지. 사실상 무기한(1년, 등록·추가 시마다 갱신).
WEB_PICK_EXPIRES_DAYS = 365

# raw_input 스탬프 "[web:이름]"에서 등록자 추출용
_WEB_AUTHOR_RE = re.compile(r"^\[web:(.+)\]$")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _web_key_error(x_web_key: str) -> str | None:
    """공유 비밀번호 검사. 통과면 None, 실패면 사용자에게 보여줄 메시지.

    - WEB_SHARED_KEY 미설정이면 전부 거부 — ALLOWED_TELEGRAM_USERS(빈 리스트=전부 거부)와
      같은 안전 기본값 원칙.
    - HTTP 헤더는 latin-1 제약이 있어 한글 키는 브라우저에서 전송 자체가 불가능하다.
      비ASCII 키가 설정돼 있으면 명확한 메시지로 거부해 조용한 벽돌 상태를 막는다.
    """
    key = settings.WEB_SHARED_KEY.strip()
    if not key:
        return "서버에 공유 비밀번호(WEB_SHARED_KEY)가 설정되지 않았습니다 (.env 확인)"
    if not key.isascii():
        return "WEB_SHARED_KEY는 영문·숫자만 지원합니다 (미니PC .env 수정 필요)"
    if not hmac.compare_digest(x_web_key.encode(), key.encode()):
        return "공유 비밀번호가 올바르지 않습니다"
    return None


def _to_int(v: object) -> int | None:
    try:
        return int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class _TTLCache:
    """단순 TTL 캐시 — 다중 브라우저 탭의 60초 폴링이 KIS/야후 호출을
    중복 발사하지 않게 한다 (미니PC에서 KIS 레이트리밋은 수집·페이퍼
    상주 프로세스와 공유되므로 웹앱 호출량을 억제해야 한다)."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._d: dict[object, tuple[float, object]] = {}

    def get(self, key: object) -> object | None:
        hit = self._d.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        return None

    def set(self, key: object, value: object) -> None:
        self._d[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._d.clear()


_index_chart_cache = _TTLCache(60.0)
_indices_cache = _TTLCache(60.0)
_markets_cache = _TTLCache(60.0)
_flows_cache = _TTLCache(60.0)
_quote_cache = _TTLCache(30.0)
# 종목별 유효 시장코드 메모 (code -> (기록시각, "UN"|"J")) — NXT 미상장 종목이
# 매 폴링마다 UN 실패 후 J 재호출로 2배 호출을 내지 않게 30분 기억.
_quote_market: dict[str, tuple[float, str]] = {}
_QUOTE_MARKET_TTL = 1800.0


def _clear_runtime_caches() -> None:
    """테스트 격리용 — 모듈 전역 캐시 전부 초기화."""
    for c in (_index_chart_cache, _indices_cache, _markets_cache, _flows_cache, _quote_cache):
        c.clear()
    _quote_market.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = SectorStore()
    await store.open()
    await store.consolidate_case_insensitive_sectors()
    master = StockMaster()
    kis = KISClient()
    http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
    app.state.store = store
    app.state.master = master
    app.state.kis = kis
    app.state.http = http
    try:
        yield
    finally:
        await store.close()
        await kis.close()
        await http.aclose()


app = FastAPI(title="trading-bot 종목 등록", lifespan=lifespan)


@app.middleware("http")
async def guard_mutations(request: Request, call_next):
    """/api 하위 변경 요청(POST 등)은 전부 공유 비밀번호 필요.

    라우트별 opt-in(dependencies=)이 아니라 미들웨어 기본 보호 — 앞으로 변경 라우트가
    추가돼도 자동으로 보호 대상에 들어간다. 조회(GET)는 키 없이 허용.
    """
    if request.url.path.startswith("/api") and request.method not in _SAFE_METHODS:
        err = _web_key_error(request.headers.get("X-Web-Key", ""))
        if err:
            return JSONResponse(status_code=401, content={"detail": err})
    return await call_next(request)


# ----- 의존성 (테스트에서 override 가능) -----
def get_store(request: Request) -> SectorStore:
    return request.app.state.store


def get_master(request: Request) -> StockMaster:
    return request.app.state.master


def get_kis(request: Request) -> KISClient:
    return request.app.state.kis


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


# ----- 요청/응답 모델 -----
class StockIn(BaseModel):
    code: str
    name: str | None = None


class _SectorNameIn(BaseModel):
    sector_name: str = Field(min_length=1)

    @field_validator("sector_name")
    @classmethod
    def normalize_and_require_sector_name(cls, value: str) -> str:
        normalized = normalize_sector_name(value)
        if not normalized:
            raise ValueError("섹터명을 입력하세요")
        return normalized


class RegisterIn(_SectorNameIn):
    pick_date: str | None = None
    stocks: list[StockIn] = Field(min_length=1)
    author: str = Field(default="", max_length=20)  # 빈 값이면 핸들러가 DEFAULT_AUTHOR로


class RemoveStockIn(_SectorNameIn):
    stock_code: str = Field(min_length=1)


class RemoveSectorIn(_SectorNameIn):
    pass


class MentorSignalIn(BaseModel):
    article_id: str = Field(min_length=1, max_length=100)
    article_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    author_id: str = Field(min_length=1, max_length=200)
    posted_at: str
    detected_at: str = Field(min_length=1)
    stock_name: str = Field(min_length=1, max_length=200)
    stock_code: str = Field(pattern=r"^\d{4}[0-9A-Z]\d$")
    sector: str | None = Field(default=None, max_length=100)
    signal_type: str = Field(min_length=1, max_length=30)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1, max_length=2000)
    source_url: str | None = Field(default=None, max_length=2000)
    mode: str

    @field_validator("posted_at", "detected_at")
    @classmethod
    def require_iso_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("ISO-8601 timestamp required") from exc
        if parsed.tzinfo is None:
            raise ValueError("timezone offset required")
        return value


_mentor_ingest_lock = asyncio.Lock()


def _mentor_duplicate_response(existing: dict) -> dict:
    try:
        previous = json.loads(existing.get("trading_response") or "{}")
    except (TypeError, json.JSONDecodeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    return {
        **previous,
        "accepted": True,
        "duplicate": True,
        "event_id": existing["id"],
        "live_order": "disabled",
    }


# ----- API -----
@app.get("/api/search")
async def search(
    q: str = "",
    limit: int = 8,
    master: StockMaster = Depends(get_master),
) -> list[dict]:
    """종목 자동완성 후보. q는 한글/영문/6자리 KRX 영숫자 코드."""
    results = await master.search(q, limit=limit)
    return [
        {"code": code, "name": name, "type": master.instrument_type(code)}
        for code, name in results
    ]


@app.get("/api/picks")
async def list_picks(
    store: SectorStore = Depends(get_store),
    master: StockMaster = Depends(get_master),
) -> list[dict]:
    """활성 픽 + 종목 현황."""
    await master.ensure_loaded()
    picks = await store.get_active_picks()
    out: list[dict] = []
    for pick in picks:
        stocks = await store.get_stocks_by_pick(pick.id) if pick.id else []
        m = _WEB_AUTHOR_RE.match(pick.raw_input or "")
        out.append(
            {
                "pick_id": pick.id,
                "pick_date": pick.pick_date,
                "expires_at": pick.expires_at.isoformat(),
                "registered_by": m.group(1) if m else None,
                "stocks": [
                    {
                        "code": s.stock_code,
                        "name": s.stock_name,
                        "sector": s.sector_name,
                        "type": master.instrument_type(s.stock_code),
                    }
                    for s in stocks
                ],
            }
        )
    return out


_INDICES = [("0001", "코스피"), ("1001", "코스닥")]
_INDEX_NAMES = dict(_INDICES)


@app.get("/api/indices")
async def indices(kis: KISClient = Depends(get_kis)) -> list[dict]:
    """국내 주요 지수(코스피·코스닥) 현재값·등락·시장폭. 실패 시 값은 null. 60초 캐시."""
    cached = _indices_cache.get("all")
    if cached is not None:
        return cached  # type: ignore[return-value]
    out: list[dict] = []
    ok = True
    for code, name in _INDICES:
        try:
            q = await kis.get_index(code)
            out.append({
                "code": code,
                "name": name,
                "value": q["value"],
                "change": q["change"],
                "change_rate": q["change_rate"],
                # 시장 폭 (상승/상한/보합/하락/하한)
                "up": q.get("up_count"),
                "upper": q.get("upper_count"),
                "flat": q.get("flat_count"),
                "down": q.get("down_count"),
                "lower": q.get("lower_count"),
            })
        except Exception:
            ok = False
            out.append({"code": code, "name": name, "value": None, "change": None, "change_rate": None})
    if ok:
        _indices_cache.set("all", out)  # 실패 응답은 캐시하지 않음
    return out


@app.get("/api/index-chart")
async def index_chart(code: str = "0001", kis: KISClient = Depends(get_kis)) -> dict:
    """당일 업종지수 5분봉 선차트 데이터 (과거→최신). 60초 캐시.

    code: '0001'(코스피) / '1001'(코스닥). 실패 시 bars=[] + null 값.
    프리장 등 당일 봉이 아직 없으면 직전 거래일 차트가 나간다 — date 필드로
    구분 가능하며 프론트가 날짜 라벨을 표시한다.
    """
    if code not in _INDEX_NAMES:
        raise HTTPException(status_code=400, detail="지원하지 않는 지수 코드입니다")
    cached = _index_chart_cache.get(code)
    if cached is not None:
        return cached  # type: ignore[return-value]

    out = {
        "code": code, "name": _INDEX_NAMES[code], "value": None, "change": None,
        "change_rate": None, "prev_close": None, "date": None, "bars": [],
    }
    try:
        data = await kis.get_index_minute_chart(code, interval_sec=300)
        s = data["summary"]
        raw = data["bars"]  # 최신→과거
        latest_date = raw[0].get("stck_bsop_date") if raw else None
        bars = []
        for row in raw:
            if row.get("stck_bsop_date") != latest_date:
                continue  # 이월 봉(직전 거래일) 제외 — 최신 거래일만
            c = _to_float(row.get("bstp_nmix_prpr"))
            if c is None:
                continue
            bars.append({"t": row.get("stck_cntg_hour", ""), "c": c})
        bars.reverse()
        out.update({
            "value": _to_float(s.get("bstp_nmix_prpr")),
            "change": _to_float(s.get("bstp_nmix_prdy_vrss")),
            "change_rate": _to_float(s.get("bstp_nmix_prdy_ctrt")),
            "prev_close": _to_float(s.get("prdy_nmix")),
            "date": latest_date,
            "bars": bars,
        })
        _index_chart_cache.set(code, out)
    except Exception as e:
        # 실패 응답은 캐시하지 않음 — 다음 요청에서 재시도. 무소음 실패 방지용 로그.
        logger.warning(f"index-chart 조회 실패 code={code}: {type(e).__name__} {e}")
    return out


# Yahoo Finance(비공식) 심볼. group: 대시보드 카드 묶음 / unit: 표시 단위 힌트.
_YAHOO = [
    ("NQ=F", "나스닥F", "us_futures", "idx"),
    ("YM=F", "다우F", "us_futures", "idx"),
    ("ES=F", "S&PF", "us_futures", "idx"),
    ("^IXIC", "나스닥", "us", "idx"),
    ("^DJI", "다우", "us", "idx"),
    ("^GSPC", "S&P500", "us", "idx"),
    ("KRW=X", "환율", "fx", "krw"),
    ("CL=F", "WTI", "fx", "usd"),
    ("EWY", "한국ETF", "fx", "usd"),
]


def _yahoo_to_market(name: str, group: str, unit: str, meta: dict) -> dict:
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose")
    if price is None:
        return {"name": name, "group": group, "unit": unit,
                "value": None, "change": None, "change_rate": None}
    change = (price - prev) if prev else 0.0
    rate = ((price - prev) / prev * 100) if prev else 0.0
    return {"name": name, "group": group, "unit": unit,
            "value": price, "change": change, "change_rate": rate}


@app.get("/api/markets")
async def markets(http: httpx.AsyncClient = Depends(get_http)) -> list[dict]:
    """미국 선물/미국장/환율·유가 (Yahoo Finance, 동시 조회). 실패 항목은 값 null. 60초 캐시."""
    cached = _markets_cache.get("all")
    if cached is not None:
        return cached  # type: ignore[return-value]

    async def fetch_one(symbol: str, name: str, group: str, unit: str) -> dict:
        try:
            r = await http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "1d"},
            )
            meta = r.json()["chart"]["result"][0]["meta"]
            return _yahoo_to_market(name, group, unit, meta)
        except Exception:
            return {"name": name, "group": group, "unit": unit,
                    "value": None, "change": None, "change_rate": None}

    out = list(await asyncio.gather(*(fetch_one(*item) for item in _YAHOO)))
    if any(m["value"] is not None for m in out):
        _markets_cache.set("all", out)  # 전 항목 실패(야후 차단 등)는 캐시하지 않음
    return out


@app.get("/api/flows")
async def flows(kis: KISClient = Depends(get_kis)) -> dict[str, dict]:
    """시장별 투자자 순매수 (개인/외국인/기관, 단위 억원). 실패 시장은 null. 60초 캐시."""
    cached = _flows_cache.get("all")
    if cached is not None:
        return cached  # type: ignore[return-value]
    out: dict[str, dict] = {}
    ok = True
    for key, market in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        try:
            f = await kis.get_market_investor_flow(market)
            # KIS 응답 단위는 백만원 → 억원
            out[key] = {k: round(v / 100) for k, v in f.items()}
        except Exception:
            ok = False
            out[key] = {"individual": None, "foreign": None, "institution": None}
    if ok:
        _flows_cache.set("all", out)
    return out


async def _quote_nxt_fallback(kis: KISClient, code: str) -> dict:
    """통합(UN) 시세 우선, 실패 시 KRX(J)로 폴백.

    UN은 NXT 프리장(08:00~)·애프터장 체결가를 반영한다. NXT 미상장 등으로
    UN이 실패/무가격이면 J 폴백으로 기존 동작을 유지하고, 그 결과를
    _quote_market 에 30분 기억해 매 폴링 2배 호출(UN 실패 후 J 재호출)을 막는다.
    """
    now = time.monotonic()
    memo = _quote_market.get(code)
    try_un = not (memo and now - memo[0] < _QUOTE_MARKET_TTL and memo[1] == "J")
    if try_un:
        try:
            q = await kis.get_quote(code, "UN")
            if q.get("price"):
                _quote_market[code] = (now, "UN")
                return q
        except Exception:
            pass
        _quote_market[code] = (now, "J")
    return await kis.get_quote(code)


@app.get("/api/quotes")
async def quotes(
    codes: str = "",
    kis: KISClient = Depends(get_kis),
) -> dict[str, dict | None]:
    """종목별 현재가·등락률 (NXT 통합 시세, 실패 시 KRX). 실패 종목은 null.

    종목별 30초 캐시 — 다중 탭이 같은 유니버스를 폴링해도 KIS 호출은 30초에
    종목당 1회로 수렴한다.
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:100]
    out: dict[str, dict | None] = {}
    for code in code_list:
        cached = _quote_cache.get(code)
        if cached is not None:
            out[code] = cached  # type: ignore[assignment]
            continue
        try:
            q = await _quote_nxt_fallback(kis, code)
            row = {
                "price": q["price"],
                "change_rate": q["change_rate"],
                "volume": q.get("volume", 0),
                "value": q.get("value", 0),
            }
            _quote_cache.set(code, row)
            out[code] = row
        except Exception:
            out[code] = None  # 실패는 캐시하지 않음 — 다음 요청에서 재시도
    return out


async def _fetch_candles(kis: KISClient, code: str, tf: str) -> list[dict]:
    """단일 종목 봉 조회+파싱. 시간 오름차순. 실패 시 빈 리스트."""
    out: list[dict] = []
    try:
        if tf == "minute":
            # 통합(UN) 우선 — NXT 프리장·애프터장 분봉 포함. 실패/빈 응답은 KRX(J).
            try:
                raw = await kis.get_minute_candles(code, "UN")
            except Exception:
                raw = []
            if not raw:
                raw = await kis.get_minute_candles(code)
            for row in raw:
                c = _to_int(row.get("stck_prpr"))
                if c is None:
                    continue
                out.append({
                    "t": row.get("stck_cntg_hour", ""),
                    "o": _to_int(row.get("stck_oprc")) or c,
                    "h": _to_int(row.get("stck_hgpr")) or c,
                    "l": _to_int(row.get("stck_lwpr")) or c,
                    "c": c,
                    "v": _to_int(row.get("cntg_vol")) or 0,
                })
            out.reverse()  # KIS는 최신→과거. 차트는 과거→최신.
        else:
            end = now_kst()
            start = end - timedelta(days=120)
            raw = await kis.get_daily_candles(
                code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "D"
            )
            for row in raw:
                c = _to_int(row.get("stck_clpr"))
                if c is None:
                    continue
                out.append({
                    "t": row.get("stck_bsop_date", ""),
                    "o": _to_int(row.get("stck_oprc")) or c,
                    "h": _to_int(row.get("stck_hgpr")) or c,
                    "l": _to_int(row.get("stck_lwpr")) or c,
                    "c": c,
                    "v": _to_int(row.get("acml_vol")) or 0,
                })
            out.sort(key=lambda x: x["t"])
    except Exception:
        out = []
    return out


@app.get("/api/candles")
async def candles(
    code: str = "",
    tf: str = "daily",
    kis: KISClient = Depends(get_kis),
) -> dict:
    """봉차트 데이터. tf=daily(일봉 ~최근120일) | minute(당일 1분봉)."""
    code = code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="code가 필요합니다")
    return {"code": code, "tf": tf, "candles": await _fetch_candles(kis, code, tf)}


@app.get("/api/candles-batch")
async def candles_batch(
    codes: str = "",
    tf: str = "daily",
    kis: KISClient = Depends(get_kis),
) -> dict:
    """여러 종목 봉을 한 번에. 행별 미니차트용. {code: candles[]}."""
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:100]
    out: dict[str, list[dict]] = {}
    for code in code_list:
        out[code] = await _fetch_candles(kis, code, tf)
    return {"tf": tf, "candles": out}


@app.post("/api/picks")
async def register(
    body: RegisterIn,
    store: SectorStore = Depends(get_store),
    master: StockMaster = Depends(get_master),
) -> dict:
    """섹터 + 종목 등록. 텔레그램 /p 와 동일 경로(resolve → upsert_sector)."""
    pick_date = body.pick_date or now_kst().strftime("%Y-%m-%d")
    # raw_input 스탬프용 등록자 정리 — "[web:이름]" 한 줄 형식 보호를 위해
    # 대괄호와 개행 등 비인쇄 문자를 제거
    author = "".join(ch for ch in body.author if ch.isprintable())
    author = author.replace("[", "").replace("]", "").strip() or DEFAULT_AUTHOR

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

    pick_template = SectorPick.create(
        pick_date, raw_input=f"[web:{author}]", expires_days=WEB_PICK_EXPIRES_DAYS
    )
    result = await store.upsert_sector(
        body.sector_name, sector_stocks, pick_template, record_pick_event=True
    )
    # 기존 활성 섹터에 추가한 경우 낡은 만료시각이 남지 않게 항상 1년 이상 보장
    if result.pick_id:
        await store.ensure_pick_expiry(result.pick_id, WEB_PICK_EXPIRES_DAYS)
    return {
        "pick_id": result.pick_id,
        "is_new_pick": result.is_new_pick,
        "added": result.added_count,
        "total": result.total_count,
        "skipped": [s.stock_name for s in result.skipped_stocks],
    }


@app.post("/api/signals/mentor")
async def ingest_mentor_signal(
    body: MentorSignalIn,
    store: SectorStore = Depends(get_store),
    master: StockMaster = Depends(get_master),
) -> dict:
    """검증된 멘토 ADD_WATCH를 paper 관심종목으로만 등록한다.

    이 경로는 주문 함수를 호출하지 않는다. 기존 Paper Runner가 trading.db의 활성
    유니버스를 다음 주기에 읽고 자체 전략 조건을 통과한 경우에만 모의 기록한다.
    """
    if body.mode != "paper":
        raise HTTPException(status_code=400, detail="Mentor signal mode must be paper")
    if settings.KIS_ENV != "PAPER":
        raise HTTPException(status_code=409, detail="Mentor signal ingest requires KIS_ENV=PAPER")
    configured_author = settings.MENTOR_AUTHOR_ID.strip()
    if not configured_author:
        raise HTTPException(status_code=503, detail="MENTOR_AUTHOR_ID is not configured")
    if not hmac.compare_digest(
        body.author_id.encode("utf-8"), configured_author.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="Mentor author verification failed")
    if body.signal_type != "ADD_WATCH":
        raise HTTPException(status_code=400, detail="Only ADD_WATCH may enter the paper watchlist")
    if body.confidence < settings.MENTOR_SIGNAL_CONFIDENCE_THRESHOLD:
        raise HTTPException(status_code=422, detail="Signal confidence is below threshold")
    await master.ensure_loaded()
    resolved = await master.resolve(body.stock_code)
    if resolved is None or not resolved[1]:
        raise HTTPException(status_code=400, detail="Unknown stock code")
    canonical_code, canonical_name = resolved
    def normalize_stock_name(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()
    if (canonical_code != body.stock_code
            or normalize_stock_name(canonical_name) != normalize_stock_name(body.stock_name)):
        raise HTTPException(status_code=400, detail="Stock name/code mismatch")

    display_sector = normalize_sector_name(body.sector or "미분류") or "미분류"
    # 수동 섹터와 pick 자체를 분리해 기존 수동 픽의 만료를 연장하지 않는다.
    sector = f"멘토 자동픽 · {display_sector[:70]}"
    key = (
        body.article_id, body.article_revision_hash, body.stock_code, body.signal_type
    )
    existing_before = await store.get_mentor_signal_event(*key)
    if existing_before is not None and existing_before["delivery_status"] != "processing":
        return _mentor_duplicate_response(existing_before)
    if existing_before is None:
        posted_at = datetime.fromisoformat(body.posted_at)
        posted_kst = posted_at.astimezone(now_kst().tzinfo)
        age_hours = (now_kst() - posted_kst).total_seconds() / 3600
        max_age = settings.MENTOR_SIGNAL_MAX_AGE_HOURS
        if max_age <= 0:
            raise HTTPException(status_code=503, detail="MENTOR_SIGNAL_MAX_AGE_HOURS is invalid")
        if age_hours > max_age:
            raise HTTPException(status_code=422, detail="Mentor signal article is stale")
        if age_hours < -(5 / 60):
            raise HTTPException(status_code=422, detail="Mentor signal article is in the future")
    async with _mentor_ingest_lock:
        reservation, inserted = await store.reserve_mentor_signal_event(
            {
                **body.model_dump(),
                "sector": sector,
            }
        )
        if not inserted and reservation["delivery_status"] != "processing":
            return _mentor_duplicate_response(reservation)
        if not reservation["payload_matches"]:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key is already reserved for a different payload",
            )
        event_id = reservation["id"]

        # processing은 이전 시도 중단 또는 다중 worker 경합 상태다. upsert 자체가
        # 멱등이므로 그대로 재개하고 최종 감사 상태를 registered로 확정한다.
        existing = await store.get_mentor_signal_event(*key)
        if existing is not None and existing["delivery_status"] != "processing":
            return _mentor_duplicate_response(existing)

        reserved_at = datetime.fromisoformat(reservation["created_at"])
        recovered_pick_id = await store.find_reserved_mentor_registration(
            sector=sector,
            stock_code=canonical_code,
            tracking_start_date=reservation["created_at"],
        )
        if recovered_pick_id is not None:
            response = {
                "accepted": True, "duplicate": False, "registered": True,
                "already_watched": False, "pick_id": recovered_pick_id,
                "stock_code": canonical_code, "stock_name": canonical_name,
                "sector": display_sector, "storage_sector": sector,
                "paper_runner": "will_load_on_next_cycle", "live_order": "disabled",
            }
            await store.complete_mentor_signal_event(event_id, response)
            return {**response, "event_id": event_id}

        pick_template = SectorPick.create(
            (body.posted_at or now_kst().isoformat())[:10],
            raw_input=f"[mentor:{body.article_id}]",
            expires_days=WEB_PICK_EXPIRES_DAYS,
        )
        pick_template.created_at = reserved_at
        pick_template.expires_at = reserved_at + timedelta(days=WEB_PICK_EXPIRES_DAYS)
        stock = SectorStock(
            pick_id=0,
            sector_name=sector,
            stock_code=canonical_code,
            stock_name=canonical_name,
            added_order=1,
        )
        result = await store.upsert_sector(
            sector, [stock], pick_template, record_pick_event=True
        )
        await store.ensure_pick_expiry(result.pick_id, WEB_PICK_EXPIRES_DAYS)
        # 동시 worker가 같은 예약을 함께 재개했더라도, 예약 시각으로 생성된
        # 종목 행을 다시 확인하면 마지막 완료자가 감사 결과를 false로 덮지 않는다.
        applied_pick_id = await store.find_reserved_mentor_registration(
            sector=sector,
            stock_code=canonical_code,
            tracking_start_date=reservation["created_at"],
        )
        registered_by_event = result.added_count > 0 or applied_pick_id is not None
        response = {
            "accepted": True,
            "duplicate": False,
            "registered": registered_by_event,
            "already_watched": not registered_by_event,
            "pick_id": applied_pick_id or result.pick_id,
            "stock_code": canonical_code,
            "stock_name": canonical_name,
            "sector": display_sector,
            "storage_sector": sector,
            "paper_runner": "will_load_on_next_cycle",
            "live_order": "disabled",
        }
        await store.complete_mentor_signal_event(event_id, response)
        return {**response, "event_id": event_id}


@app.post("/api/picks/remove-stock")
async def remove_stock(
    body: RemoveStockIn,
    store: SectorStore = Depends(get_store),
) -> dict:
    """섹터에서 특정 종목 제거. 빈 픽은 자동 archive."""
    result = await store.remove_stock_from_sector(body.sector_name, body.stock_code)
    if not result["removed_from_picks"]:
        raise HTTPException(status_code=404, detail="해당 종목을 찾을 수 없습니다")
    return result


@app.post("/api/picks/remove-sector")
async def remove_sector(
    body: RemoveSectorIn,
    store: SectorStore = Depends(get_store),
) -> dict:
    """섹터 전체 제거(종목 DELETE). 빈 픽은 자동 archive."""
    result = await store.archive_sector(body.sector_name)
    if not result["affected_picks"]:
        raise HTTPException(status_code=404, detail="해당 섹터를 찾을 수 없습니다")
    return result


# ----- 정적 프론트 -----
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
