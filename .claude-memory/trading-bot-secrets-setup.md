---
name: trading-bot-secrets-setup
description: 노트북 .env 키 세팅 완료 상태와 미사용 키 정보
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccc5d3b-07ea-40eb-acd9-25c20010d3fa
---

2026-06-27 노트북에서 `.env`를 `.env.example`에서 새로 생성하고 키를 채워 세팅 완료. KIS 모의투자(PAPER) 키·시크릿·계좌, 텔레그램 봇 토큰·chat_id 모두 입력됨. KIS 연결(삼성전자 시세 조회)·텔레그램 전송 테스트 통과.

텔레그램 봇은 `@zzapmoneying_bot`(쩝머닝매매봇), 알림 수신 chat은 사용자 본인(@Metrokid1101).

`KIS_HTS_ID`는 `your_hts_id` placeholder로 남겨둠 — 코드 어디서도 참조하지 않아 무방. 실시간 시세 승인키는 app_key/secret으로 `/oauth2/Approval`에서 발급한다(`core/websocket_client.py`).

**Why:** `.env`는 gitignore라 PC 간 동기화 안 됨. 노트북에서 처음부터 세팅했음.
**How to apply:** 키를 다시 조회할 일이 있으면 재발급(revoke/reset) 말고 KIS 포털·BotFather에서 조회만 할 것. 실행은 [[laptop-run-env]] 참고.
