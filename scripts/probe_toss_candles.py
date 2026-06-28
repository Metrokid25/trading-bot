"""토스증권 Open API 캔들 프로브 — 과거 분봉/프리장 제공 여부 확인용 일회성 진단.

목적(미검증 가설 판가름):
  A) GET /api/v1/candles 가 과거 1분봉을 주는가, 어디까지 거슬러 가나?
  B) 그 1분봉에 NXT 프리장(08:00~08:50) 봉이 *포함*돼 오는가?
     (토스 캔들엔 거래소/세션 파라미터가 없음 → 통합 시세면 프리장이 섞여 올 수 있음)
  C) 응답 스키마(시간/OHLCV 필드명) — 본 수집기 구현 전 확인.

인증: OAuth2 client_credentials (POST /oauth2/token). 자격증명은 .env 에서 로드.

사용:
  ./.venv/Scripts/python.exe scripts/probe_toss_candles.py
  ./.venv/Scripts/python.exe scripts/probe_toss_candles.py --symbol 042700 --before 2026-06-24T09:00:00+09:00 --pages 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

KST = timezone(timedelta(hours=9))

# 응답에서 시각으로 쓰일 만한 후보 필드명(스키마 미확정 → 방어적으로 탐색).
_TIME_KEYS = ("timestamp", "time", "dateTime", "datetime", "startTime",
             "openTime", "dt", "date", "baseTime")


def _get_token() -> str:
    cid = settings.TOSS_CLIENT_ID
    secret = settings.TOSS_CLIENT_SECRET
    if not cid or not secret:
        raise SystemExit(
            "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 .env 에 비어 있음. "
            ".env 에 발급받은 client_id(c_...)/client_secret(s_...) 를 넣어줘."
        )
    r = httpx.post(
        f"{settings.TOSS_BASE_URL}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    if r.status_code != 200:
        raise SystemExit(f"[토큰 실패] {r.status_code}\n{r.text[:800]}")
    tok = r.json().get("access_token")
    if not tok:
        raise SystemExit(f"[토큰 응답에 access_token 없음]\n{r.text[:800]}")
    print(f"[auth] access_token 획득 (len={len(tok)})")
    return tok


def _extract_list(payload) -> list[dict]:
    """응답 봉투(list / {candles} / {result:{candles}} 등)에서 봉 리스트를 꺼낸다."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("candles", "data", "items", "content"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        # 중첩 봉투: result/data 가 dict 면 그 안에서 다시 찾는다.
        for k in ("result", "data"):
            v = payload.get(k)
            if isinstance(v, dict):
                got = _extract_list(v)
                if got:
                    return got
    return []


def _vol(bar: dict) -> float:
    try:
        return float(bar.get("volume") or 0)
    except (TypeError, ValueError):
        return 0.0


def _close(bar: dict):
    return bar.get("closePrice") or bar.get("close")


def _parse_kst(bar: dict) -> datetime | None:
    for k in _TIME_KEYS:
        if k in bar and bar[k] not in (None, ""):
            raw = bar[k]
            try:
                if isinstance(raw, (int, float)):  # epoch (s or ms)
                    sec = raw / 1000 if raw > 1e12 else raw
                    return datetime.fromtimestamp(sec, KST)
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)
                return dt.astimezone(KST)
            except (ValueError, OSError):
                return None
    return None


def _fetch(token: str, symbol: str, interval: str, count: int, before: str | None) -> dict:
    params = {"symbol": symbol, "interval": interval, "count": count}
    if before:
        params["before"] = before
    r = httpx.get(
        f"{settings.TOSS_BASE_URL}/api/v1/candles",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20.0,
    )
    print(f"[candles] GET {r.request.url}\n         -> {r.status_code}")
    if r.status_code != 200:
        raise SystemExit(f"[캔들 실패] {r.status_code}\n{r.text[:800]}")
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="042700", help="종목코드 (기본 한미반도체)")
    ap.add_argument("--interval", default="1m", choices=["1m", "1d"])
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--before", default="2026-06-24T09:00:00+09:00",
                   help="이 시각 이전 봉부터 (6월 프리장 확인용 기본값)")
    ap.add_argument("--pages", type=int, default=3, help="before 페이징 횟수(깊이 확인)")
    args = ap.parse_args()

    token = _get_token()

    before = args.before
    oldest_overall: datetime | None = None
    pre_slot_total = 0       # 09:00 이전 시간슬롯 봉 수(패딩 포함)
    pre_traded_total = 0     # 그 중 volume>0 (실제 프리장 체결)
    first_dump_done = False

    for page in range(1, args.pages + 1):
        payload = _fetch(token, args.symbol, args.interval, args.count, before)
        bars = _extract_list(payload)
        if not bars:
            print(f"[page {page}] 봉 0개 — raw 일부:\n{str(payload)[:600]}")
            break

        if not first_dump_done:
            print(f"\n[스키마] 첫 봉 raw = {bars[0]}\n")
            first_dump_done = True

        # (parsed_dt, bar) 페어
        rows = [(t, b) for b, t in ((b, _parse_kst(b)) for b in bars) if t is not None]
        if not rows:
            print(f"[page {page}] 시간 파싱 실패. 첫 봉 키: {list(bars[0].keys())}")
            break

        times = [t for t, _ in rows]
        newest, oldest = max(times), min(times)
        pre = [(t, b) for t, b in rows if t.hour < 9]            # 09:00 이전
        pre_traded = [(t, b) for t, b in pre if _vol(b) > 0]      # 실제 체결 있는 프리장
        after = [t for t in times if t.hour >= 16]
        pre_slot_total += len(pre)
        pre_traded_total += len(pre_traded)
        oldest_overall = oldest if oldest_overall is None else min(oldest_overall, oldest)

        print(f"[page {page}] 봉 {len(bars)}개 | {oldest:%Y-%m-%d %H:%M} ~ {newest:%Y-%m-%d %H:%M} KST"
              f" | <09:00 {len(pre)}개(체결>0 {len(pre_traded)}) | >=16:00 {len(after)}개")
        if pre_traded:
            for t, b in sorted(pre_traded)[:5]:
                print(f"           ▶ 프리장 실체결 {t:%m-%d %H:%M}  close={_close(b)}  vol={b.get('volume')}")
        elif pre:
            t, b = sorted(pre)[0]
            print(f"           (프리장 슬롯 있으나 전부 vol=0 — 예: {t:%m-%d %H:%M} close={_close(b)} vol={b.get('volume')})")

        before = oldest.isoformat()

    print("\n===== 판정 =====")
    print(f"가장 멀리 거슬러 닿은 시각: {oldest_overall:%Y-%m-%d %H:%M} KST" if oldest_overall else "데이터 없음")
    if pre_traded_total > 0:
        print(f"✅ 토스 API가 과거 프리장 '실체결'(volume>0) 봉을 줌 (총 {pre_traded_total}개) "
              "→ NXT 프리장 백테스트 가능. 게임 끝.")
    elif pre_slot_total > 0:
        print(f"⚠️ 09:00 이전 시간슬롯 봉은 오는데 전부 volume=0 (패딩/합성). "
              "실제 프리장 체결은 안 들어옴 → 정규장만 유효, 프리장은 갭 프록시/forward.")
    else:
        print("❌ 09:00 이전 봉 자체가 없음 → 정규장만.")


if __name__ == "__main__":
    main()
