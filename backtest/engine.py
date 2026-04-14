"""이벤트 기반 백테스트 엔진.

실전과 동일한 Signal/Exit 로직을 히스토리컬 3분봉에 적용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config import settings
from config.constants import TAKE_PROFIT_LEVELS, ExitReason
from data.models import Candle, Position, Trade
from strategy.signal import evaluate_buy


@dataclass
class BacktestConfig:
    seed: int = 10_000_000
    weight_per_stock: float = 0.33
    stop_loss_pct: float = -5.0
    max_concurrent: int = 3
    fee_rate: float = 0.00015
    tax_rate: float = 0.0018  # 매도 시


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    mdd_pct: float = 0.0
    num_trades: int = 0


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig | None = None) -> None:
        self.cfg = cfg or BacktestConfig()

    def run(self, data: dict[str, list[Candle]]) -> BacktestResult:
        """data: {code: [Candle, ...]} — 이미 시간 정렬된 3분봉."""
        cfg = self.cfg
        positions: dict[str, Position] = {}
        closes_by_code: dict[str, list[float]] = {c: [] for c in data}
        cash = float(cfg.seed)
        realized = 0.0
        trades: list[Trade] = []
        equity_curve: list[tuple[datetime, float]] = []
        wins = losses = 0

        # 시간축 병합
        all_events: list[tuple[datetime, str, Candle]] = []
        for code, cs in data.items():
            for c in cs:
                all_events.append((c.ts, code, c))
        all_events.sort(key=lambda x: x[0])

        for ts, code, candle in all_events:
            closes_by_code[code].append(candle.close)
            price = candle.close

            # 기존 포지션 관리
            pos = positions.get(code)
            if pos:
                pnl_ratio = pos.pnl_ratio(price)
                # 손절
                if pnl_ratio * 100 <= cfg.stop_loss_pct:
                    pnl = self._close(pos, price, cfg)
                    realized += pnl
                    cash += price * pos.qty * (1 - cfg.fee_rate - cfg.tax_rate)
                    trades.append(Trade(code, "SELL", price, pos.qty, ts,
                                        reason="SL", pnl=pnl, exit_reason=ExitReason.STOP_LOSS))
                    (wins if pnl > 0 else losses).__add__(1)
                    if pnl > 0: wins += 1
                    else: losses += 1
                    del positions[code]
                else:
                    # 분할 익절
                    for idx, (target, ratio) in enumerate(TAKE_PROFIT_LEVELS):
                        if idx in pos.tp_hit:
                            continue
                        if pnl_ratio >= target:
                            qty = max(1, int(pos.qty * ratio))
                            if qty >= pos.qty:
                                pnl = self._close(pos, price, cfg)
                                realized += pnl
                                cash += price * pos.qty * (1 - cfg.fee_rate - cfg.tax_rate)
                                trades.append(Trade(code, "SELL", price, pos.qty, ts,
                                                    reason=f"TP{idx+1}", pnl=pnl,
                                                    exit_reason=ExitReason.TAKE_PROFIT))
                                if pnl > 0: wins += 1
                                else: losses += 1
                                del positions[code]
                                break
                            else:
                                pnl = (price - pos.entry_price) * qty
                                realized += pnl
                                cash += price * qty * (1 - cfg.fee_rate - cfg.tax_rate)
                                pos.qty -= qty
                                pos.realized_pnl += pnl
                                pos.tp_hit.add(idx)
                                trades.append(Trade(code, "SELL", price, qty, ts,
                                                    reason=f"TP{idx+1}", pnl=pnl,
                                                    exit_reason=ExitReason.TAKE_PROFIT))
                                if pnl > 0: wins += 1

            # 신규 진입
            if code not in positions and len(positions) < cfg.max_concurrent:
                sig = evaluate_buy(code, closes_by_code[code], ts)
                if sig:
                    budget = cfg.seed * cfg.weight_per_stock
                    qty = int(budget // price)
                    cost = price * qty * (1 + cfg.fee_rate)
                    if qty > 0 and cost <= cash:
                        cash -= cost
                        positions[code] = Position(code=code, entry_price=price, qty=qty, opened_at=ts)
                        trades.append(Trade(code, "BUY", price, qty, ts, reason=sig.reason))

            # equity 기록
            mv = sum(positions[c].qty * closes_by_code[c][-1] for c in positions if closes_by_code[c])
            equity_curve.append((ts, cash + mv + realized))

        final_equity = equity_curve[-1][1] if equity_curve else cfg.seed
        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            final_equity=final_equity,
            total_return_pct=(final_equity - cfg.seed) / cfg.seed * 100.0,
            win_rate=(wins / (wins + losses) * 100.0) if (wins + losses) else 0.0,
            mdd_pct=self._mdd(equity_curve),
            num_trades=sum(1 for t in trades if t.side == "SELL"),
        )

    @staticmethod
    def _close(pos: Position, price: float, cfg: BacktestConfig) -> float:
        gross = (price - pos.entry_price) * pos.qty
        fee = price * pos.qty * (cfg.fee_rate + cfg.tax_rate)
        return gross - fee

    @staticmethod
    def _mdd(curve: list[tuple[datetime, float]]) -> float:
        if not curve:
            return 0.0
        peak = curve[0][1]
        mdd = 0.0
        for _, eq in curve:
            peak = max(peak, eq)
            dd = (eq - peak) / peak * 100.0 if peak > 0 else 0.0
            mdd = min(mdd, dd)
        return mdd
