from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scripts.db_snapshot as snapshot_module
from scripts.db_snapshot import create_snapshot, install_snapshot, verify_snapshot


def _make_db(path: Path, rows: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO samples(value) VALUES (?)",
            [(f"row-{index}",) for index in range(rows)],
        )


def _source_dbs(root: Path) -> None:
    root.mkdir()
    _make_db(root / "paper.db", 3)
    _make_db(root / "trading.db", 4)
    _make_db(root / "toss_candles.db", 5)


def test_create_and_verify_snapshot(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    output_root = tmp_path / "snapshots"
    _source_dbs(db_root)

    snapshot = create_snapshot(
        db_root,
        output_root,
        repo_root=tmp_path,
        now=datetime(2026, 8, 7, 21, 45, tzinfo=ZoneInfo("Asia/Seoul")),
        allow_dirty=True,
    )

    assert snapshot.name == "20260807-214500"
    manifest = verify_snapshot(output_root)
    assert manifest["read_only_analysis_copy"] is True
    assert set(manifest["files"]) == {
        "paper.db",
        "trading.db",
        "toss_candles.db",
    }
    assert manifest["files"]["paper.db"]["tables"]["samples"] == 3
    assert not list(snapshot.glob("*.db-wal"))
    assert not list(snapshot.glob("*.db-shm"))
    assert json.loads((output_root / "latest.json").read_text(encoding="utf-8"))[
        "snapshot_id"
    ] == "20260807-214500"


def test_verify_rejects_corrupted_snapshot(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    snapshot = create_snapshot(
        db_root, tmp_path / "out", repo_root=tmp_path, allow_dirty=True
    )
    with (snapshot / "paper.db").open("ab") as stream:
        stream.write(b"corrupt")

    with pytest.raises(RuntimeError, match="크기 불일치"):
        verify_snapshot(snapshot)


def test_without_candles_and_install_readonly(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    snapshot = create_snapshot(
        db_root,
        tmp_path / "out",
        include_candles=False,
        repo_root=tmp_path,
        allow_dirty=True,
    )

    installed = install_snapshot(snapshot, tmp_path / "analysis")
    manifest = verify_snapshot(installed)
    assert set(manifest["files"]) == {"paper.db", "trading.db"}
    assert not (installed / "paper.db").stat().st_mode & stat.S_IWRITE
    with sqlite3.connect(installed / "paper.db") as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO samples(value) VALUES ('must-fail')")
    assert not (installed / "toss_candles.db").exists()


def test_backup_reads_committed_wal_without_modifying_source(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    db_root.mkdir()
    connections: list[sqlite3.Connection] = []
    try:
        for name in ("paper.db", "trading.db", "toss_candles.db"):
            conn = sqlite3.connect(db_root / name)
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            conn.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO samples(value) VALUES ('committed-in-wal')")
            conn.commit()
            connections.append(conn)
        source_hash_before = (db_root / "paper.db").read_bytes()

        snapshot = create_snapshot(
            db_root, tmp_path / "out", repo_root=tmp_path, allow_dirty=True
        )

        with sqlite3.connect(snapshot / "paper.db") as copied:
            assert copied.execute("SELECT value FROM samples").fetchone()[0] == "committed-in-wal"
        assert (db_root / "paper.db").read_bytes() == source_hash_before
        assert (db_root / "paper.db-wal").exists()
        connections[0].execute("INSERT INTO samples(value) VALUES ('still-writable')")
        connections[0].commit()
    finally:
        for conn in connections:
            conn.close()


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"paper.db": {}},
        {"paper.db": {}, "trading.db": {}, "../outside.db": {}},
    ],
)
def test_verify_rejects_incomplete_or_unsafe_manifest(
    tmp_path: Path, files: dict[str, object]
) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    snapshot = create_snapshot(
        db_root, tmp_path / "out", repo_root=tmp_path, allow_dirty=True
    )
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError):
        verify_snapshot(snapshot)


def test_verify_rejects_snapshot_id_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "latest.json").write_text(
        json.dumps({"snapshot_id": "../outside"}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="snapshot_id"):
        verify_snapshot(root)


def test_install_rolls_back_if_latest_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    snapshot = create_snapshot(
        db_root, tmp_path / "out", repo_root=tmp_path, allow_dirty=True
    )
    destination_root = tmp_path / "analysis"
    original_replace = os.replace

    def fail_latest(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(destination).name == "latest.json":
            raise OSError("simulated latest publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_latest)
    with pytest.raises(OSError, match="simulated"):
        install_snapshot(snapshot, destination_root)
    assert not (destination_root / snapshot.name).exists()
    assert not list(destination_root.glob(".partial-*"))


def test_create_rejects_git_metadata_failure_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    monkeypatch.setattr(
        snapshot_module, "_git_metadata", lambda _: {"error": "git unavailable"}
    )

    with pytest.raises(RuntimeError, match="git unavailable"):
        create_snapshot(db_root, tmp_path / "out", repo_root=tmp_path)


def test_create_rolls_back_if_latest_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_root = tmp_path / "db"
    _source_dbs(db_root)
    output_root = tmp_path / "out"
    original_replace = os.replace

    def fail_latest(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(destination).name == "latest.json":
            raise OSError("simulated latest publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_latest)
    with pytest.raises(OSError, match="simulated"):
        create_snapshot(
            db_root, output_root, repo_root=tmp_path, allow_dirty=True
        )
    assert not list(output_root.glob("20*"))
    assert not list(output_root.glob(".partial-*"))
    assert not list(output_root.glob(".latest.*.tmp"))


def test_backup_is_consistent_during_repeated_wal_commits(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    db_root.mkdir()
    for name in ("paper.db", "trading.db", "toss_candles.db"):
        with sqlite3.connect(db_root / name) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO samples(value) VALUES (?)",
                [("seed",)] * 20_000,
            )

    stop = threading.Event()
    started = threading.Event()

    def writer() -> None:
        with sqlite3.connect(db_root / "paper.db", timeout=30) as conn:
            index = 0
            while not stop.is_set():
                conn.execute("INSERT INTO samples(value) VALUES (?)", (f"live-{index}",))
                conn.commit()
                index += 1
                started.set()
                time.sleep(0.001)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(timeout=5)
        snapshot = create_snapshot(
            db_root, tmp_path / "out", repo_root=tmp_path, allow_dirty=True
        )
    finally:
        stop.set()
        thread.join(timeout=5)

    with sqlite3.connect(snapshot / "paper.db") as copied:
        copied_count = copied.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        assert copied.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with sqlite3.connect(db_root / "paper.db") as source:
        source_count = source.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert 20_001 <= copied_count <= source_count
