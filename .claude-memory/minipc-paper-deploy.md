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

**운영 이슈**:
- **[원인 확정 2026-07-14] 상주 무음 사망 4회의 범인 = Claude 앱 프로세스 트리.**
  AI 세션이 Start-Process 로 띄운 상주는 Claude 앱의 Job 트리에 묶여, 앱 자동
  업데이트(이벤트 7045 "Claude 서비스 설치" — 07-14 09:53, 07-10 06:22 실측 일치)나
  앱 종료 시 통째로 정리된다. python 크래시(WER)/절전/재부팅 이벤트 0건,
  타 부모의 아카이브봇은 생존 — 코드 문제 아님.
  **→ 규칙: 상주 기동/재시작은 반드시 Claude 트리 밖에서.**
  ① 표준: `Invoke-CimMethod -ClassName Win32_Process -MethodName Create
  -Arguments @{CommandLine='...python.exe ...'; CurrentDirectory='C:\trading-bot'}`
  (부모=WmiPrvSE, 즉시 분리) ② 재부팅 커버: 시작프로그램 VBS.
  **Start-Process 로 상주를 띄우지 말 것** — 다음 Claude 업데이트 때 또 죽는다.
- **픽 7일 만료**(sector_picks.expires_at): 매주 재등록 필요. 만료되면 유니버스가
  조용히 쪼그라든다(07-10 실측: 59→8종목).
- KIS 분봉(inquire-time-itemchartprice, UN)은 간헐 500 — 실패분은 반복 재시도로
  회수되나 완고한 잔여는 토스 백필(1m 4년 소급 가능)로 커버 가능.
- **[2026-07-15] Windows Update 자동 재부팅 사고 + 이중 방어 조치.**
  09:16 TrustedInstaller가 "운영 체제 업그레이드"로 자동 재부팅 → Startup VBS는
  로그온 시에만 실행되므로 22:35 오너 로그인까지 13시간 상주 3종 전체 다운.
  조치: ① WU 정책 적용(관리자 UAC 승인) — `HKLM\SOFTWARE\Policies\Microsoft\
  Windows\WindowsUpdate\AU` NoAutoUpdate=1 + NoAutoRebootWithLoggedOnUsers=1,
  사용시간 08:00~02:00. **업데이트는 이제 수동 전용 — 주말에 수동 설치 권장.**
  ② 잔여 리스크: 재부팅(정전 등) 후 로그온 전까지는 여전히 봇 다운 — 자동
  로그온 또는 관리자 예약작업(AtStartup) 승격은 오너 결정 대기.
- **[2026-07-15 발견] 20:05 이후 재시작 시 당일 토스 캐시 고착 버그(코드 미수정).**
  장중 수집이 죽고(09:11 컷) 20:05 이후 재기동하면 `ensure_day_cached`가
  부분 데이터를 '완전 수집'으로 오인 → record_day가 잘린 데이터로 finalized=1
  확정 + 오알림 발송(07-15 실측: bench +4.67%로 확정, 실제 +8.53%).
  복구 절차(멱등): 당일 candles/fetched 삭제 → ensure_day_cached 재수집 →
  paper_notified 당일 키 삭제 → record_day 재실행(정정 알림 자동 발송).
  근본 수정(마지막 봉 <15:30이면 강제 재수집)은 백로그.
