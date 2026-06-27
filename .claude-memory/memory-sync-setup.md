---
name: memory-sync-setup
description: 프로젝트 메모리가 git로 동기화되는 구조 (junction)
metadata:
  type: project
---

이 프로젝트의 Claude 메모리는 git로 PC↔노트북 동기화된다. 하니스 메모리 경로(`~/.claude/projects/C--trading-bot/memory`)가 저장소 안 `.claude-memory/` 폴더로 **디렉토리 junction** 연결되어 있어서, 메모리를 쓰면 곧바로 저장소에 반영된다.

따라서 메모리를 추가/수정한 뒤에는 **`git add -A && commit && push` 해야 다른 기기에 전달**된다. 새 기기에서는 clone 후 junction을 1회 걸어야 한다 — 절차는 저장소 `.claude-memory/SYNC_SETUP.md` 참고.

**Why:** Claude Code 대화/메모리는 기기 로컬에만 저장되고 자동 클라우드 동기화가 없다. 어제 PC에서 한 웹 UI 작업이 커밋 안 돼 노트북으로 안 따라온 사고가 계기.
**How to apply:** 기기 전환 전 항상 commit+push. 실행 환경은 [[laptop-run-env]], 키 세팅은 [[trading-bot-secrets-setup]].
