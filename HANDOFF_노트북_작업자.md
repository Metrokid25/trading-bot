# 노트북 AI 작업자 인수인계

## 2026-08-07 Mentor Signal Paper 연동 이어하기

- 원격 `main`에는 Mentor Signal Paper ingest와 최신 Trading main이 통합돼 있다.
  기능 기준점은 `2f8ced4`이며 실제 시작점은 이 문서를 포함한 최신 `origin/main`이다.
- 검증 기준은 Trading `486 passed, 1 skipped`, Reader `823 passed`, Fixture E2E 통과다.
  멘토 자동픽은 `[mentor:` provenance로 수동 픽과 격리되고 등록 당일 Paper replay 및
  REAL scanner에서 제외된다.
- Codex 자동화 `pc-16`이 2026-08-07 16:10 KST에 미니PC 배포를 한 번 실행한다.
  웹앱은 필요 시 WMI로만 재기동하며 tracker·주문 프로세스는 건드리지 않는다.
- Reader는 Shadow로만 시작한다. Paper 자동등록과 실전 주문은 활성화하지 않는다.
- 출근 후 아래 §2 절차로 clean/pull한 다음 `HANDOFF_AI작업자.md` 최상단과 이 작업의
  예약 실행 결과를 확인한다. 배포 결과가 없거나 실패했으면 중복 재기동하지 말고 보고한다.
- 다음 작업은 Shadow 판독/지연/감사행을 읽기 전용으로 분석하고 테스트를 보강하는 것이다.
  미니PC 운영 DB, `.env`, 상주 프로세스는 노트북에서 변경하지 않는다.

기준일: 2026-08-07

작업 경로: `C:\trading-bot`

기준 브랜치/커밋: `main` / 이 문서를 포함한 최신 `origin/main`
(직전 기능 기준 `3f5cf21`)

> 이 문서는 별도 시작 프롬프트 없이 작동하는 노트북 인수인계 본문이다.
> `HANDOFF_AI작업자.md`를 먼저 읽은 작업자는 이 문서를 끝까지 읽고 아래 시작
> 절차를 실제로 수행한 뒤, 현재 상태를 오너에게 보고하고 다음 지시를 기다린다.

## 1. 역할

노트북은 trading-bot의 개발·백테스트·코드리뷰 기기다. 오너는 코딩하지 않고
방향과 승인만 결정한다. 실제 운영 원장은 미니PC에 있으며 다음 항목은 노트북에서
실행하거나 변경하지 않는다.

- `paper.db` 기록 및 `strategy.paper_runner`의 기록 모드
- `trading.db`를 운영 원장으로 간주하는 쓰기 작업
- 실전·모의 주문
- 미니PC 상주 프로세스와 예약 작업의 직접 변경
- `.env`, API 키, 웹 공유 키 출력 또는 Git 추적

노트북에서 허용되는 기본 작업은 코드·문서 수정, 테스트, 백테스트, 정적 분석,
독립 코드리뷰다.

## 2. 세션 시작 절차

순서대로 실행하고 실제 출력으로 상태를 보고한다.

```powershell
cd C:\trading-bot
git fetch origin
git status --short --branch
git rev-list --left-right --count HEAD...origin/main
```

1. tracked 로컬 변경이 없고 behind이면 `git pull --ff-only origin main`.
2. 미추적 파일은 소유자를 확인하기 전 삭제·이동·추적하지 않는다.
3. `HANDOFF_AI작업자.md` 전체를 읽는다.
4. `PROJECT_HANDOFF.md` 최신 3~4개 섹션을 읽는다.
5. 이 문서와 작업 영역 관련 문서를 읽는다.
6. 현재 브랜치·HEAD·ahead/behind·로컬 변경을 먼저 보고한다.
7. 임의 작업을 시작하지 말고 오너의 다음 지시를 기다린다.

## 3. 개발 절차

- 비자명 변경은 `main`이 아닌 별도 브랜치에서 한다.
- 착수 전에 무엇을 왜 바꾸는지 보고한다.
- 요약이나 과거 메모리보다 실제 코드·Git·테스트 결과를 우선한다.
- 테스트는 저장소 루트가 아니라 `tests\`만 실행한다.

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m pytest tests\ -q
git diff --check
```

- 2026-08-02 테스트 기준은 `463 passed`, 기존
  `pandas_market_calendars` 경고 1건이다.
- 비자명 변경은 커밋 전에 독립 리뷰어가 전체 diff와 실제 결함을 검토해야 한다.
- 커밋·`main` fast-forward·push는 오너 승인 후에만 한다.
- force push, `git reset --hard`, 사용자 변경 폐기는 금지한다.
- 완료 시 `PROJECT_HANDOFF.md`와 필요한 인수인계 문서를 함께 갱신한다.

## 4. 현재 구현 상태

`3f5cf21 feat: improve paper reporting and v2 quality research`까지 origin/main에
반영됐고, 이 인수인계 커밋에는 아래 `gm_regime_v1` 연구 파일도 포함된다.

- append-only 실제 편입·이탈 이벤트
- `gm_v3_joined`, `v4r_joined`, `bench_joined`
- 시간순 공유현금 `v2_portfolio`, `v2_leader_portfolio`
- 종목당 20%, 동시 5종목, 정규화 섹터당 최대 2종목
- `paper_portfolio_allocations` 감사 테이블
- 신규 관찰축의 기존 텔레그램 알림 격리
- `v2_qv`/`v2_qv_portfolio`: 눌림 거래량 `<=0.8x` + 돌파 거래량 `>=1.5x`.
  현재 76종목 공유현금 회고 NAV `+5.51%`, MDD `-4.57%`; 직렬복리
  `+50.5%`를 실계좌 성과로 해석하지 않는다.
- top-down 연구 코드는 있으나 GM/R13/v4r paper 축에는 아직 연결하지 않았다.
  기존 v2에는 시장 게이트를 적용하지 않는다.
- `strategy/gm_regime_v1.py`, `backtest/run_gm_regime_v1.py`,
  `tests/test_gm_regime_v1.py`, `docs/research/gm_regime_v1.md`는 연구 전용이다.
  기존 `v2_qv` 공유현금 결과에 장세별 100%/50%/20% 노출 배수를 적용하지만
  paper runner·텔레그램·주문에는 연결하지 않았다.
- `gm_regime_v1`은 새 알파로 NO-GO다. 섹터 `pick_date` 근사 표본은 거래 0건이라
  판정 불가이고, 고정 유니버스 소급 탐색은 선택·생존편향이 있다. 출력하는
  피크노출×일봉 비교도 동일노출 벤치나 초과수익이 아니다. 숫자와 다음 검증 조건은
  연구 문서를 따른다.
- 월별 진단상 현재 전략군에는 강한 상승 추세를 오래 보유하는 수익 엔진이 부족하다.
  다음 후보는 `gm_trend_v1`이지만 아직 구현 승인이 아니므로 오너 지시를 기다린다.

이 축들은 forward 측정 신뢰도를 높이기 위한 **관찰축**이다. 기존 v2 직렬복리를
실계좌 수익률로 해석하지 말고, 신규 벤치도 아직 동일 gross exposure 벤치는
아니라는 한계를 항상 병기한다.

## 5. 미니PC 협업

오너에게 명령 실행이나 결과 복사·붙여넣기를 요구하지 않는다. 메인 PC/노트북에서
다음 공용 브리지로 미니PC trading 담당 Codex에 직접 지시한다.

```powershell
& "$env:USERPROFILE\.codex\scripts\invoke-mini-bot-codex.ps1" `
  -Bot trading -Prompt "<읽기 전용 확인 지시>"
```

2026-07-26 실측 기준으로 SSH 성공 계정은 `미니PC@100.100.141.24`다.
`jason@100.100.141.24`는 같은 키로 거절되므로, 브리지 기본 설정이 `jason`이면
다음처럼 `-SshUser "미니PC"`를 명시한다.

```powershell
& "$env:USERPROFILE\.codex\scripts\invoke-mini-bot-codex.ps1" `
  -Bot trading -SshUser "미니PC" -Prompt "<읽기 전용 확인 지시>"
```

기본 호출은 읽기 전용이다. pull, 파일/DB 쓰기, 테스트 중 상태 변경, 재기동 등은
오너가 정확한 범위를 승인했을 때만 다음 형식을 사용한다.

```powershell
& "$env:USERPROFILE\.codex\scripts\invoke-mini-bot-codex.ps1" `
  -Bot trading -Prompt "<승인 범위 안의 구체적 지시>" `
  -AllowMutation -ApprovalNote "<오너가 승인한 정확한 범위>"
```

절대 규칙:

- trading-bot만 소유한다. Archive/RAG 저장소·DB·Qdrant를 건드리지 않는다.
- 미니PC의 미추적 `C:\trading-bot\AGENTS.md`를 보존하고 먼저 읽는다.
- 상주 프로세스는 반드시 WMI로 기동한다. `Start-Process` 금지.
- 장중 08:00~16:00에는 코드 반영 목적 재기동 금지.
- 주문, 예약 작업, 시크릿, DB 쓰기는 각각 명시적 승인 없이는 금지.
- 원격 출력과 rc를 그대로 보고하고, 연결 실패 시 오너에게 수동 복붙을 요구하지 않는다.
- 2026-07-26 현재 브리지 스크립트가 원격 `codex` CLI 인자를 한 덩어리로 전달해
  실패할 수 있다. 이 경우 같은 SSH 계정으로 `powershell -EncodedCommand`를 보내
  읽기 전용 명령만 직접 실측하고, 브리지 스크립트 파싱 문제는 별도로 고친다.

## 6. 마지막 확인 상태와 다음 확인

2026-07-24 09:48 미니PC 실측:

- `main` HEAD `540949a`, origin 대비 ahead/behind `0/0`
- 전체 테스트 `433 passed`, 기존 경고 1건
- 미추적 `AGENTS.md` 보존
- 장중이어서 웹앱과 paper runner 재기동은 보류

2026-07-26 추가 실측:

- SSH `미니PC@100.100.141.24 hostname` 성공, 출력 `DESKTOP-NFN1RCA`
- `C:\trading-bot`은 `main` HEAD `540949a`, origin 대비 ahead/behind `0/0`
- 미추적 `AGENTS.md` 존재 및 보존 필요
- 상주 3프로세스 생존: tracker, paper runner, uvicorn 8000
- `strategy.paper_runner --report` exit code 0

이는 과거 실측이다. 다음 작업자는 현재 상태를 다시 확인해야 한다. 새 코드의 운영
반영 여부가 불명확하면 읽기 전용으로 다음을 확인한다.

- 상주 3프로세스 PID·부모·명령행
- 포트 8000
- `strategy.paper_runner --report`
- `universe_membership_events` 테이블/trigger/bootstrap 존재 여부

재기동이 여전히 필요하면 오너 승인 후 장 마감에 웹앱을 WMI로 먼저 재기동하고,
그 다음 paper runner만 WMI로 재기동한다. tracker와 주문 프로세스는 건드리지 않는다.

## 7. 2026-08-02 노트북 전달 체크포인트

- 전달 전 로컬 `main`과 `origin/main`은 `3f5cf21`에서 ahead/behind `0/0`이었다.
- 신규 연구 테스트 16 passed, 전체 `tests\` 463 passed, 기존 warning 1건,
  `git diff --check` 정상으로 실측했다.
- pick-date 근사 재현은 50종목·실데이터 50종목을 읽고 거래 0건으로 종료했다.
- `--ignore-pick-date` 편향 탐색은 문서의 +3.21%/MDD -2.84%, 피크노출×일봉
  proxy와의 단순 격차 -37.64%p를 재현했다. 동일노출·초과수익으로 해석하지 않는다.
- 원 PC의 `tmp/` PDF 렌더·텍스트 추출물은 미추적 로컬 산출물이므로 커밋하지
  않았다. 노트북에서 없더라도 결손이 아니다.
- pull 직후 위 테스트를 다시 실행하고 브랜치·HEAD·ahead/behind·로컬 변경을
  오너에게 보고한 뒤 다음 지시를 기다린다. 미니PC 배포나 재기동은 하지 않는다.

## 8. 2026-08-07 운영 DB 분석 스냅샷

- 미니PC 운영 DB를 직접 수정·동기화하지 않는다. `scripts/db_snapshot.py`로 생성한
  스냅샷만 내려받아 검증·설치하고 읽기 전용 분석한다.
- 코드를 pull한 뒤 `docs/db_analysis_snapshots.md`를 읽는다. 받은 디렉터리에서
  `.venv\Scripts\python.exe scripts\db_snapshot.py verify <snapshot>`을 먼저 실행하고,
  `install <snapshot> --destination <analysis-root>`으로 별도 분석 사본을 만든다.
- `manifest.json`의 `git.commit`과 분석 코드 커밋을 맞춘다. 분석 중 생기는 표,
  Parquet, 임시 테이블은 운영 DB가 아니라 별도 출력 경로에 저장한다.
- 스냅샷이나 수정 DB를 미니PC `db/`로 되돌려 보내지 않는다. 전략 수정은 브랜치,
  테스트, 보고서로 전달하며 운영 반영은 오너의 별도 승인을 받는다.
- 현재 기준본은 미니PC
  `C:\trading-bot\db\snapshots\20260807-225346`이며 manifest Git 커밋은
  `2dc3bc2`, `git.reproducible=true`, `paper.db` 최신 확정일은 2026-08-07이다.
  노트북은 Tailscale SSH `미니PC@100.100.141.24`에서 이 디렉터리를 내려받고
  반드시 `verify` 후 `install`한다. 더 최신 `latest.json`이 있으면 그 ID와
  manifest의 Git 커밋을 우선한다.
