"""백테스트 결과 리포트."""
from __future__ import annotations

import csv
from pathlib import Path

from backtest.engine import BacktestResult


def print_summary(r: BacktestResult) -> None:
    print("=" * 50)
    print(f"최종 자산: {r.final_equity:,.0f}원")
    print(f"총 수익률: {r.total_return_pct:+.2f}%")
    print(f"매매 횟수: {r.num_trades}")
    print(f"승률:     {r.win_rate:.1f}%")
    print(f"MDD:      {r.mdd_pct:.2f}%")
    print("=" * 50)


def save_trades_csv(r: BacktestResult, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "code", "side", "price", "qty", "reason", "pnl", "exit_reason"])
        for t in r.trades:
            w.writerow([t.ts.isoformat(), t.code, t.side, t.price, t.qty,
                        t.reason, t.pnl, t.exit_reason.value if t.exit_reason else ""])


def save_equity_csv(r: BacktestResult, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity"])
        for ts, eq in r.equity_curve:
            w.writerow([ts.isoformat(), eq])
