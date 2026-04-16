"""이벤트 기반 백테스트 엔진.

새 전략: 3분봉 멀티TF 시그널 + ATR 기반 손절/분할익절/트레일링 + VWAP/MACD 청산.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config import settings
from config.constants import (
    ATR_STOP_MULT,
    ATR_TP_MULTS,
    ATR_TP_RATIOS,
    ATR_TRAILING_TRIGGER,
    TP_STOP_BUFFER_ATR,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MAX_POSITION_PCT,
    RISK_PER_TRADE_PCT,
    VOLUME_LOOKBACK,
    VOLUME_SURGE_MULT,
    ExitReason,
)
from data.candle_store import CandleBuffer
from data.models import Candle, Position, Trade
from strategy.indicators import macd_hist_series, vwap
from strategy.signal import evaluate_buy


@dataclass
class BacktestConfig:
    seed: int = 10_000_000
    max_concurrent: int = 3
    fee_rate: float = 0.00015
    tax_rate: float = 0.0018  # 매도 시
    risk_per_trade: float = RISK_PER_TRADE_PCT
    max_position_pct: float = MAX_POSITION_PCT
    eligible_codes: set[str] | None = None  # None = 전체 허용, 아니면 이 집합만 진입
    allow_breakout: bool = False              # v5: BREAKOUT 시그널 채널 추가
    atr_sizing_mode: str = "fixed"            # v6 step1: "fixed" | "dynamic"
    quality_sizing_mode: str = "off"          # v6 step2: "off" | "on"


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

    def _size(
        self,
        price: float,
        atr_val: float,
        atr_avg20: float | None = None,
        signal_quality: float | None = None,
    ) -> int:
        if price <= 0 or atr_val <= 0:
            return 0
        risk = self.cfg.seed * self.cfg.risk_per_trade
        stop_dist = atr_val * ATR_STOP_MULT
        qty_risk = int(risk // stop_dist)
        qty_cap = int((self.cfg.seed * self.cfg.max_position_pct) // price)
        qty = max(0, min(qty_risk, qty_cap))
        # v6 step1: 동적 ATR 사이징 — atr_avg20 / current_atr 비율로 승수 적용.
        if (
            self.cfg.atr_sizing_mode == "dynamic"
            and atr_avg20 is not None
            and atr_avg20 > 0
        ):
            atr_mult = max(0.5, min(2.0, atr_avg20 / atr_val))
            qty = int(qty * atr_mult)
            qty = min(qty, qty_cap)
        # v6 step2: 시그널 품질 사이징 — 0.5 ~ 1.5 승수 곱.
        if (
            self.cfg.quality_sizing_mode == "on"
            and signal_quality is not None
        ):
            quality_mult = 0.5 + float(signal_quality)
            qty = int(qty * quality_mult)
            qty = min(qty, qty_cap)
        return max(0, qty)

    def run(self, data: dict[str, list[Candle]]) -> BacktestResult:
        cfg = self.cfg
        positions: dict[str, Position] = {}
        buffers: dict[str, CandleBuffer] = {c: CandleBuffer(c) for c in data}
        cash = float(cfg.seed)
        realized = 0.0
        trades: list[Trade] = []
        equity_curve: list[tuple[datetime, float]] = []
        wins = losses = 0

        all_events: list[tuple[datetime, str, Candle]] = []
        for code, cs in data.items():
            for c in cs:
                all_events.append((c.ts, code, c))
        all_events.sort(key=lambda x: x[0])

        for ts, code, candle in all_events:
            buf = buffers[code]
            # 봉을 직접 append (틱 재생 생략, 종가 기준)
            buf.closed.append(candle)
            price = candle.close
            pos = positions.get(code)

            # --- 포지션 관리 ---
            if pos:
                # 손절
                if price <= pos.stop_price:
                    reason = ExitReason.TRAIL_STOP if pos.trailing_activated else ExitReason.STOP_LOSS
                    pnl = self._close_pnl(pos, price, cfg)
                    cash += price * pos.qty * (1 - cfg.fee_rate - cfg.tax_rate)
                    realized += pnl
                    trades.append(Trade(
                        code, "SELL", price, pos.qty, ts,
                        reason=reason.value, pnl=pnl, exit_reason=reason,
                        atr=pos.atr, stop_price=pos.stop_price, tp_prices=tuple(pos.tp_prices),
                    ))
                    (wins if pnl > 0 else losses).__add__(1)
                    if pnl > 0: wins += 1
                    else: losses += 1
                    del positions[code]
                    pos = None

                if pos:
                    # 트레일링 본절
                    if not pos.trailing_activated and pos.atr > 0:
                        if price >= pos.entry_price + pos.atr * ATR_TRAILING_TRIGGER:
                            pos.trailing_activated = True
                            if pos.entry_price > pos.stop_price:
                                pos.stop_price = pos.entry_price

                    # 분할 익절
                    for idx, (tp_price, ratio) in enumerate(zip(pos.tp_prices, ATR_TP_RATIOS)):
                        if idx in pos.tp_hit or pos is None:
                            continue
                        if price >= tp_price:
                            if ratio >= 1.0 or int(pos.qty * ratio) >= pos.qty:
                                pnl = (price - pos.entry_price) * pos.qty
                                cash += price * pos.qty * (1 - cfg.fee_rate - cfg.tax_rate)
                                realized += pnl
                                trades.append(Trade(
                                    code, "SELL", price, pos.qty, ts,
                                    reason=f"TP{idx+1}", pnl=pnl,
                                    exit_reason=ExitReason.TAKE_PROFIT,
                                    atr=pos.atr, stop_price=pos.stop_price,
                                    tp_prices=tuple(pos.tp_prices),
                                ))
                                if pnl > 0: wins += 1
                                else: losses += 1
                                del positions[code]
                                pos = None
                                break
                            else:
                                qty = max(1, int(pos.qty * ratio))
                                pnl = (price - pos.entry_price) * qty
                                cash += price * qty * (1 - cfg.fee_rate - cfg.tax_rate)
                                realized += pnl
                                pos.qty -= qty
                                pos.realized_pnl += pnl
                                pos.tp_hit.add(idx)
                                new_stop = tp_price - pos.atr * TP_STOP_BUFFER_ATR
                                if new_stop > pos.stop_price:
                                    pos.stop_price = new_stop
                                trades.append(Trade(
                                    code, "SELL", price, qty, ts,
                                    reason=f"TP{idx+1}", pnl=pnl,
                                    exit_reason=ExitReason.TAKE_PROFIT,
                                    atr=pos.atr, stop_price=pos.stop_price,
                                    tp_prices=tuple(pos.tp_prices),
                                ))
                                if pnl > 0: wins += 1

                # VWAP/MACD 청산시그널 (봉 마감 시)
                if pos:
                    prev_qty = pos.qty
                    pos = self._check_exit_signal(code, buf, ts, pos, positions, trades, cfg)
                    if pos is None:
                        cash += price * prev_qty * (1 - cfg.fee_rate - cfg.tax_rate)

            # --- 신규 진입 (eligible_codes 게이트 적용) ---
            if code not in positions and len(positions) < cfg.max_concurrent:
                eligible = cfg.eligible_codes is None or code in cfg.eligible_codes
                sig = (evaluate_buy(code, buf, ts, allow_breakout=cfg.allow_breakout)
                       if eligible else None)
                if sig:
                    atr_val = float(sig.meta.get("atr", 0.0) or 0.0)
                    kind = sig.meta.get("kind", "PULLBACK")
                    atr_avg20 = sig.meta.get("atr_avg20")
                    signal_quality = sig.meta.get("signal_quality")
                    qty = self._size(price, atr_val, atr_avg20, signal_quality)
                    cost = price * qty * (1 + cfg.fee_rate)
                    if qty > 0 and cost <= cash and atr_val > 0:
                        cash -= cost
                        stop_price = price - atr_val * ATR_STOP_MULT
                        tp_prices = [price + atr_val * m for m in ATR_TP_MULTS]
                        positions[code] = Position(
                            code=code, entry_price=price, qty=qty, opened_at=ts,
                            atr=atr_val, stop_price=stop_price, tp_prices=tp_prices,
                        )
                        trades.append(Trade(
                            code, "BUY", price, qty, ts,
                            reason=f"[{kind}] {sig.reason}", atr=atr_val,
                            stop_price=stop_price, tp_prices=tuple(tp_prices),
                        ))

            # equity 기록 (미실현 + cash)
            mv = 0.0
            for c, p in positions.items():
                last = buffers[c].closed[-1].close if buffers[c].closed else p.entry_price
                mv += last * p.qty
            equity_curve.append((ts, cash + mv))

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

    def _check_exit_signal(self, code, buf, ts, pos, positions, trades, cfg):
        candles = buf.candles()
        if len(candles) < max(MACD_SLOW + MACD_SIGNAL, VOLUME_LOOKBACK + 2):
            return pos
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        vols = [c.volume for c in candles]
        today = candles[-1].ts.date()
        sess = next((i for i, c in enumerate(candles) if c.ts.date() == today), len(candles) - 1)
        vwap3 = vwap(highs[sess:], lows[sess:], closes[sess:], vols[sess:])
        hist = macd_hist_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        price = closes[-1]
        vol_window = vols[-(VOLUME_LOOKBACK + 1):-1]
        avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0

        exit_reason: ExitReason | None = None
        two_bar_break = len(closes) >= 2 and closes[-1] < vwap3 and closes[-2] < vwap3
        if two_bar_break and avg_vol > 0 and vols[-1] >= avg_vol * VOLUME_SURGE_MULT:
            exit_reason = ExitReason.VWAP_BREAK
        elif len(hist) >= 2 and hist[-2] > 0 >= hist[-1]:
            exit_reason = ExitReason.MACD_FLIP

        if exit_reason:
            pnl = (price - pos.entry_price) * pos.qty + pos.realized_pnl
            trades.append(Trade(
                code, "SELL", price, pos.qty, ts,
                reason=exit_reason.value, pnl=pnl, exit_reason=exit_reason,
                atr=pos.atr, stop_price=pos.stop_price, tp_prices=tuple(pos.tp_prices),
            ))
            del positions[code]
            return None
        return pos

    @staticmethod
    def _close_pnl(pos: Position, price: float, cfg: BacktestConfig) -> float:
        gross = (price - pos.entry_price) * pos.qty
        fee = price * pos.qty * (cfg.fee_rate + cfg.tax_rate)
        return gross - fee + pos.realized_pnl

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
