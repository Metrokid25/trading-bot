# Memory Index

- [Laptop run env](laptop-run-env.md) — venv는 Python 3.14, `./.venv/Scripts/python.exe`로 실행 + `PYTHONIOENCODING=utf-8`
- [Trading bot secrets setup](trading-bot-secrets-setup.md) — 노트북 .env 키 세팅 완료, HTS_ID는 미사용
- [Memory sync setup](memory-sync-setup.md) — 메모리는 .claude-memory/ junction으로 git 동기화, 전환 전 commit+push
- [Git push main manual](git-push-main-manual.md) — main 직접 push는 auto-mode가 막음, 사용자가 직접 push
- [Trading bot purpose](trading-bot-purpose.md) — 섹터쏠림+수급 단타봇, 풀백 재폭발 전략, 현재 데이터 누적 단계
- [Data accumulation machine](data-accumulation-machine.md) — 데이터 적립은 노트북에서 (db/trading.db가 정식 누적 DB)
- [Workflow rules](workflow-rules.md) — 표준 규칙(리뷰 전 커밋·기기 동기화·문서 최신화)은 CLAUDE.md에 명문화
- [NXT premarket historical data](nxt-premarket-historical-data.md) — 과거 NXT 프리장 분봉 = 토스 Open API로 확보 가능(확인됨); KIS/트뷰/크레온 불가
- [Trading-bot operating charter](trading-bot-operating-charter.md) — 오너 지정 상주 운영헌장: forward가 최우선 자원, 벤치마크 대비만 판단, 우선순위(유니버스단일화→모의투자→수급축적→gm_v3 실데이터)
- [MiniPC paper deploy](minipc-paper-deploy.md) — 미니PC 모의투자 상주 완료(2026-07-06): 자동시작=Startup VBS(작업스케줄러 아님, 관리자 회피), paper_start 2026-07-06, IP 등록됨
- [Mentor archive buy timing](mentor-archive-buy-timing.md) — 스승님 매수타점 원칙(진바닥→무릎, 허리 필터, 거래량 마름/실림) + article_id, v3 구현
- [Web colleague access](web-colleague-access.md) — 웹앱 공유키(WEB_SHARED_KEY, 영문만)+등록자 스탬프 구현, 기존섹터 추가 시 author 미기록은 오너 결정 대기
- [AI worker handoff](../HANDOFF_AI작업자.md) — 신규 AI 작업자 인수인계 총정리(현황·규칙·함정·백로그), 매 마디 갱신 대상
