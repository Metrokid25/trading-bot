"""Probe which KIS market-division code yields NXT premarket (08:00~09:00) minute bars.

READ-ONLY by design:
- only calls the minute-candle quotation TR (FHKST03010200)
- no orders, no INSERT/UPDATE/DELETE, no token values printed

The production wrapper (core/kis_api.py) hardcodes FID_COND_MRKT_DIV_CODE="J"
(KRX regular session). This probe reuses KISClient's token/header/rate-limit
infrastructure but varies the market-division code so we can see, empirically,
which code (J / NX / UN) returns 08:00~09:00 minute data on the KIS REAL server.

Usage:
    .venv/Scripts/python.exe scripts/probe_nxt_minute_source.py
    .venv/Scripts/python.exe scripts/probe_nxt_minute_source.py --code 005930 --hhmmss 085500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.kis_api import KISClient  # noqa: E402

# KRX 정규장 / NXT / 통합(KRX+NXT) 후보. 어느 코드가 장전 분봉을 주는지 확인용.
MARKET_CODES = ("J", "NX", "UN")
MINUTE_TR_ID = "FHKST03010200"
MINUTE_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"


async def probe_once(
    client: KISClient, code: str, hhmmss: str, market_code: str
) -> dict[str, object]:
    """단일 (시장코드, 시각) 조합으로 분봉 TR 1회 호출. 응답 메타만 반환."""
    await client._market_limiter.acquire()
    headers = await client._real_headers(MINUTE_TR_ID)
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": market_code,
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": hhmmss,
        "FID_PW_DATA_INCU_YN": "Y",
    }
    try:
        r = await client._real_client.get(
            MINUTE_ENDPOINT, headers=headers, params=params
        )
    except Exception as exc:  # noqa: BLE001
        return {"market_code": market_code, "error": f"{type(exc).__name__}: {exc}"}

    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    rt_cd = body.get("rt_cd")
    msg1 = (body.get("msg1") or "").strip()
    rows = body.get("output2") or []

    hours = sorted(str(row.get("stck_cntg_hour") or "").zfill(6) for row in rows if row.get("stck_cntg_hour"))
    premarket = [h for h in hours if "080000" <= h < "090000"]

    return {
        "market_code": market_code,
        "http_status": r.status_code,
        "rt_cd": rt_cd,
        "msg1": msg1,
        "row_count": len(rows),
        "min_hour": hours[0] if hours else None,
        "max_hour": hours[-1] if hours else None,
        "premarket_count": len(premarket),
        "premarket_min": premarket[0] if premarket else None,
        "premarket_max": premarket[-1] if premarket else None,
    }


async def run(code: str, hhmmss: str) -> int:
    client = KISClient()
    try:
        # 토큰 1회 확보 (REAL 시세 서버). 실패 시 즉시 중단.
        await client._ensure_real_token()
        print("KIS REAL token: OK (시세는 항상 REAL 서버)")
        print(f"probe target: code={code} hhmmss={hhmmss}")
        print(f"TR={MINUTE_TR_ID} endpoint={MINUTE_ENDPOINT}")
        print("=" * 72)

        results = []
        for market_code in MARKET_CODES:
            res = await probe_once(client, code, hhmmss, market_code)
            results.append(res)
            if "error" in res:
                print(f"[{market_code}] ERROR: {res['error']}")
                continue
            print(
                f"[{market_code}] http={res['http_status']} rt_cd={res['rt_cd']} "
                f"rows={res['row_count']} time_range={res['min_hour']}~{res['max_hour']} "
                f"premarket(08~09)={res['premarket_count']} "
                f"({res['premarket_min']}~{res['premarket_max']})"
            )
            if res["msg1"]:
                print(f"      msg1: {res['msg1']}")

        print("=" * 72)
        winners = [
            r["market_code"]
            for r in results
            if "error" not in r and int(r.get("premarket_count") or 0) > 0
        ]
        if winners:
            print(f"VERDICT: NXT premarket data available via market_code(s): {winners}")
        else:
            any_rows = any(int(r.get("row_count") or 0) > 0 for r in results if "error" not in r)
            if any_rows:
                print("VERDICT: TR responds, but NO 08:00~09:00 rows under any code "
                      "(premarket not served by this TR, or no data for this day/time).")
            else:
                print("VERDICT: No rows under any code — check trading day / time / token.")
        return 0
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only probe for NXT premarket minute data source."
    )
    parser.add_argument("--code", default="005930", help="Stock code (default 005930).")
    parser.add_argument(
        "--hhmmss",
        default="085500",
        help="Query anchor time HHMMSS, past-direction (default 085500).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(run(args.code, args.hhmmss)))
