# HANDOFF — AI 작업자 인수인계 (기준 본문: 2026-07-20, 최신 상태는 아래 갱신 블록)

## 최우선 최신 갱신 — 2026-08-05 Mentor Signal Paper Ingest

- 작업 브랜치 `feature/mentor-signal-ingest`에 `POST /api/signals/mentor`와
  `mentor_signal_events` 감사/멱등 테이블을 추가했다.
- 수신은 X-Web-Key, paper 모드, KIS_ENV=PAPER, 작성자, ADD_WATCH, 신뢰도,
  StockMaster 이름/코드를 모두 재검증한 뒤 기존 SectorStore 관심종목 경로만 호출한다.
- Paper Runner는 기존 `load_universe` 주기에서 자동등록 종목을 동적으로 읽는다.
  실전 주문 코드와 `main.py`는 수정하거나 호출하지 않았다.
- 상세 계약은 `docs/mentor_signal_ingest.md`. 미니PC 배포/웹앱 재기동/paper 전환은
  아직 수행하지 않았다.

## 최우선 최신 갱신 — 2026-08-04

- 미니PC는 `c56d496` 배포가 완료됐다. 2026-08-04 12:02 KST 최종 실측에서
  web `5480/4396`, paper `13308/16320`, tracker `8020/11620`이 생존했고,
  8000 포트와 `0167A0 → SOL AI반도체TOP2플러스 (etf)` 검색이 정상이다.
- 운영 `paper.db`에는 2026-08-04 기준 `v2_portfolio`, `v2_qv`,
  `v2_qv_portfolio`가 각각 1행 생성됐다. 현재 장중 임시값(`finalized=0`)이며
  `--report` rc=0과 세 축 노출을 확인했다. 20:05 이후 확정값으로 평가한다.
- 운영/백업 DB `quick_check=ok`; `universe_membership_events`는 table 1개,
  trigger 11개, bootstrap/전체 76행이다. 웹 유니버스는 10 picks, 77 rows,
  76 distinct codes로 보존됐다.
- 2026-08-03 예약 배포는 Windows PowerShell 5.1의 두 함정 때문에 중간 실패했다:
  charset 없는 JSON을 `Invoke-RestMethod`로 읽으면 UTF-8 한글이 깨질 수 있고,
  다중행 Python을 native `python -c` 인수로 직접 넘기면 quoting이 깨질 수 있다.
  배포 검증기는 raw bytes를 UTF-8로 디코딩하고, Python 코드는 UTF-8 Base64 후
  짧은 ASCII launcher에서 decode/exec한다. PowerShell `ConvertFrom-Json` 배열은
  `System.Object[]` 중첩 여부를 확인하고 DB 직접 조회로 최종 판정한다.
- 오너가 2026-08-04 이번 1회에 한해 장중 코드 반영 재기동 예외를 승인했다.
  예외는 소진됐으며 이후에는 다시 08~16시 재기동 금지와 WMI 기동 규칙을 지킨다.
- 미니PC의 미추적 `AGENTS.md`는 SHA256
  `6A87C43BA943EE165CE608C1FD683D6DC2B6B0A7C94D2EAEFEA7396C6F48A9CC`로 보존됐다.
  배포 스크립트·백업·로그는 `db/backups/deploy-20260803-0958/`에 남아 있으며
  Git 추적 파일이 아니다. tracker·실주문·시크릿은 변경하지 않았다.

## 최우선 최신 갱신 — 2026-08-02

- 노트북은 `git pull --ff-only origin main` 후 이 문서 전체와
  `HANDOFF_노트북_작업자.md`, `PROJECT_HANDOFF.md` 마지막 3~4개 섹션을 읽는다.
  별도 복붙 프롬프트는 필요 없다. 이 문서를 포함한 최신 커밋은 `git log -1`로
  확인하고, 로컬 미추적 파일을 임의로 삭제·이동·추적하지 않는다.
- 직전 origin/main 기능 기준은 `3f5cf21 feat: improve paper reporting and v2
  quality research`다. 여기에는 텔레그램 페이퍼 요약 개편과 `v2_qv` 연구·관찰축이
  포함된다.
- 이번 인수인계에는 연구 전용 `gm_regime_v1` 계산 계층·백테스트·테스트·연구 문서를
  포함한다. 이는 `TopDownGate`의 장세에 따라 기존 `v2_qv` 공유현금 노출을
  100%/50%/20%로 줄여 보는 overlay이며, **paper runner·텔레그램·주문에는 연결하지
  않았다.** 기존 전략 동작도 바꾸지 않는다.
- `gm_regime_v1`은 새 알파 모델로 **NO-GO**다. 섹터 `pick_date` 근사 경계를 적용한
  2026-06-28~07-10 실행은 분봉 표본 부족으로 거래 0건이라 판정 불가였다.
  50종목 고정 소급 탐색은 +3.21%/MDD -2.84%였지만 피크노출×일봉 proxy와의
  단순 격차가 -37.64%p였다. 이 proxy는 동일노출 벤치나 초과수익이 아니며,
  선택·생존편향까지 있으므로 성과 근거로 사용하지 않는다.
- 월별 재분석에서 기존 전략군은 약세 방어에 비해 1·2·4·5월 상승 참여가 매우
  낮았다. 다음 연구 후보는 상승장 전용 `gm_trend_v1`이지만 아직 구현 지시나 승격
  승인이 아니다. 노트북 작업자는 현재 Git 상태를 보고한 뒤 오너 지시를 기다린다.
- 2026-08-02 로컬 실측: 신규 테스트 16 passed, 전체 `tests\` **463 passed**,
  기존 `pandas_market_calendars` warning 1건, `git diff --check` 정상. 재현 명령과
  한계는 `docs/research/gm_regime_v1.md`에 있다.
- `tmp/`의 PDF 렌더·추출 산출물은 로컬 미추적 자료다. 인수인계 커밋에 넣지 않으며,
  현재 PC에서도 삭제하지 않는다. 노트북에는 Git 추적 코드·문서만 전달된다.
- 이번 인수인계는 로컬 코드·문서·테스트만 다룬다. 미니PC DB·파일·상주 프로세스·
  예약 작업·주문은 변경하지 않았으며, 미니PC의 실제 배포 상태는 필요할 때 승인
  게이트가 있는 공용 브리지로 다시 실측한다.

## 이전 갱신 — 2026-07-27

- 2026-07-27 변경은 텔레그램 페이퍼 요약 전체거래/분할발송/고정폭 표,
  top-down 연구 코드, `v2_qv`/`v2_qv_portfolio` 관찰축을 포함한다.
- `v2_qv`는 눌림 거래량 `<=0.8x`와 돌파 거래량 `>=1.5x`를 모두 요구한다.
  현재 76종목 회고 테스트의 공유현금 NAV는 약 6.5개월 `+5.51%`, MDD
  `-4.57%`다. 직렬복리 `+50.5%`를 실계좌 수익으로 보고하지 않는다.
- 기존 v2는 유지한다. v2에 시장 게이트·R2 강제를 적용하지 않는다. 복합 품질점수와
  프리장 상승률 상위 선별은 성과 악화로 폐기했다.
- 신규 qv 축은 검증 전 텔레그램에서 숨기며, 배포 후 8~12주 forward로만 승격을
  판단한다. 상세 근거는 `PROJECT_HANDOFF.md` 최신 섹션을 본다.
- 정식 테스트 기준은 최종 push 전 `pytest tests\ -q` 실측값을 사용한다.
  미니PC에는 아직 미배포 상태다.

- 노트북 작업자는 이 문서 전체를 읽은 다음 `HANDOFF_노트북_작업자.md`를 읽고
  역할 경계와 시작 절차를 적용한다. 별도 프롬프트 없이 이 두 Markdown 문서가
  노트북 작업의 단일 인수인계 진입점이다.
- 2026-07-27 push 전 origin/main 기준은 `6fd344b`다. 이 문서를 포함한 최신
  커밋은 `git log -1`로 확인하며 전체 테스트 실측값을 기준으로 삼는다.
- P0 forward 측정 신뢰도 보강으로 실제 편입 이벤트 기반
  `gm_v3_joined`/`v4r_joined`/`bench_joined`와 공유현금
  `v2_portfolio`/`v2_leader_portfolio` 관찰축이 추가됐다.
- 2026-07-24 미니PC는 `540949a` pull과 전체 테스트까지 완료했으나 당시
  장중이라 웹앱·paper runner 재기동은 보류했다. 현재 배포 상태는 다시 실측한다.
- 노트북은 개발 기기다. `paper.db` 기록, 운영 `trading.db` 쓰기, 주문 및 상주
  프로세스 변경을 하지 않는다. 미니PC 작업은 승인 게이트가 있는 공용 브리지로
  담당 Codex에 직접 지시한다.
- 2026-07-26 SSH 브리지는 `미니PC@100.100.141.24`로 성공했다. `jason@...`은 실패하므로
  브리지 호출 시 필요하면 `-SshUser "미니PC"`를 명시한다. 세부 실측과 브리지 CLI
  파싱 주의점은 `PROJECT_HANDOFF.md` 마지막 섹션과 `HANDOFF_노트북_작업자.md` §5~6을 본다.

- 최신 상세 인수인계는 `HANDOFF_트레이딩봇_담당자.txt`와
  `PROJECT_HANDOFF.md` 마지막 3~4개 섹션을 함께 읽는다.
- ETF 웹앱은 숫자 코드뿐 아니라 `0167A0` 같은 영문 혼합 KRX 코드도 지원한다.
  StockMaster 캐시는 v3이며, KIS 실측 ETF 1,143개(영문 혼합 279개)를 포함한다.
- 웹앱·paper 변경 배포 전후에는 운영 `trading.db`를 보존한다. 필요한 프로세스만
  장 마감 후 WMI로 재기동하며, 변경과 무관한 tracker·주문 프로세스는 끊지 않는다.

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

### 페이퍼 전략 축 (paper.db, 기존축 + 신규 측정 관찰축)
`v2`(당일치기) / `v2_leader`(주도섹터만 — "주도주만 거래" 컨셉의 본체) /
`gm_v3` + `gm_v3_r13/r14/r13r14`(멘토룰 R1~R16, GM3_VARIANTS) /
`v4r`(재폭등 관찰 축, 채택 아님) / `bench_bh`(당일 유니버스 동일가중 무비용) /
`gm_v3_joined`·`v4r_joined`·`bench_joined`(실제 편입경계) /
`v2_portfolio`·`v2_leader_portfolio`·`v2_qv_portfolio`·
`bench_v2_portfolio`(공유현금 측정).
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
  requests 없어 수집 깨짐). 2026-07-27 기준은 447 passed, 기존 warning 1건이다.
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
2. **v2_qv forward 관측** — 기존 v2와 공유현금 NAV·실측 슬리피지 비교.
3. **v4r 레짐 필터 연구** — 시장 추세 게이트로 Q1형 손실 차단 + 오버나이트
   엣지 보존 (아직 paper 미연결).
4. 캐시 고착 버그 근본 수정(마지막 봉 <15:30 이면 강제 재수집) — 백로그.
5. 워치독(3프로세스 감시·재기동·알림) — 원인 해결(WMI)로 긴급도 낮아졌으나
   보조 안전망 후보.
6. gm_v3 일봉을 토스 네이티브 1d 로 전환 검토(KIS 워밍업/합성패딩 제거).
7. 20거래일 후(~8월 초) 1차 알파 판정 → 이후 실행엔진 설계.

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
