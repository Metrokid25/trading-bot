"""strategy_gm_v3 — 멘토 매매 원칙 기반 룰 엔진 (시그널 생성 전용).

기존 v1~v4/acc 백테스트, Phase 2.5 수집 로직과 완전 격리된 신규 모듈.
실주문 코드 없음 — 시그널 생성 + 로깅 + 페이퍼 검증까지만 (절대 제약 3항).
"""
from strategy.gm_v3.config import GmV3Config
from strategy.gm_v3.models import DailyBar, Signal, SignalType, StockState
from strategy.gm_v3.rules import evaluate_day, liquidation_order

__all__ = ["GmV3Config", "DailyBar", "Signal", "SignalType", "StockState",
           "evaluate_day", "liquidation_order"]
