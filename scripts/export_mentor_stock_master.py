"""Export a read-only stock-master snapshot for Mentor Signal Reader."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.stock_master import StockMaster


async def export_snapshot(output: Path) -> int:
    master = StockMaster()
    await master.ensure_loaded()
    if not master._by_code:
        raise RuntimeError("stock master is empty")
    payload = {"version": 1, "by_code": master._by_code}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, output)
    return len(master._by_code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    count = asyncio.run(export_snapshot(Path(args.output)))
    print(f"exported={count} path={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
