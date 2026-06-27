---
name: workflow-rules
description: 표준 작업 규칙(리뷰 전 커밋·기기 동기화·문서 최신화)은 CLAUDE.md에 있음
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9ccc5d3b-07ea-40eb-acd9-25c20010d3fa
---

사용자 요청(2026-06-27)으로 표준 작업 규칙을 저장소 루트 `CLAUDE.md`에 명문화했다. 매 세션 자동 로드 + git 동기화라 PC↔노트북이 같은 규칙을 공유한다.

핵심 3가지:
1. **커밋 전 독립 리뷰**: 비자명한 코드 변경은 커밋 전에 /code-review(또는 독립 서브에이전트)로 리뷰하고 명확한 문제는 고친다.
2. **기기 동기화**: 세션 시작 시 `git fetch && pull`, 종료/전환 전 `commit && push`. git 동기화 대상(코드·CLAUDE.md·PROJECT_HANDOFF.md·.claude-memory)과 비대상(.env·db/·.venv) 구분.
3. **문서 최신화**: 마디마다 PROJECT_HANDOFF.md + 메모리 갱신 후 push.

**Why:** 2026-06-27 노트북이 origin보다 뒤처진 채 작업해 웹 UI를 잃은 줄 안 사고 재발 방지 + 리뷰 자동화.
**How to apply:** 매 세션 CLAUDE.md를 따른다. 규칙 변경 시 CLAUDE.md를 단일 출처로 갱신. 관련 [[data-accumulation-machine]] [[memory-sync-setup]] [[git-push-main-manual]].
