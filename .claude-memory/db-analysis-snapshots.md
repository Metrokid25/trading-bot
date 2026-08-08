# DB analysis snapshots

- 미니PC의 `paper.db`, `trading.db`, `toss_candles.db`가 운영 원본이다.
- 다른 작업자는 SQLite Backup API로 만든 `db/snapshots/<snapshot_id>` 사본만
  읽는다. 운영 DB 직접 복사·수정·역동기화는 금지한다.
- `scripts/db_snapshot.py create`는 세 DB, `--without-candles`는 소형 두 DB만
  생성한다. `--output-root`로 NAS/단방향 공유 폴더를 지정할 수 있다.
- `manifest.json`은 Git 커밋/상태, DB 요약, 최신 시점, SHA-256, quick_check를
  보존한다. `verify` 후 `install --destination`으로 읽기 전용 분석 사본을 만든다.
- 작업자는 manifest의 Git 커밋과 코드를 맞추고 파생 데이터는 별도 DB/파일에 쓴다.
- 상세 명령과 전달 규칙: `docs/db_analysis_snapshots.md`.
- 2026-08-07 최신 전체본: `20260807-225346`, main `2dc3bc2`, reproducible=true,
  paper 확정일 2026-08-07, toss 79종목/6,254,185행/최신 19:38 KST, 세 DB 검증 ok.
- 자동화 `db`: 평일 21:45 경량본. 자동화 `db-2`: 금요일 22:00 전체본.
  실패할 때만 알리며 자동 삭제는 하지 않는다.
- 2026-08-08 전체 전략 진단은 `docs/research/db_strategy_diagnosis_20260808.md`.
  불완전 분봉 확정·유니버스 중복·원장 불일치가 P0이며, 전략 튜닝 전 완결성 게이트와
  공유현금 회계를 고친다. 잠정 PIT 계좌 재생성은 v2 -0.17%, v2_qv -2.78%다.
