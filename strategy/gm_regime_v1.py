"""GM Regime v1 — 시장 상태를 진입 차단이 아닌 노출 비중으로 반영한다.

이 모듈은 주문이나 페이퍼 DB를 건드리지 않는 연구용 순수 계산 계층이다.
기존 전략이 선택한 포지션과 손익을 그대로 두고, 당일 시장 상태에 따라
투입 비중만 축소한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class RegimeSizingConfig:
    """기존 포트폴리오 노출 대비 시장 상태별 배수.

    risk_off=0.20은 검증판 GM-R01의 '고변동 구간 종가 기준 20%'를 연구
    후보로 옮긴 값이다. neutral=0.50은 중간 상태를 위한 구현 가설이며
    아카이브의 직접 정량 근거가 아니다.
    """

    risk_on: float = 1.00
    neutral: float = 0.50
    risk_off: float = 0.20
    # 워밍업/coverage가 부족하면 장세를 추측하지 않고 원 포지션을 유지한다.
    unknown: float = 1.00

    def validated(self) -> "RegimeSizingConfig":
        values = (self.risk_on, self.neutral, self.risk_off, self.unknown)
        if not all(0.0 <= value <= 1.0 for value in values):
            raise ValueError("regime exposure factors must be between 0 and 1")
        if not self.risk_on >= self.neutral >= self.risk_off:
            raise ValueError(
                "regime exposure factors must satisfy risk_on >= neutral >= risk_off"
            )
        return self

    def factor(self, regime: str) -> float:
        self.validated()
        try:
            return {
                "risk_on": self.risk_on,
                "neutral": self.neutral,
                "risk_off": self.risk_off,
                "unknown": self.unknown,
            }[regime]
        except KeyError as exc:
            raise ValueError(f"unknown market regime: {regime!r}") from exc


def size_portfolio_day(
    day_ret: float,
    selected: Iterable[dict],
    regime: str,
    cfg: RegimeSizingConfig | None = None,
) -> tuple[float, list[dict]]:
    """기존 공유현금 포트폴리오 결과를 시장 상태별 비중으로 축소한다.

    시그널을 탈락시키지 않으므로 risk_off에서도 관찰 표본이 유지된다.
    입력 행은 수정하지 않고 사본에 ``base_weight``, ``weight``, ``pnl``,
    ``regime``을 기록한다.
    """
    cfg = (cfg or RegimeSizingConfig()).validated()
    factor = cfg.factor(regime)
    sized: list[dict] = []
    for row in selected:
        item = dict(row)
        base_weight = float(row.get("weight", 0.0))
        base_pnl = float(row.get("pnl", base_weight * row.get("ret_net", 0.0)))
        item["base_weight"] = base_weight
        item["weight"] = base_weight * factor
        item["pnl"] = base_pnl * factor
        item["regime"] = regime
        item["exposure_factor"] = factor
        sized.append(item)
    return day_ret * factor, sized
