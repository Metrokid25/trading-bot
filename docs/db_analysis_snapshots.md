# 운영 DB 분석 스냅샷

미니PC의 `db/paper.db`, `db/trading.db`, `db/toss_candles.db`는 운영 원본이다.
노트북·데스크톱 작업자는 이 파일을 직접 열거나 되돌려 쓰지 않고, 장 마감 후 만든
분석용 스냅샷만 읽는다.

## 미니PC에서 생성

장 마감 데이터가 확정된 뒤 저장소 루트에서 실행한다.

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe scripts\db_snapshot.py create
```

기본 동작은 미추적 `AGENTS.md`를 제외한 Git 작업 변경이 있으면 생성을 거부한다.
따라서 manifest의 커밋을 체크아웃하면 스냅샷 생성 코드와 전략 코드를 재현할 수
있다. 긴급 조사에서 dirty 상태가 불가피할 때만 `--allow-dirty`를 사용하며, 이 경우
manifest의 `git.reproducible`은 `false`다.

기본 출력은 `db/snapshots/<YYYYMMDD-HHMMSS>/`이며 다음 파일을 포함한다.

- 세 SQLite DB의 Backup API 복제본
- Git 커밋·브랜치·로컬 상태
- 테이블별 행 수와 주요 최신 시점
- 파일 크기와 SHA-256
- `PRAGMA quick_check` 결과

대용량 캔들 DB가 필요 없는 일일 점검은 다음처럼 생성할 수 있다.

```powershell
.venv\Scripts\python.exe scripts\db_snapshot.py create --without-candles
```

공유 폴더나 NAS에 직접 발행하려면 `--output-root`에 로컬 또는 UNC 경로를 준다.
완성 전에는 `.partial-*` 디렉터리만 보이며, 모든 DB 검증이 끝난 뒤 최종 디렉터리와
`latest.json`이 게시된다.

## 전달과 검증

현재 스냅샷을 다른 PC로 복사한 뒤 다음 명령으로 검증한다.

```powershell
.venv\Scripts\python.exe scripts\db_snapshot.py verify D:\trading-data\snapshots
```

검증된 사본을 별도 분석 디렉터리에 설치하고 DB 파일의 쓰기 속성을 제거하려면:

```powershell
.venv\Scripts\python.exe scripts\db_snapshot.py install `
  D:\trading-data\snapshots `
  --destination D:\trading-data\analysis
```

Tailscale SSH를 쓰는 노트북은 미니PC의 `latest.json`과 해당 `snapshot_id`
디렉터리를 SFTP/SCP로 내려받은 다음 위 `verify`/`install` 절차를 따른다. 장기적으로는
`--output-root`를 읽기 권한만 배포한 NAS·Syncthing 단방향 폴더 또는 비공개 객체
저장소에 지정한다.

## 분석 규칙

1. `manifest.json`의 `git.commit`과 같은 코드를 체크아웃한다.
2. 스냅샷 DB는 SQLite URI `mode=ro` 또는 `PRAGMA query_only=ON`으로 연다.
3. 분석 중 파생 테이블은 별도 DB나 CSV/Parquet에 저장한다.
4. 작업자 DB를 미니PC 운영 DB로 역동기화하지 않는다.
5. `.env`, API 키, `WEB_SHARED_KEY`, 운영 로그는 스냅샷에 포함하지 않는다.
6. 전략 변경 결과는 코드·테스트·보고서로 전달하고 운영 반영은 별도 승인받는다.
