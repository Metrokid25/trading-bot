"""운영 SQLite DB의 분석용 읽기 전용 스냅샷을 만든다.

운영 파일을 단순 복사하지 않고 SQLite Backup API를 사용하므로 WAL 쓰기가
진행 중이어도 일관된 복제본을 얻는다. 스냅샷에는 Git 상태, DB 요약,
SHA-256을 기록한 manifest.json이 포함된다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_ROOT = PROJECT_ROOT / "db"
DEFAULT_OUTPUT_ROOT = DEFAULT_DB_ROOT / "snapshots"
DATABASE_NAMES = ("paper.db", "trading.db", "toss_candles.db")
CORE_DATABASE_NAMES = frozenset(DATABASE_NAMES[:2])
ALLOWED_DATABASE_NAMES = frozenset(DATABASE_NAMES)
MANIFEST_VERSION = 1
KST = ZoneInfo("Asia/Seoul")
SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}$")


def _readonly_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return f"{path.resolve().as_uri()}{suffix}"


def _connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    return sqlite3.connect(
        _readonly_uri(path, immutable=immutable), uri=True, timeout=30
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def _database_summary(path: Path, database_name: str) -> dict[str, Any]:
    with closing(_connect_readonly(path, immutable=True)) as conn:
        quick_check = _scalar(conn, "PRAGMA quick_check")
        if quick_check != "ok":
            raise RuntimeError(f"{database_name} quick_check 실패: {quick_check}")

        tables: dict[str, int] = {}
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table_name in table_names:
            quoted = _quote_identifier(table_name)
            tables[table_name] = int(_scalar(conn, f"SELECT COUNT(*) FROM {quoted}"))

        latest: dict[str, Any] = {}
        if database_name == "paper.db" and "paper_daily" in tables:
            latest_row = conn.execute(
                "SELECT MAX(day), MAX(CASE WHEN finalized=1 THEN day END) "
                "FROM paper_daily"
            ).fetchone()
            latest = {
                "latest_day": latest_row[0],
                "latest_finalized_day": latest_row[1],
            }
        elif database_name == "trading.db":
            if "pick_daily_tracking" in tables:
                latest["latest_tracking_day"] = _scalar(
                    conn, "SELECT MAX(trading_day) FROM pick_daily_tracking WHERE status='success'"
                )
            if "universe_membership_events" in tables:
                latest["latest_membership_event"] = _scalar(
                    conn, "SELECT MAX(occurred_at) FROM universe_membership_events"
                )
        elif database_name == "toss_candles.db" and "candles" in tables:
            row = conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(DISTINCT symbol) FROM candles"
            ).fetchone()
            latest = {
                "first_candle_ts": row[0],
                "latest_candle_ts": row[1],
                "symbols": row[2],
            }

        return {
            "quick_check": quick_check,
            "user_version": int(_scalar(conn, "PRAGMA user_version")),
            "tables": tables,
            **latest,
        }


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    try:
        return {
            "commit": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "status_porcelain": run("status", "--short").splitlines(),
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"error": str(exc)}


def _meaningful_git_status(metadata: dict[str, Any]) -> list[str]:
    """기기 로컬 운영 규칙 파일인 미추적 AGENTS.md만 예외로 둔다."""
    return [
        line
        for line in metadata.get("status_porcelain", [])
        if line.strip() != "?? AGENTS.md"
    ]


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(function: Any, target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if path.exists():
        shutil.rmtree(path, onerror=make_writable_and_retry)


def _validate_snapshot_id(value: Any) -> str:
    snapshot_id = str(value)
    if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise RuntimeError(f"잘못된 snapshot_id: {snapshot_id!r}")
    return snapshot_id


def _validate_file_names(value: Any) -> set[str]:
    if not isinstance(value, dict):
        raise RuntimeError("manifest files는 객체여야 함")
    names = set(value)
    if not CORE_DATABASE_NAMES.issubset(names):
        raise RuntimeError("manifest에 paper.db와 trading.db가 모두 필요함")
    if not names.issubset(ALLOWED_DATABASE_NAMES):
        invalid = sorted(names - ALLOWED_DATABASE_NAMES)
        raise RuntimeError(f"허용되지 않은 DB 파일명: {invalid}")
    return names


def _backup_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"운영 DB 없음: {source}")
    with closing(_connect_readonly(source)) as source_conn:
        with closing(sqlite3.connect(destination, timeout=30)) as destination_conn:
            source_conn.backup(destination_conn, pages=4096, sleep=0.01)
            destination_conn.execute("PRAGMA journal_mode=DELETE")


def create_snapshot(
    db_root: Path,
    output_root: Path,
    *,
    include_candles: bool = True,
    repo_root: Path = PROJECT_ROOT,
    now: datetime | None = None,
    allow_dirty: bool = False,
) -> Path:
    created_at = now or datetime.now(KST)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=KST)
    snapshot_id = created_at.astimezone(KST).strftime("%Y%m%d-%H%M%S")
    git_metadata = _git_metadata(repo_root)
    dirty_status = _meaningful_git_status(git_metadata)
    if ("error" in git_metadata or dirty_status) and not allow_dirty:
        reason = git_metadata.get("error") or ", ".join(dirty_status)
        raise RuntimeError(
            "재현 가능한 스냅샷을 위해 Git 작업트리를 먼저 정리해야 함: "
            + str(reason)
        )
    git_metadata["reproducible"] = not dirty_status and "error" not in git_metadata
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / snapshot_id
    partial_dir = output_root / f".partial-{snapshot_id}"
    if final_dir.exists() or partial_dir.exists():
        raise FileExistsError(f"이미 존재하는 스냅샷: {snapshot_id}")
    partial_dir.mkdir()

    names = list(DATABASE_NAMES if include_candles else DATABASE_NAMES[:2])
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "created_at_kst": created_at.astimezone(KST).isoformat(),
        "source_role": "minipc-production",
        "read_only_analysis_copy": True,
        "git": git_metadata,
        "files": {},
    }

    published = False
    latest_published = False
    latest_temp: Path | None = None
    try:
        for name in names:
            source = db_root / name
            destination = partial_dir / name
            print(f"[snapshot] {name} 백업 중...", flush=True)
            _backup_database(source, destination)
            summary = _database_summary(destination, name)
            manifest["files"][name] = {
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
                **summary,
            }

        manifest_path = partial_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        partial_dir.replace(final_dir)
        published = True

        latest = {"snapshot_id": snapshot_id, "manifest": f"{snapshot_id}/manifest.json"}
        latest_temp = output_root / f".latest.{snapshot_id}.{os.getpid()}.tmp"
        latest_temp.write_text(
            json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(latest_temp, output_root / "latest.json")
        latest_published = True
        return final_dir
    except Exception:
        if latest_temp is not None:
            latest_temp.unlink(missing_ok=True)
        _remove_tree(partial_dir)
        if published and not latest_published:
            _remove_tree(final_dir)
        raise


def resolve_snapshot(path: Path) -> Path:
    path = path.resolve()
    if path.is_file() and path.name == "manifest.json":
        return path.parent
    if (path / "manifest.json").is_file():
        return path
    latest_path = path / "latest.json"
    if latest_path.is_file():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        snapshot_id = _validate_snapshot_id(latest.get("snapshot_id"))
        candidate = (path / snapshot_id).resolve()
        if not candidate.is_relative_to(path):
            raise RuntimeError("latest.json 경로가 스냅샷 루트를 벗어남")
        if (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError(f"스냅샷 또는 latest.json을 찾을 수 없음: {path}")


def verify_snapshot(path: Path, *, _allow_partial_dir: bool = False) -> dict[str, Any]:
    snapshot_dir = resolve_snapshot(path)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise RuntimeError(f"지원하지 않는 manifest_version: {manifest.get('manifest_version')}")
    snapshot_id = _validate_snapshot_id(manifest.get("snapshot_id"))
    expected_names = {snapshot_id}
    if _allow_partial_dir:
        expected_names.add(f".partial-{snapshot_id}")
    if snapshot_dir.name not in expected_names:
        raise RuntimeError(
            f"manifest snapshot_id와 디렉터리 불일치: {snapshot_id} != {snapshot_dir.name}"
        )
    file_names = _validate_file_names(manifest.get("files"))
    for name in sorted(file_names):
        expected = manifest["files"][name]
        database_path = (snapshot_dir / name).resolve()
        if database_path.parent != snapshot_dir:
            raise RuntimeError(f"DB 경로가 스냅샷 디렉터리를 벗어남: {name}")
        if not database_path.is_file():
            raise FileNotFoundError(f"스냅샷 DB 없음: {database_path}")
        actual_size = database_path.stat().st_size
        if actual_size != expected["bytes"]:
            raise RuntimeError(f"{name} 크기 불일치: {actual_size} != {expected['bytes']}")
        actual_hash = _sha256(database_path)
        if actual_hash != expected["sha256"]:
            raise RuntimeError(f"{name} SHA-256 불일치")
        with closing(_connect_readonly(database_path, immutable=True)) as conn:
            quick_check = _scalar(conn, "PRAGMA quick_check")
        if quick_check != "ok":
            raise RuntimeError(f"{name} quick_check 실패: {quick_check}")
        print(f"[verify] {name}: ok", flush=True)
    return manifest


def install_snapshot(source: Path, destination_root: Path) -> Path:
    source_dir = resolve_snapshot(source)
    manifest = verify_snapshot(source_dir)
    snapshot_id = _validate_snapshot_id(manifest["snapshot_id"])
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = (destination_root / snapshot_id).resolve()
    partial = (destination_root / f".partial-{snapshot_id}").resolve()
    if not destination.is_relative_to(destination_root) or not partial.is_relative_to(
        destination_root
    ):
        raise RuntimeError("설치 경로가 destination 루트를 벗어남")
    if destination.exists() or partial.exists():
        raise FileExistsError(f"이미 설치된 스냅샷: {snapshot_id}")
    published = False
    latest_published = False
    latest_temp = destination_root / f".latest.{snapshot_id}.{os.getpid()}.tmp"
    try:
        shutil.copytree(source_dir, partial)
        verify_snapshot(partial, _allow_partial_dir=True)
        for name in _validate_file_names(manifest["files"]):
            db_path = partial / name
            os.chmod(db_path, db_path.stat().st_mode & ~stat.S_IWRITE)
        partial.replace(destination)
        published = True
        latest_temp.write_text(
            json.dumps(
                {"snapshot_id": snapshot_id, "manifest": f"{snapshot_id}/manifest.json"},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(latest_temp, destination_root / "latest.json")
        latest_published = True
        return destination
    except Exception:
        latest_temp.unlink(missing_ok=True)
        _remove_tree(partial)
        if published and not latest_published:
            _remove_tree(destination)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="운영 DB에서 새 스냅샷 생성")
    create.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    create.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    create.add_argument(
        "--without-candles",
        action="store_true",
        help="대용량 toss_candles.db를 제외하고 paper/trading DB만 생성",
    )
    create.add_argument(
        "--allow-dirty",
        action="store_true",
        help="비재현 dirty Git 상태를 manifest에 명시하고 예외적으로 생성",
    )

    verify = subparsers.add_parser("verify", help="해시와 SQLite 무결성 검증")
    verify.add_argument("snapshot", type=Path)

    install = subparsers.add_parser("install", help="검증 후 분석용 읽기 전용 사본 설치")
    install.add_argument("snapshot", type=Path)
    install.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create":
        result = create_snapshot(
            args.db_root,
            args.output_root,
            include_candles=not args.without_candles,
            allow_dirty=args.allow_dirty,
        )
        print(f"[done] {result}")
    elif args.command == "verify":
        manifest = verify_snapshot(args.snapshot)
        print(f"[done] snapshot_id={manifest['snapshot_id']}")
    elif args.command == "install":
        result = install_snapshot(args.snapshot, args.destination)
        print(f"[done] 읽기 전용 분석 사본: {result}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
