---
name: git-push-main-manual
description: main 직접 push는 auto-mode가 막으므로 사용자가 직접 push해야 함
metadata:
  type: feedback
---

이 repo에서 `git push origin main`(기본 브랜치 직접 push)은 Claude auto-mode 분류기가 위험 동작으로 소프트 차단한다. 게다가 `.claude/settings.local.json`의 `autoMode.allow`를 에이전트가 직접 편집해 권한을 넓히는 것도 self-modification 가드레일로 차단된다.

**Why:** 단일 메인테이너 repo라 PR 없이 main에 직접 커밋/푸시하는 워크플로우인데, auto-mode는 기본적으로 이를 막는다.
**How to apply:** 커밋까지는 Claude가 하되, **push는 사용자가 직접** `git push origin main` 실행하도록 요청한다. 자동화를 원하면 사용자가 직접 settings.local.json에 autoMode.allow 예외를 넣어야 한다(에이전트는 불가). 메모리 동기화 흐름은 [[memory-sync-setup]].
