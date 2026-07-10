---
name: minipc-paper-deploy
description: "미니PC 모의투자 상주 배포 완료(2026-07-06) — 자동시작은 Startup VBS(작업스케줄러 아님), IP/paper_start 등 운영 사실"
metadata:
  node_type: memory
  type: project
---

미니PC = 모의투자(paper) 지정 상주 기기. **2026-07-06 배포·상주 가동 완료.**

**자동시작 메커니즘(중요·비자명)**: 작업 스케줄러(Register-ScheduledTask/schtasks)는
관리자 권한이 필요해 비관리자 세션에서 `Access is denied`로 실패한다. 그래서
**사용자 시작프로그램 폴더의 VBS**로 우회했다:
`C:\Users\미니PC\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\trading-bot-paper.vbs`
→ 로그온 시 `main_tracker.py`를 창 없이(hidden) 기동(`PYTHONIOENCODING=utf-8`).
상주 프로세스 재시작/점검 시 이 VBS를 실행하거나 `main_tracker.py`를 직접 띄우면 된다.
크래시 자동재시작은 없음(스케줄러 대비 트레이드오프) — 필요하면 관리자로 작업
스케줄러 태스크(`trading-bot-paper-resident`, AtLogOn) 등록으로 승격 가능.

**운영 사실** (2026-07-06 오후 A안 전환 후):
- 상주 = **두 프로세스**: `main_tracker.py`(수집 전담, 16:00) + `python -m
  strategy.paper_runner --market-schedule`(페이퍼 전담, KST 타임테이블 5/10/30분).
  VBS가 둘 다 기동. main_tracker의 paper_job은 제거됨(이중 기록자 방지).
- 유니버스 = **미니PC trading.db 라이브** — **픽 등록/교체는 미니PC 웹앱에서만**
  (gitignore라 타 기기 등록분 안 옴). 벤치=당일 유니버스 동일가중 무비용,
  연속 종목 오버나이트 포함. regime=live_universe_v1.
- `paper_start = 2026-07-06` 고정. `db/paper.db`(WAL). 비용 0.25%/편도 스탬프.
- 미니PC 공인 IP `182.212.35.163` = 토스 화이트리스트 이미 등록(프리장 실체결 200 OK).
- 미니PC는 집 고정회선 직결 → `.env`에 `TOSS_PROXY` 없음(노트북만 AWS 터널 사용).
- 절전: AC standby=0.
- venv Python 3.12(노트북은 3.14). `db/paper.db`·`db/toss_candles.db`·`db/trading.db`는 gitignore.

**절대규칙**: paper.db 기록은 이 미니PC 1대에서만(노트북/PC 기록 금지, 원장 분리 방지).
성과는 벤치마크 대비 초과수익으로만 판단. 관련: [[trading-bot-operating-charter]],
[[nxt-premarket-historical-data]], [[data-accumulation-machine]].

**운영 이슈(2026-07-10 기준)**:
- 상주 프로세스 **무음 사망 3회 재발**(로그 에러 없이 끊김, 재부팅 무관) — 세션마다
  3프로세스(webapp/main_tracker/paper loop) 생존 확인 습관 필수. 워치독 도입 제안 상태.
- **픽 7일 만료**(sector_picks.expires_at): 매주 재등록 필요. 만료되면 유니버스가
  조용히 쪼그라든다(07-10 실측: 59→8종목).
- KIS 분봉(inquire-time-itemchartprice, UN)은 간헐 500 — 실패분은 반복 재시도로
  회수되나 완고한 잔여는 토스 백필(1m 4년 소급 가능)로 커버 가능.
