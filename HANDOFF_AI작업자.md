# HANDOFF — AI 작업자 인수인계 (기준 본문: 2026-07-20, 최신 상태는 아래 갱신 블록)

## 최우선 최신 갱신 — 2026-07-22

- 최신 상세 인수인계는 `HANDOFF_트레이딩봇_담당자.txt`와
  `PROJECT_HANDOFF.md` 마지막 3~4개 섹션을 함께 읽는다.
- ETF 웹앱은 숫자 코드뿐 아니라 `0167A0` 같은 영문 혼합 KRX 코드도 지원한다.
  StockMaster 캐시는 v3이며, KIS 실측 ETF 1,143개(영문 혼합 279개)를 포함한다.
- 최신 전체 테스트 기준은 **397 passed, 기존 warning 1개**다.
- 미니PC 배포 전후에는 운영 `trading.db`를 보존하고 웹앱 uvicorn만 장 마감 후
  WMI로 재기동한다. tracker/paper_runner를 코드 반영 목적으로 끊지 않는다.

### 오너 표준 호출 문구

오너가 **“미니PC에서 작업끝났으니 깃pull 해”**라고 말하면 다음을 한 작업으로
해석하고, 중간 단계를 생략하지 않는다.

1. 미니PC인지 확인하고 `C:\trading-bot`에서 `git fetch origin`, `git status` 실행.
2. 로컬 변경/충돌 여부를 먼저 보고한 뒤 안전하면 `git pull --ff-only origin main`.
3. 이 문서, `HANDOFF_트레이딩봇_담당자.txt`, `PROJECT_HANDOFF.md` 최신 섹션을
   다시 읽어 pull된 변경의 목적·배포 절차·기대 테스트 수를 파악.
4. `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'` 후
   `.venv\Scripts\python.exe -m pytest tests\ -q` 실행.
5. 상주 3프로세스와 `--report`를 실측. 장중에는 코드 반영 재시작 금지.
6. 문서에 배포 대기 작업이 있으면 장 마감 후 해당 프로세스만 WMI로 재기동.
   웹앱 변경은 uvicorn만 재기동하며 Start-Process는 절대 사용하지 않음.
7. 문서에 적힌 스모크 테스트를 수행하고 실제 출력으로 완료 보고.

문구가 짧아도 단순 pull만 하고 끝내라는 뜻이 아니다. pull된 인수인계 전체를 읽고
필요한 배포·검증까지 완료하라는 의미다. 장중이라 재기동할 수 없으면 pull/테스트와
현황 확인까지만 하고, 재기동 보류 이유와 장 마감 후 남은 절차를 명확히 보고한다.

> 이 문서 하나로 새 AI 작업자가 프로젝트를 이해하고 안전하게 작업을 시작할 수
> 있게 쓴 인수인계다. 전부 실측·코드 근거로 기록했으며, 이후 세션은 이 문서와
> `PROJECT_HANDOFF.md`(시간순 로그) 최신 섹션을 함께 보면 된다.
> **요약을 맹신하지 말 것 — 착수 전 실제 코드·git·실행결과로 재확인이 이 팀의 표준이다.**

---

## 1. 프로젝트가 뭔가 (북극성)

- 투자 멘토("스승님/굿머닝") 매매철학을 코드로 기계화한 KOSPI/KOSDAQ 자동매매
  봇. 임무는 판단 대체가 아니라 **"실행 규율의 기계화"** — 오너는 종목선정 감각이
  있으나 진입/손절/청산에서 감정매매가 반복되는 걸 기계로 막는 것.
- 백테스트로는 알파 미입증 → **forward(모의투자)만이 답**. "가장 비싼 자원은
  코드가 아니라 forward 시간." 모든 작업은 "모의투자 가동을 앞당기나/방해 안
  하나"로 판정한다.
- **최종 관문**: paper_start(2026-07-06)부터 최소 20거래일(~8월 초) 후 벤치마크
  대비 초과수익 판정. "알파 없음"도 유효한 결론(폐기/수정 근거).
- 운영 모델: **매일 오너/동료가 미니PC 웹앱에 픽 등록 → 그날 바로 라이브
  유니버스 반영 → 전략들이 병행 기록 → 벤치 대비 비교.**

## 2. 시스템 현황 (전부 실측 확인된 상태)

### 미니PC = 모의투자 지정 기기, 상주 3프로세스
| 프로세스 | 역할 |
|---|---|
| `main_tracker.py` | 매일 16:00 Phase2.5 수집 파이프라인 (분봉 적립, 페이퍼 호출 없음) |
| `python -m strategy.paper_runner --market-schedule` | 페이퍼 상주 (KST 타임테이블: 08~16시 5분 / 16~18시 10분 / 저녁 30분 / 23~06시 중단) |
| `uvicorn webapp.server:app --host 0.0.0.0 --port 8000` | 종목 등록 웹 (Tailscale 동료 공유) |

- ⚠️ **상주 기동/재시작 규칙(절대)**: AI 도구에서 `Start-Process` 로 띄우면
  Claude 앱 프로세스 트리에 묶여 **앱 업데이트/종료 때 같이 죽는다**(무음 사망
  4회의 확정 원인, 이벤트로그 실측). 반드시 WMI 로:
  `Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments
  @{CommandLine='...python.exe ...'; CurrentDirectory='C:\trading-bot'}`
  재부팅 커버는 시작프로그램 VBS(`trading-bot-paper.vbs`, 3프로세스 기동).
- 세션 시작 습관: **3프로세스 생존 확인**(`Get-CimInstance Win32_Process` 로
  trading-bot python 필터). 죽어 있으면 WMI 재기동 — paper 는 리플레이 멱등이라
  기록은 복구된다(단 장중 라이브 관측은 유실).

### 페이퍼 전략 축 (paper.db, 8축 병행)
`v2`(당일치기) / `v2_leader`(주도섹터만 — "주도주만 거래" 컨셉의 본체) /
`gm_v3` + `gm_v3_r13/r14/r13r14`(멘토룰 R1~R16, GM3_VARIANTS) /
`v4r`(재폭등 관찰 축, 채택 아님) / `bench_bh`(당일 유니버스 동일가중 무비용).
- 장중 기록은 임시(finalized=0), **20:05 이후 확정(finalized=1)** + 텔레그램
  요약 자동 발송(@zzapmoneying_bot, 전 축 + 오늘 매매 상세 + 건수·평균·승률).
- 성과 보고 규칙: 절대손익 단독 금지 — "시드 x% (시장보다 y%p …)" 병기,
  v2 계열은 복리 누적 대신 건별 통계(직렬복리 착시 방지).
- record_upto: 결측 거래일 자동 소급 + 미확정 재확정. 데이터 0건이면 기록 스킵.

### 웹 공유 (배포 완료)
- 동료가 Tailscale(`100.100.141.24:8000`)로 접속, 변경 API 는 `X-Web-Key`
  (.env `WEB_SHARED_KEY`) 인증, 등록자 `[web:이름]` 스탬프. 웹픽 유효 1년.
- **픽 등록은 반드시 미니PC 웹앱** — trading.db 는 gitignore(기기 로컬)라 타
  기기 등록분은 안 넘어온다. 방화벽은 Tailscale 인터페이스+100.64.0.0/10 한정.

## 3. 전략 지식 요약 (백테스트/실측 판정 — PROJECT_HANDOFF 해당 섹션 근거)

- **v2**: 프리장+5% → 눌림 → 지지·다지기 3봉 → 아침고점 재돌파. 유일하게 양
  기간(1~3월 +27.6% / 4~6월 +63.0%, 75종목) 플러스. 현 주력 비교 대상.
- **v3/구 v4(선발대)**: OOS 탈락/순비용 — 코드 보존(기록 재현용), 신규 작업 금지.
- **v4r(v4재폭등)**: v2 + 국소 스윙 기준선(파동 추적) + 재진입≤4 + 승자 게이트
  + 오버나이트 무기한. A/B 판정: **레짐 도박**(Q1 -55.7% / Q2 +441.9%,
  애프터 진입·재진입은 기각) → 기본형 채택 불가. 현재 **애프터 제외 정제형이
  페이퍼 관찰 축**으로 forward 기록 중(7/6~7/20 소급: 47건 · 평균 -1.34% ·
  승률 32% · -50.8% — Q1 패턴 재현 중). 후속 방향: **레짐 필터(시장 추세
  게이트) 결합** — 오버나이트 엣지(Q2 154건 평균 +2.62%)가 유일한 진짜 엣지.
- **gm_v3**: 멘토룰 R1~R12 + Tier1 확장 R13~R16(기본 OFF, `docs/gm_v3_tier1_spec.md`).
  R13(지지레벨 매수)이 forward 에서 하락장에 더 물리는 중 — 변형 축 데이터가 판정.
- **수급(오너 결정 07-14)**: KIS 일별 확정 수급만 사용(`data/flow_data.py` v3
  게이트). 장중 종목 수급은 추정치·5구간·백필 불가로 **수집하지 않기로 확정**.
- **데이터 소스 실측**: 토스 1분봉 ~4년 / 일봉 35년+ 온디맨드 백필 가능(KIS
  "당일만" 제약은 토스엔 없음). KIS 일별 수급은 최근 30영업일 고정.
- **실행(라이브 주문) 엔진**: `main.py` 는 구식 전략(evaluate_buy/ATR·VWAP·MACD)
  스택 — **현 전략과 무관, 켜지 말 것**. 알파 실증 후 v2_leader/gm_v3 용 실행
  엔진 신규 배선이 로드맵(그때 main.py 교체).

## 4. 절대 규칙 (운영 헌장 — 위반 금지)

1. **실전 주문 경로 활성화 금지.** 오너 명시 승인 전까지 페이퍼까지만.
2. **성과는 벤치마크 대비 초과수익으로만 판단.** 절대손익 단독 보고 금지.
3. **자기 소유 DB에만 쓰기** (paper.db / toss_candles.db / trading.db).
   ai-moneyingbot 코퍼스(mentor.db)는 읽기 전용.
4. **paper.db 기록은 미니PC 1대에서만.** 노트북/PC에서 기록 실행 금지.
5. **커밋/푸시는 오너 승인 후.** 검증 출력은 액면 그대로 먼저 읽기. 추측은
   반드시 "추측"으로 명시. Co-Authored-By 넣지 말 것.

## 5. 작업 규칙 (프로세스)

- 세션 시작: `git fetch origin && git status` → behind 면 pull 먼저.
- 비자명 변경: **별도 브랜치 → 커밋 전 독립 리뷰(/code-review 또는 리뷰
  서브에이전트) → 수정 → 커밋 → 오너 승인 → main FF 머지 → push.**
  force push 금지, main 직접 커밋 금지(문서 전용은 오너가 관례 승인해 옴).
- pytest: `$env:PYTHONUTF8="1"` + **`tests\` 디렉토리만**(루트 test_*.py 는
  requests 없어 수집 깨짐). 현재 383 passed 유지가 기준.
- 실행: `.venv\Scripts\python.exe`(미니PC=3.12, 노트북=3.14),
  출력 스크립트는 `PYTHONIOENCODING=utf-8`.
- **장중(08~16시) 도는 상주를 끊는 재시작 금지** — 죽은 걸 살리는 복구는 OK.
  코드 반영 재시작은 장 마감 후 + 반드시 WMI(§2).
- 마디마다 `PROJECT_HANDOFF.md` + `.claude-memory/` 갱신 → commit+push
  ("pull만 하면 최신").
- 보고: 착수 전 무엇/왜 → 완료 후 실행 명령+실제 출력 첨부 → 선택지는 번호로.

## 6. 함정 목록 (전부 실제로 겪은 것)

- `data/daily_data.py` 등 4파일의 M 표시는 **LF/CRLF EOL 노이즈** —
  커밋에 절대 포함하지 말 것 (`git diff --numstat` 비면 노이즈).
- PowerShell: ① 커밋 메시지에 큰따옴표 있으면 히어스트링이 깨진다 — **메시지는
  파일로 쓰고 `git commit -F`** ② `git checkout` 등의 실패를 `| Out-Null` 로
  삼키지 말 것(체크아웃 실패 후 엉뚱한 브랜치에서 작업한 사고 있음) ③ worktree
  만들면 그 브랜치는 checkout 잠김 — 쓰고 나면 `git worktree remove`(정션은
  rmdir 로 링크만 먼저 제거).
- **캐시 고착 버그(코드 미수정, 백로그)**: 장중 수집이 죽었다가 20:05 이후
  재기동하면 부분 캐시를 완전으로 오인해 잘린 데이터로 확정할 수 있음.
  복구(멱등): 당일 candles/fetched 삭제 → 재수집 → paper_notified 당일 키
  삭제 → record_day 재실행. (minipc-paper-deploy.md 메모리 참조)
- **KRX 달력 ≠ 실거래일**: 2026-07-17 처럼 pandas 달력엔 거래일인데 실데이터
  0봉인 날 존재 — record_day 의 "데이터 0건 스킵"이 보호하며, 기록 없음이 정상.
- KIS 분봉(inquire-time-itemchartprice)은 간헐 500 — 반복 재시도로 회수,
  잔여는 토스 백필 가능.
- paper_trades PK 는 (strategy, code, opened_on, closed_on) — 당일 다회
  트레이드 축은 opened_on 에 **진입 봉 ISO 시각**을 넣어야 함(v4r 이 그 방식).
- 텔레그램: paper 발송은 `strategy/paper_notify.py`(httpx 직접 POST, 폴링
  없음). mark-before-send(at-most-once) + `paper_notified` dedup.

## 7. 다음 작업 후보 (오너와 우선순위 확인 후 착수)

1. (상시) forward 관측 — `--report`, 알림, 3프로세스 생존.
2. **v4r 레짐 필터 연구** — 시장 추세 게이트로 Q1형 손실 차단 + 오버나이트
   엣지 보존 (v4r 관찰 축 데이터가 근거로 쌓이는 중).
3. 캐시 고착 버그 근본 수정(마지막 봉 <15:30 이면 강제 재수집) — 백로그.
4. 워치독(3프로세스 감시·재기동·알림) — 원인 해결(WMI)로 긴급도 낮아졌으나
   보조 안전망 후보.
5. gm_v3 일봉을 토스 네이티브 1d 로 전환 검토(KIS 워밍업/합성패딩 제거).
6. 20거래일 후(~8월 초) 1차 알파 판정 → 이후 실행엔진 설계.

## 8. 시작 절차 (복붙)

```powershell
cd C:\trading-bot
git fetch origin; git status          # behind면 git pull origin main 먼저
# 상주 생존 확인 (미니PC)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*trading-bot*' } |
  Select-Object ProcessId, CommandLine
# 페이퍼 현황
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m strategy.paper_runner --report
```
읽을 문서 순서: 이 문서 → `CLAUDE.md` → `PROJECT_HANDOFF.md` 최신 3~4개 섹션
→ `.claude-memory/MEMORY.md` 인덱스(특히 minipc-paper-deploy, operating-charter)
→ (작업 영역별) `docs/gm_v3_tier1_spec.md`, `HANDOFF_웹공유.md`.
