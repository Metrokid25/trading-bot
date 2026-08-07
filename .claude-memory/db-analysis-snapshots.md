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
