"""토스증권 Open API 캔들 클라이언트 (백테스트용 과거 분봉 소스).

토스 Open API 는 KIS 와 달리 과거 1분봉을 NXT 프리장(08:00~08:50) 실체결까지
제공한다(2026-06 확인, 최소 1년 보존). 거래소/세션 파라미터는 없고 통합 시세로
프리/정규/애프터가 모두 섞여 내려온다.

인증: OAuth2 client_credentials (POST /oauth2/token), 자격증명은 .env.
캔들: GET /api/v1/candles?symbol=&interval=1m&count<=200&before=<ISO>
응답: {"result": {"candles": [{timestamp, openPrice, highPrice, lowPrice,
       closePrice, volume, currency}, ...]}}  (전부 문자열, 최신→과거 정렬)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from config import settings

KST = timezone(timedelta(hours=9))
_MAX_COUNT = 200
# 토큰 디스크 캐시(24h 유효) — 매 실행 재발급으로 /oauth2/token 가 막히는 것 방지.
_TOKEN_CACHE = Path(settings.DB_PATH).parent / "toss_token.json"


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime          # KST tz-aware
    open: int
    high: int
    low: int
    close: int
    volume: int


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_bar(raw: dict) -> Bar | None:
    ts_raw = raw.get("timestamp")
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return Bar(
        ts=dt.astimezone(KST),
        open=_to_int(raw.get("openPrice")),
        high=_to_int(raw.get("highPrice")),
        low=_to_int(raw.get("lowPrice")),
        close=_to_int(raw.get("closePrice")),
        volume=_to_int(raw.get("volume")),
    )


class TossClient:
    """동기 httpx 기반 토스 캔들 클라이언트. 토큰은 1회 발급 후 재사용."""

    def __init__(self, *, throttle_sec: float = 0.25) -> None:
        # TOSS_PROXY 설정 시 토스 호출만 고정 IP 출구(SSH 터널 등)로 라우팅.
        proxy = settings.TOSS_PROXY or None
        self._client = httpx.Client(base_url=settings.TOSS_BASE_URL, timeout=20.0,
                                    proxy=proxy)
        self._token: str | None = None
        self._throttle = throttle_sec

    def __enter__(self) -> "TossClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _load_cached_token(self) -> str | None:
        try:
            data = json.loads(_TOKEN_CACHE.read_text())
            # 만료 60초 전까지만 유효로 간주.
            if data.get("expires_at", 0) - 60 > time.time():
                return data.get("access_token")
        except (OSError, ValueError):
            pass
        return None

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        cached = self._load_cached_token()
        if cached:
            self._token = cached
            return cached

        cid, secret = settings.TOSS_CLIENT_ID, settings.TOSS_CLIENT_SECRET
        if not cid or not secret:
            raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 .env 에 비어 있음")

        last: httpx.Response | None = None
        for attempt in range(5):
            r = self._client.post(
                "/oauth2/token",
                data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            last = r
            if r.status_code in (403, 429) or r.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))  # 발급 차단/레이트 → 길게 백오프
                continue
            r.raise_for_status()
            payload = r.json()
            tok = payload.get("access_token")
            if not tok:
                raise RuntimeError(f"토큰 응답에 access_token 없음: {r.text[:300]}")
            try:
                _TOKEN_CACHE.write_text(json.dumps({
                    "access_token": tok,
                    "expires_at": time.time() + int(payload.get("expires_in", 86400)),
                }))
            except OSError:
                pass
            self._token = tok
            return tok
        if last is not None:
            last.raise_for_status()
        raise RuntimeError("토큰 발급 실패")

    def _get_candles(self, symbol: str, interval: str, count: int, before: str | None) -> list[Bar]:
        params = {"symbol": symbol, "interval": interval, "count": min(count, _MAX_COUNT)}
        if before:
            params["before"] = before
        last_exc: Exception | None = None
        for attempt in range(6):
            try:
                r = self._client.get(
                    "/api/v1/candles",
                    params=params,
                    headers={"Authorization": f"Bearer {self._ensure_token()}"},
                )
            except httpx.TransportError as exc:  # 연결 끊김/타임아웃 등 → 백오프 후 재시도
                last_exc = exc
                time.sleep(0.6 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            result = r.json().get("result") or {}
            raw = result.get("candles") or []
            return [b for b in (_parse_bar(x) for x in raw) if b is not None]
        if last_exc is not None:
            raise last_exc
        r.raise_for_status()
        return []

    def fetch_1m_range(self, symbol: str, start: date, end: date) -> list[Bar]:
        """[start, end] 날짜 범위의 1분봉 전체를 오름차순으로 반환.

        end 다음날 09:00 을 커서로 과거 방향 페이징하며, start 이전으로 내려가면 멈춘다.
        통합 시세라 프리장/정규장/애프터 봉이 모두 포함된다.
        """
        cursor = datetime.combine(end + timedelta(days=1), datetime.min.time(), KST).replace(hour=9)
        start_dt = datetime.combine(start, datetime.min.time(), KST)
        out: dict[datetime, Bar] = {}

        while True:
            bars = self._get_candles(symbol, "1m", _MAX_COUNT, cursor.isoformat())
            if not bars:
                break
            oldest = min(b.ts for b in bars)
            for b in bars:
                if start_dt <= b.ts:
                    out[b.ts] = b
            if oldest <= start_dt:
                break
            cursor = oldest  # 다음 페이지: 가장 오래된 봉 이전
            if self._throttle:
                time.sleep(self._throttle)

        return sorted(out.values(), key=lambda b: b.ts)
