"""데이터 수집 + 백테스트 + 리포트 원샷 실행.

사용법:
    python -m backtest.run_full                    # 디폴트 종목
    python -m backtest.run_full 005930,000660      # 종목 지정

1) KIS에서 3분봉 수집 → SQLite 저장
2) SQLite 에서 로드 → 백테스트 실행
3) 승률 / 평균손익비 / MDD / 수익곡선 출력 + CSV 저장
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from backtest.collect_data import collect_and_save
from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.report import save_equity_csv, save_trades_csv
from data.candle_store import CandleStore

DEFAULT_CODES = ["005930", "000660", "035720", "035420", "005380"]  # 삼성 하이닉스 카카오 네이버 현대차


def extended_summary(r: BacktestResult) -> None:
    sells = [t for t in r.trades if t.side == "SELL"]
    wins = [t for t in sells if t.pnl > 0]
    losses = [t for t in sells if t.pnl <= 0]
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf") if avg_win > 0 else 0.0
    total_pnl = sum(t.pnl for t in sells)

    by_reason: dict[str, int] = {}
    for t in sells:
        key = t.exit_reason.value if t.exit_reason else "?"
        by_reason[key] = by_reason.get(key, 0) + 1

    print("=" * 60)
    print(f"  최종 자산   : {r.final_equity:,.0f} 원")
    print(f"  총 수익률   : {r.total_return_pct:+.2f}%")
    print(f"  실현 PNL    : {total_pnl:+,.0f} 원")
    print(f"  매매 횟수   : {len(sells)} (익절 {len(wins)} / 손절 {len(losses)})")
    print(f"  승률        : {r.win_rate:.1f}%")
    print(f"  평균 익절   : {avg_win:+,.0f} 원")
    print(f"  평균 손절   : {avg_loss:+,.0f} 원")
    print(f"  손익비(R:R) : {rr:.2f}")
    print(f"  MDD         : {r.mdd_pct:.2f}%")
    print(f"  청산 사유   : {by_reason}")
    print("=" * 60)


def ascii_equity_curve(r: BacktestResult, width: int = 60, height: int = 12) -> None:
    curve = r.equity_curve
    if len(curve) < 2:
        print("  (equity curve: 데이터 부족)")
        return
    values = [eq for _, eq in curve]
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        print("  (equity curve: 변동 없음)")
        return
    # x축 샘플링
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    rows = [[" "] * len(sampled) for _ in range(height)]
    for x, v in enumerate(sampled):
        y = int((v - vmin) / (vmax - vmin) * (height - 1))
        y = height - 1 - y
        rows[y][x] = "*"
    print(f"\n  수익 곡선  (min={vmin:,.0f} ~ max={vmax:,.0f})")
    for row in rows:
        print("  |" + "".join(row))
    print("  +" + "-" * len(sampled))


async def load_all(codes: list[str]) -> dict:
    store = CandleStore()
    await store.open()
    # 매우 넓은 범위로 로드 (모든 수집 데이터 포함)
    start = datetime(2000, 1, 1)
    end = datetime.now() + timedelta(days=1)
    data = {c: await store.load(c, start, end) for c in codes}
    await store.close()
    return data


async def main(codes: list[str]) -> None:
    logger.info(f"[1/3] KIS 3분봉 수집 시작: {codes}")
    stats = await collect_and_save(codes)
    total = sum(stats.values())
    print("\n[수집 결과]")
    for k, v in stats.items():
        print(f"  {k}: {v}개")
    print(f"  합계 : {total}개\n")

    if total == 0:
        print("수집된 데이터가 없습니다. KIS API 응답을 확인하세요.")
        return

    logger.info("[2/3] SQLite 로드")
    data = await load_all(codes)
    for c, cs in data.items():
        if cs:
            logger.info(f"  {c}: {len(cs)}봉  ({cs[0].ts} ~ {cs[-1].ts})")

    logger.info("[3/3] 백테스트 실행")
    engine = BacktestEngine(BacktestConfig())
    result = engine.run(data)

    print()
    extended_summary(result)
    ascii_equity_curve(result)

    Path("backtest/results").mkdir(parents=True, exist_ok=True)
    save_trades_csv(result, "backtest/results/trades.csv")
    save_equity_csv(result, "backtest/results/equity.csv")
    print("\n  저장: backtest/results/trades.csv, equity.csv")


if __name__ == "__main__":
    codes = sys.argv[1].split(",") if len(sys.argv) > 1 else DEFAULT_CODES
    asyncio.run(main(codes))
