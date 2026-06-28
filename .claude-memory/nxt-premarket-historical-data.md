---
name: nxt-premarket-historical-data
description: "과거 NXT 프리장 분봉 = 토스 Open API로 확보 가능(확인됨). KIS/트뷰/크레온은 불가"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 9085dd3e-443d-4ba3-b770-e565aacb1c7f
---

KR 주식 **과거(historical) NXT 프리장(08:00~08:50) 분봉**은 2026-06 기준 어떤 retail 경로로도 받을 수 없다. NXT(넥스트레이드)가 2025-03 출범한 신생 거래소라 historical 상품이 아직 없음.

소스별 (2026-06-28 조사):
- **KIS API**: 분봉 당일만(`inquire-time-itemchartprice` FHKST03010200). NXT historical 엔드포인트 없음.
- **TradingView(유료 포함)**: 한국 데이터 = KRX 거래소 한정. NXT 미취급(별도 파트너십 없음). 유료는 실시간 KRX + 히스토리 깊이만 늘 뿐 프리장 무관. `collect_tv.py`는 `extended_session=False` 정규장만.
- **크레온(대신 CYBOS Plus)**: KRX 분봉은 1년+ 깊게 줌(본장 백테스트엔 최적). 단 StockChart의 **NXT historical은 제공 일정 미정**.
- **토스증권 Open API** ✅ **확인됨 (정답)**: `GET /api/v1/candles` interval `1m`/`1d`, `before`(ISO) 페이징으로 과거 1분봉. `result.candles[]` 봉투, 봉 필드 `timestamp`/`openPrice`/`highPrice`/`lowPrice`/`closePrice`/`volume`/`currency`(전부 str), count 최대 200/콜.
  - **프로브(2026-06-28, `scripts/probe_toss_candles.py`, 한미반도체 042700)**: 06-24 프리장 08:01~08:50 **실체결 봉 다수**(예: 08:03 close 252500 vol 5938, 08:05 246500 vol 7011) — **NXT 프리장 historical을 진짜로 줌**. 16:00 이후 애프터마켓 봉도 옴 = NXT 통합 12시간 세션 전체.
  - 거래소/세션 파라미터는 없음(통합 시세로 다 섞여 옴). 09:00 직전 무거래 분은 vol=0 패딩 봉.
  - 인증: OAuth2 client_credentials, `POST /oauth2/token`, base `https://openapi.tossinvest.com`. 자격증명 `.env` `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`(`config/settings.py`).
  - 미확인: `before` 페이징 최대 깊이(6월 1일/그 이전까지 닿는지) — 추가 확인 필요.
- **우리 파이프라인**: NXT 프리장 실수집은 `market_code=UN`으로 **forward(당일)만**. 16:00 `full_pipeline_job`.

결론/적용:
- **6월 백테스트의 프리장 조건 = 09:00 시가 갭상승 프록시**(전일종가 대비 +X%)로 대체. 프리장 급등은 정규장 시가 갭으로 발현됨.
- **본장 6월 분봉 = 무료 tvDatafeed 우선, 깊이 부족 시 크레온**. 트뷰 결제 불필요.
- **진짜 프리장 = 7월+ forward 수집**으로 쌓고, 나중에 "갭 ↔ 실제 프리장" 상관으로 프록시 보정.
- NXT 데이터 상품이 성숙하면(크레온 NXT historical 출시 등) 재확인.

관련: [[trading-bot-purpose]], [[data-accumulation-machine]]
