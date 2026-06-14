"""Inspect whether stored minute data can cover NXT premarket monitoring.

This script is read-only by design:
- no KIS/API calls
- no INSERT/UPDATE/DELETE
- SQLite is opened with mode=ro
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_MIGRATIONS = ("007", "008", "009")
# Keep this local constant instead of importing config.settings. Importing settings
# calls ensure_dirs(), which is a write side effect for a read-only inspection tool.
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "trading.db"
AGG_BUILDER_PATH = PROJECT_ROOT / "core" / "minute_agg_builder.py"


@dataclass(frozen=True, slots=True)
class InspectionResult:
    db_path: str
    trading_day: str | None
    raw_schema: str
    applied_migrations: dict[str, bool]
    total_raw_rows: int
    trading_day_raw_rows: int
    premarket_rows: int
    regular_open_rows: int
    regular_0930_rows: int
    premarket_stock_counts: list[tuple[str, int]]
    premarket_ohlcv: list[
        tuple[str, int, float | None, float | None, float | None, float | None, int, int | None]
    ]
    agg_08_bucket_count: int
    agg_builder_findings: list[tuple[int, str]]
    agg_supports_08: bool

    @property
    def verdicts(self) -> list[str]:
        return [
            "NXT_DB_DATA_PRESENT" if self.premarket_rows > 0 else "NXT_DB_DATA_ABSENT",
            "NXT_AGG_SUPPORTS_08_BUCKETS" if self.agg_supports_08 else "NXT_AGG_NEEDS_CHANGE",
            (
                "MIGRATIONS_OK"
                if all(self.applied_migrations.values())
                else "MIGRATIONS_MISSING"
            ),
        ]


def _readonly_uri(db_path: str | Path) -> str:
    path = Path(db_path).resolve()
    return f"file:{quote(path.as_posix(), safe=':/')}?mode=ro"


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _detect_raw_schema(conn: sqlite3.Connection) -> str:
    columns = _table_columns(conn, "pick_minute_raw")
    if not columns:
        return "missing"
    if "minute_time" in columns:
        return "new"
    if "bar_time" in columns:
        return "legacy_bar_time"
    return "unsupported"


def _count_rows(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0] or 0)


def _latest_trading_day(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute(
        """
        SELECT DISTINCT trading_day
        FROM pick_minute_raw
        WHERE trading_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        """
    ).fetchall()
    valid_days: list[date] = []
    for row in rows:
        try:
            valid_days.append(date.fromisoformat(str(row[0])))
        except ValueError:
            continue
    if not valid_days:
        return None
    return max(valid_days).isoformat()


def _applied_migrations(conn: sqlite3.Connection) -> dict[str, bool]:
    if not _table_exists(conn, "schema_migrations"):
        return {version: False for version in REQUIRED_MIGRATIONS}
    rows = conn.execute(
        "SELECT version FROM schema_migrations WHERE version IN (?, ?, ?)",
        REQUIRED_MIGRATIONS,
    ).fetchall()
    applied = {str(row[0]) for row in rows}
    return {version: version in applied for version in REQUIRED_MIGRATIONS}


def _agg_builder_findings() -> list[tuple[int, str]]:
    if not AGG_BUILDER_PATH.exists():
        return []

    findings: list[tuple[int, str]] = []
    needles = (
        "session_start = ts.replace(hour=9, minute=0",
        "if ts < session_start:",
        "minutes_since_start = int((ts - session_start)",
        "bucket_start = session_start + timedelta",
    )
    for lineno, line in enumerate(AGG_BUILDER_PATH.read_text(encoding="utf-8").splitlines(), 1):
        if any(needle in line for needle in needles):
            findings.append((lineno, line.strip()))
    return findings


def _agg_supports_08(findings: list[tuple[int, str]]) -> bool:
    has_0900_anchor = any("hour=9" in line and "minute=0" in line for _, line in findings)
    has_pre_session_skip = any("ts < session_start" in line for _, line in findings)
    return not (has_0900_anchor and has_pre_session_skip)


def inspect_db(db_path: str | Path, trading_day: str | None = None) -> InspectionResult:
    with _connect_readonly(db_path) as conn:
        migrations = _applied_migrations(conn)
        raw_schema = _detect_raw_schema(conn)
        total_raw_rows = (
            _count_rows(conn, "SELECT COUNT(*) FROM pick_minute_raw")
            if raw_schema != "missing"
            else 0
        )
        selected_day = trading_day or (
            _latest_trading_day(conn) if raw_schema != "missing" else None
        )

        if selected_day is None or raw_schema == "missing":
            trading_day_raw_rows = 0
            premarket_rows = 0
            regular_open_rows = 0
            regular_0930_rows = 0
            premarket_stock_counts = []
            premarket_ohlcv = []
        else:
            trading_day_raw_rows = _count_rows(
                conn,
                "SELECT COUNT(*) FROM pick_minute_raw WHERE trading_day = ?",
                (selected_day,),
            )
            if raw_schema != "new":
                premarket_rows = 0
                regular_open_rows = 0
                regular_0930_rows = 0
                premarket_stock_counts = []
                premarket_ohlcv = []
            else:
                premarket_rows = _count_rows(
                    conn,
                    """
                    SELECT COUNT(*)
                    FROM pick_minute_raw
                    WHERE trading_day = ?
                      AND substr(minute_time, 12, 5) >= '08:00'
                      AND substr(minute_time, 12, 5) < '08:51'
                    """,
                    (selected_day,),
                )
                regular_open_rows = _count_rows(
                    conn,
                    """
                    SELECT COUNT(*)
                    FROM pick_minute_raw
                    WHERE trading_day = ?
                      AND substr(minute_time, 12, 5) >= '09:00'
                      AND substr(minute_time, 12, 5) < '09:30'
                    """,
                    (selected_day,),
                )
                regular_0930_rows = _count_rows(
                    conn,
                    """
                    SELECT COUNT(*)
                    FROM pick_minute_raw
                    WHERE trading_day = ?
                      AND substr(minute_time, 12, 5) >= '09:30'
                      AND substr(minute_time, 12, 5) < '10:00'
                    """,
                    (selected_day,),
                )
                premarket_stock_counts = [
                    (str(row[0]), int(row[1]))
                    for row in conn.execute(
                        """
                        SELECT stock_code, COUNT(*) AS row_count
                        FROM pick_minute_raw
                        WHERE trading_day = ?
                          AND substr(minute_time, 12, 5) >= '08:00'
                          AND substr(minute_time, 12, 5) < '08:51'
                        GROUP BY stock_code
                        ORDER BY row_count DESC, stock_code
                        """,
                        (selected_day,),
                    ).fetchall()
                ]
                premarket_ohlcv = [
                    (
                        str(row[0]),
                        int(row[1]),
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        int(row[6] or 0),
                        None if row[7] is None else int(row[7]),
                    )
                    for row in conn.execute(
                        """
                        WITH ordered AS (
                            SELECT
                                stock_code, minute_time, open, high, low, close, volume, value,
                                ROW_NUMBER() OVER (
                                    PARTITION BY stock_code ORDER BY minute_time, id
                                ) AS rn_asc,
                                ROW_NUMBER() OVER (
                                    PARTITION BY stock_code ORDER BY minute_time DESC, id DESC
                                ) AS rn_desc
                            FROM pick_minute_raw
                            WHERE trading_day = ?
                              AND substr(minute_time, 12, 5) >= '08:00'
                              AND substr(minute_time, 12, 5) < '08:51'
                        )
                        SELECT
                            stock_code,
                            COUNT(*) AS row_count,
                            MAX(CASE WHEN rn_asc = 1 THEN open END) AS open,
                            MAX(high) AS high,
                            MIN(low) AS low,
                            MAX(CASE WHEN rn_desc = 1 THEN close END) AS close,
                            SUM(COALESCE(volume, 0)) AS volume,
                            SUM(value) AS value
                        FROM ordered
                        GROUP BY stock_code
                        ORDER BY row_count DESC, stock_code
                        """,
                        (selected_day,),
                    ).fetchall()
                ]

        agg_08_bucket_count = 0
        agg_columns = _table_columns(conn, "pick_minute_agg")
        if selected_day is not None and {"trading_day", "bucket_start"}.issubset(agg_columns):
            agg_08_bucket_count = _count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM pick_minute_agg
                WHERE trading_day = ?
                  AND substr(bucket_start, 12, 2) = '08'
                """,
                (selected_day,),
            )

    findings = _agg_builder_findings()
    return InspectionResult(
        db_path=str(Path(db_path).resolve()),
        trading_day=selected_day,
        raw_schema=raw_schema,
        applied_migrations=migrations,
        total_raw_rows=total_raw_rows,
        trading_day_raw_rows=trading_day_raw_rows,
        premarket_rows=premarket_rows,
        regular_open_rows=regular_open_rows,
        regular_0930_rows=regular_0930_rows,
        premarket_stock_counts=premarket_stock_counts,
        premarket_ohlcv=premarket_ohlcv,
        agg_08_bucket_count=agg_08_bucket_count,
        agg_builder_findings=findings,
        agg_supports_08=_agg_supports_08(findings),
    )


def format_report(result: InspectionResult) -> str:
    lines = [
        "NXT premarket data inspection",
        f"DB: {result.db_path}",
        f"trading_day: {result.trading_day or '(none)'}",
        f"pick_minute_raw schema: {result.raw_schema}",
        "",
        "Migrations:",
    ]
    for version in REQUIRED_MIGRATIONS:
        lines.append(
            f"  m{version}: {'applied' if result.applied_migrations[version] else 'missing'}"
        )

    lines.extend(
        [
            "",
            "Raw minute rows:",
            f"  total pick_minute_raw rows: {result.total_raw_rows}",
            f"  selected trading_day rows: {result.trading_day_raw_rows}",
            f"  08:00~08:50 rows: {result.premarket_rows}",
            f"  09:00~09:30 rows: {result.regular_open_rows}",
            f"  09:30~10:00 rows: {result.regular_0930_rows}",
            "",
            "08:00~08:50 stock row counts:",
        ]
    )
    if result.premarket_stock_counts:
        for stock_code, row_count in result.premarket_stock_counts:
            lines.append(f"  {stock_code}: {row_count}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("08:00~08:50 per-stock OHLCV/value summary:")
    if result.premarket_ohlcv:
        lines.append("  stock_code rows open high low close volume value")
        for row in result.premarket_ohlcv:
            stock_code, row_count, open_, high, low, close, volume, value = row
            lines.append(
                f"  {stock_code} {row_count} {open_} {high} {low} {close} {volume} {value}"
            )
    else:
        if result.raw_schema == "new":
            lines.append("  (not available: no 08:00~08:50 rows)")
        else:
            lines.append(f"  (not available: {result.raw_schema} raw schema)")

    lines.extend(
        [
            "",
            "Aggregate table:",
            f"  pick_minute_agg 08-hour bucket_start rows: {result.agg_08_bucket_count}",
            f"  aggregate trading_day filter: {result.trading_day or '(none; not counted)'}",
            "",
            "minute_agg_builder 09:00 anchor findings:",
        ]
    )
    if result.agg_builder_findings:
        for lineno, text in result.agg_builder_findings:
            lines.append(f"  core/minute_agg_builder.py:{lineno}: {text}")
    else:
        lines.append("  (no matching code found)")

    lines.extend(["", "Verdict:"])
    lines.extend(f"  {verdict}" for verdict in result.verdicts)
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only inspection for NXT 08:00~08:50 minute raw data."
    )
    parser.add_argument("--trading-day", help="Trading day to inspect, YYYY-MM-DD.")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite DB path. Defaults to db/trading.db under the project root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = inspect_db(args.db_path, trading_day=args.trading_day)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
