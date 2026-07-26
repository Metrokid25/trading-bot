# 노트북 AI 작업자 인수인계

기준일: 2026-07-26  
작업 경로: `C:\trading-bot`  
기준 브랜치/커밋: `main` / `540949a`

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

- 현재 기준은 `433 passed`, 기존 `pandas_market_calendars` 경고 1건이다.
- 비자명 변경은 커밋 전에 독립 리뷰어가 전체 diff와 실제 결함을 검토해야 한다.
- 커밋·`main` fast-forward·push는 오너 승인 후에만 한다.
- force push, `git reset --hard`, 사용자 변경 폐기는 금지한다.
- 완료 시 `PROJECT_HANDOFF.md`와 필요한 인수인계 문서를 함께 갱신한다.

## 4. 현재 구현 상태

`540949a fix: make paper metrics membership aware`까지 origin/main에 반영됐다.

- append-only 실제 편입·이탈 이벤트
- `gm_v3_joined`, `v4r_joined`, `bench_joined`
- 시간순 공유현금 `v2_portfolio`, `v2_leader_portfolio`
- 종목당 20%, 동시 5종목, 정규화 섹터당 최대 2종목
- `paper_portfolio_allocations` 감사 테이블
- 신규 관찰축의 기존 텔레그램 알림 격리

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

## 6. 마지막 확인 상태와 다음 확인

2026-07-24 09:48 미니PC 실측:

- `main` HEAD `540949a`, origin 대비 ahead/behind `0/0`
- 전체 테스트 `433 passed`, 기존 경고 1건
- 미추적 `AGENTS.md` 보존
- 장중이어서 웹앱과 paper runner 재기동은 보류

이는 과거 실측이다. 다음 작업자는 현재 상태를 다시 확인해야 한다. 새 코드의 운영
반영 여부가 불명확하면 읽기 전용으로 다음을 확인한다.

- 상주 3프로세스 PID·부모·명령행
- 포트 8000
- `strategy.paper_runner --report`
- `universe_membership_events` 테이블/trigger/bootstrap 존재 여부

재기동이 여전히 필요하면 오너 승인 후 장 마감에 웹앱을 WMI로 먼저 재기동하고,
그 다음 paper runner만 WMI로 재기동한다. tracker와 주문 프로세스는 건드리지 않는다.
