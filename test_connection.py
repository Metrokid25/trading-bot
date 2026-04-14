"""한국투자증권 모의투자 API 연결 테스트.

.env에서 키를 읽어 토큰 발급 → 삼성전자(005930) 현재가 조회까지 수행.
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    app_key = os.getenv("KIS_PAPER_APP_KEY")
    app_secret = os.getenv("KIS_PAPER_APP_SECRET")
    base_url = os.getenv("KIS_PAPER_BASE_URL", "https://openapivts.koreainvestment.com:29443")

    if not app_key or not app_secret or app_key.startswith("your_"):
        print("[ERROR] .env 에 KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET 를 설정하세요.")
        return 1

    print(f"[1/2] 토큰 발급 요청 → {base_url}")
    token_resp = requests.post(
        f"{base_url}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        headers={"content-type": "application/json"},
        timeout=10,
    )
    if token_resp.status_code != 200:
        print(f"[ERROR] 토큰 발급 실패: {token_resp.status_code} {token_resp.text}")
        return 1
    access_token = token_resp.json()["access_token"]
    print(f"  OK  access_token={access_token[:20]}...")

    print("[2/2] 삼성전자(005930) 현재가 조회")
    price_resp = requests.get(
        f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {access_token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "FHKST01010100",
        },
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"},
        timeout=10,
    )
    if price_resp.status_code != 200:
        print(f"[ERROR] 시세 조회 실패: {price_resp.status_code} {price_resp.text}")
        return 1

    data = price_resp.json()
    if data.get("rt_cd") != "0":
        print(f"[ERROR] 응답 오류: {data.get('msg_cd')} {data.get('msg1')}")
        return 1

    out = data["output"]
    print(f"  종목명   : {out.get('bstp_kor_isnm', '삼성전자')}")
    print(f"  현재가   : {int(out['stck_prpr']):,} 원")
    print(f"  전일대비 : {out['prdy_vrss']} ({out['prdy_ctrt']}%)")
    print(f"  거래량   : {int(out['acml_vol']):,} 주")
    print("\n[OK] 연결 테스트 성공")
    return 0


if __name__ == "__main__":
    sys.exit(main())
