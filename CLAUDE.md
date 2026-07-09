# CLAUDE.md — trading-bot 표준 작업 규칙

이 파일은 매 세션 자동 로드된다. **PC ↔ 노트북 충돌 방지**와 **작업 품질**을 위한 표준 운영 규칙이며, git으로 동기화되므로 두 기기가 같은 규칙을 공유한다. (= "이 파일만 있으면 충돌 안 나는" 단일 기준점)

## 0. 먼저 읽을 것
- 프로젝트 목적·전략·Phase 현황: `PROJECT_HANDOFF.md`
- 누적된 의사결정·환경 메모: `.claude-memory/` (인덱스 `MEMORY.md`)

---

## 1. 기기 동기화 프로토콜 (PC ↔ 노트북 충돌 방지) — 최우선

> 배경: 2026-06-27 노트북이 origin보다 뒤처진 채 작업을 시작해, 원격에 이미 있던
> 웹 UI를 "잃은 줄" 알았던 사고가 있었다. 아래 규칙으로 재발 방지한다.

### git으로 동기화되는 것 (commit/push/pull로 따라옴)
- 모든 **코드**, 이 **CLAUDE.md**, **PROJECT_HANDOFF.md**, **`.claude-memory/`**(AI 메모리)

### git으로 동기화 안 되는 것 (기기 로컬 · gitignore)
- `.env` — API 키. 기기마다 직접 채운다 (`.env.example` 참고). 재발급 말고 조회만.
- `db/trading.db` — 기기별 독립. **노트북 = Phase 2.5 분봉 적립 정본**(KIS 당일만 제공=백필 불가).
  **미니PC = 모의투자 지정 기기**(2026-07-06): 페이퍼 유니버스는 미니PC trading.db 라이브 조회 —
  **픽 등록/교체는 반드시 미니PC 웹앱에서** (다른 기기 등록분은 안 넘어옴). `db/paper.db` 도 미니PC 전용 원장.
- `.venv/`, `.claude/settings.local.json`

### 세션 시작 시 (필수)
```bash
git fetch origin && git status
# 'behind'면 반드시 git pull 먼저. pull 없이 새 작업 시작 금지.
```

### 작업 종료 / 기기 전환 전 (필수)
```bash
# 1) PROJECT_HANDOFF.md + 관련 .claude-memory 최신화
# 2) git add -A && git commit && git push origin main
```
push 안 하고 끄면 다른 기기에서 못 받는다(= 오늘 사고의 원인). **끄기 전 push는 의무.**

### 새 기기 1회 세팅
1. `git clone` 또는 `git pull`
2. `.env` 생성 (`.env.example` 복사 후 키 입력)
3. 메모리 junction 연결: `.claude-memory/SYNC_SETUP.md` 절차대로
4. (적립 기기일 때만) `python scripts/migrations/migration_runner.py`

---

## 2. 커밋 전 독립 리뷰 (표준 절차)

**비자명한 코드 변경을 커밋하기 전에는 독립 리뷰어를 띄워 리뷰한다.**
- 방법: `/code-review`(기본 high) 또는 독립 리뷰 서브에이전트
- 리뷰에서 나온 **명확한 버그·관찰성 문제는 고치고**, 설계 판단은 사용자에게 보고 후 결정
- 문서만/사소한 변경이면 생략 가능
- 흐름: 작업 → (어느 정도 쌓이면) 독립 리뷰 → 수정 → 커밋 → push

---

## 3. 문서 최신 유지 (충돌 최소화)

기능 1개 완료, 의사결정 변경, Phase 진전 등 **마디마다**:
- `PROJECT_HANDOFF.md` 갱신 (현재 상태 + 다음 작업)
- 관련 `.claude-memory/` 갱신 (인덱스 `MEMORY.md` 포함)
- commit + push

→ 항상 "**pull만 하면 최신**"인 상태를 유지해 충돌 여지를 없앤다.

---

## 4. 실행 환경 (노트북)
- venv는 Python 3.14 → 실행은 `./.venv/Scripts/python.exe` (시스템 python 3.12와 다름)
- Windows 콘솔 한글/이모지 깨짐 방지: 출력 있는 스크립트는 `PYTHONIOENCODING=utf-8` 접두
- 데이터 적립: 평일 장중~16:00 노트북 켜고 `python main_tracker.py`
- 웹 대시보드: `./.venv/Scripts/python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000`
  (노트북=코드 검증용 localhost. 동료 공유 배포는 미니PC에서 0.0.0.0 + `.env` `WEB_SHARED_KEY`(영문·숫자) 필수 — `HANDOFF_웹공유.md` 참고)
