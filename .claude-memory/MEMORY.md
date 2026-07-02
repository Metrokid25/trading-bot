# Memory Index

- [Laptop run env](laptop-run-env.md) — venv는 Python 3.14, `./.venv/Scripts/python.exe`로 실행 + `PYTHONIOENCODING=utf-8`
- [Trading bot secrets setup](trading-bot-secrets-setup.md) — 노트북 .env 키 세팅 완료, HTS_ID는 미사용
- [Memory sync setup](memory-sync-setup.md) — 메모리는 .claude-memory/ junction으로 git 동기화, 전환 전 commit+push
- [Git push main manual](git-push-main-manual.md) — main 직접 push는 auto-mode가 막음, 사용자가 직접 push
- [Trading bot purpose](trading-bot-purpose.md) — 섹터쏠림+수급 단타봇, 풀백 재폭발 전략, 현재 데이터 누적 단계
- [Data accumulation machine](data-accumulation-machine.md) — 데이터 적립은 노트북에서 (db/trading.db가 정식 누적 DB)
- [Workflow rules](workflow-rules.md) — 표준 규칙(리뷰 전 커밋·기기 동기화·문서 최신화)은 CLAUDE.md에 명문화
- [NXT premarket historical data](nxt-premarket-historical-data.md) — 과거 NXT 프리장 분봉 = 토스 Open API로 확보 가능(확인됨); KIS/트뷰/크레온 불가
- [Mentor archive buy timing](mentor-archive-buy-timing.md) — 스승님 매수타점 원칙(진바닥→무릎, 허리 필터, 거래량 마름/실림) + article_id, v3 구현
