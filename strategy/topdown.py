"""사전 관측 데이터만 사용하는 시장 -> 업종 -> 종목 진입 허가 계층.

공식 지수/투자자 수급 이력이 없는 토스 분봉 백테스트에서도 검증할 수 있도록,
등록 유니버스의 전일까지 일봉으로 합성 시장 상태를 만든다. 당일 봉은 어떤
계산에도 포함하지 않으며, 실운영 전에는 KOSPI/KOSDAQ 및 확정 수급 축을 별도로
추가한다.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Mapping, Sequence

from strategy.gm_v3.models import DailyBar


@dataclass(frozen=True, slots=True)
class TopDownConfig:
    ma_days: int = 20
    slope_days: int = 5
    relative_days: int = 5
    market_breadth_min: float = 0.55
    market_rising_ma_min: float = 0.50
    market_min_score: int = 2
    sector_breadth_min: float = 0.50
    sector_min_stocks: int = 2


@dataclass(frozen=True, slots=True)
class GateDecision:
    day: date
    code: str
    allowed: bool
    regime: str
    market_score: int
    market_return: float
    market_breadth: float
    market_rising_ma: float
    sector_pass: bool
    stock_pass: bool


def _mean(values: list[float]) -> float:
    return fmean(values) if values else 0.0


def _sector_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


class TopDownGate:
    """종목/거래일별 진입 허가표.

    build()가 각 거래일 ``d``를 평가할 때 ``bar.day < d``인 봉만 사용한다.
    """

    def __init__(self, decisions: Mapping[tuple[str, date], GateDecision]):
        self.decisions = dict(decisions)
        self.days = sorted({day for _code, day in self.decisions})

    def decision(self, code: str, day: date) -> GateDecision | None:
        return self.decisions.get((code, day))

    def allows(self, code: str, day: date) -> bool:
        item = self.decision(code, day)
        return bool(item and item.allowed)

    def allows_next_session(self, code: str, signal_day: date) -> bool:
        """종가 신호 뒤 다음 세션 진입 허가.

        다음 거래일 결정은 ``signal_day`` 봉까지 포함한 확정 데이터로 계산된다.
        """
        i = bisect_right(self.days, signal_day)
        return i < len(self.days) and self.allows(code, self.days[i])

    @classmethod
    def build(
        cls,
        bars_by_code: Mapping[str, Sequence[DailyBar]],
        sectors_by_code: Mapping[str, Sequence[str]],
        days: Sequence[date],
        cfg: TopDownConfig | None = None,
    ) -> "TopDownGate":
        cfg = cfg or TopDownConfig()
        ordered = {
            code: sorted(bars, key=lambda b: b.day)
            for code, bars in bars_by_code.items()
        }
        decisions: dict[tuple[str, date], GateDecision] = {}

        for day in sorted(set(days)):
            stats: dict[str, dict[str, float | bool]] = {}
            for code, bars in ordered.items():
                hist = [b for b in bars if b.day < day]
                need = cfg.ma_days + cfg.slope_days
                if len(hist) < need:
                    continue
                closes = [float(b.close) for b in hist]
                ma_now = _mean(closes[-cfg.ma_days:])
                ma_then = _mean(
                    closes[-(cfg.ma_days + cfg.slope_days):-cfg.slope_days])
                rel_n = min(cfg.relative_days, len(closes) - 1)
                rel_ret = closes[-1] / closes[-1 - rel_n] - 1
                stats[code] = {
                    "above_ma": closes[-1] >= ma_now,
                    "rising_ma": ma_now >= ma_then,
                    "return": rel_ret,
                }

            market_returns = [float(v["return"]) for v in stats.values()]
            market_breadth = _mean(
                [1.0 if bool(v["above_ma"]) else 0.0 for v in stats.values()])
            market_rising = _mean(
                [1.0 if bool(v["rising_ma"]) else 0.0 for v in stats.values()])
            market_return = _mean(market_returns)
            market_score = (
                int(market_return > 0)
                + int(market_breadth >= cfg.market_breadth_min)
                + int(market_rising >= cfg.market_rising_ma_min)
            )
            if market_score >= cfg.market_min_score:
                regime = "risk_on"
            elif market_score == cfg.market_min_score - 1:
                regime = "neutral"
            else:
                regime = "risk_off"

            sector_members: dict[str, set[str]] = {}
            for code, sectors in sectors_by_code.items():
                for sector in sectors:
                    sector_members.setdefault(_sector_key(sector), set()).add(code)
            sector_ok: dict[str, bool] = {}
            for sector, members in sector_members.items():
                available = [stats[c] for c in members if c in stats]
                sec_ret = _mean([float(v["return"]) for v in available])
                sec_breadth = _mean(
                    [1.0 if bool(v["above_ma"]) else 0.0 for v in available])
                sector_ok[sector] = (
                    len(available) >= cfg.sector_min_stocks
                    and sec_ret > market_return
                    and sec_breadth >= cfg.sector_breadth_min
                )

            for code in ordered:
                stock = stats.get(code)
                stock_pass = bool(
                    stock
                    and stock["above_ma"]
                    and stock["rising_ma"]
                )
                code_sectors = sectors_by_code.get(code, ())
                sector_pass = any(
                    sector_ok.get(_sector_key(s), False) for s in code_sectors)
                allowed = (
                    market_score >= cfg.market_min_score
                    and sector_pass
                    and stock_pass
                )
                decisions[(code, day)] = GateDecision(
                    day=day,
                    code=code,
                    allowed=allowed,
                    regime=regime,
                    market_score=market_score,
                    market_return=market_return,
                    market_breadth=market_breadth,
                    market_rising_ma=market_rising,
                    sector_pass=sector_pass,
                    stock_pass=stock_pass,
                )
        return cls(decisions)
