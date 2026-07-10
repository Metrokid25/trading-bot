"""gm_v3 데이터 모델 — 일봉, 시그널, 종목별 상태.

엔진은 종목당 StockState 하나를 유지하며 evaluate_day() 가 봉마다 갱신한다.
포지션 체결(open/add/close)은 러너 책임 — 엔진은 시그널만 낸다 (절대 제약 3항).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from enum import Enum


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: Date
    open: float
    high: float
    low: float
    close: float
    volume: float


class SignalType(str, Enum):
    BUY = "BUY"      # 진입 (weight = 목표 비중 대비 투입 비율)
    SELL = "SELL"    # 청산 (weight = 보유 대비 청산 비율, 1.0 = 전량)
    WATCH = "WATCH"  # R3 급등 → 눌림 대기 등록 (정보성)
    MARK = "MARK"    # R5 거래량 마름 분할매수 후보 마킹 (정보성, 매수 아님)
    HOLD = "HOLD"    # R11 홀딩 플래그 발동 (정보성)


#: SELL 시그널 우선순위 (숫자 낮을수록 먼저 적용; 러너가 사용)
#: 손절(R10) > 구조 손절(R16) > 위험 청산(R9) > 반전캔들(R15) > 어깨(R7)
#: > 목표 분할(R8) > 목표격자(R14)
SELL_PRIORITY = {"R10": 0, "R16": 1, "R9": 2, "R15": 3, "R7": 4, "R8": 5, "R14": 6}


@dataclass(frozen=True, slots=True)
class Signal:
    day: Date
    stock_code: str
    type: SignalType
    rule: str                 # 'R1' ~ 'R12'
    weight: float             # BUY: 투입 비중 / SELL: 청산 비율 / 정보성: 0
    price: float              # 참조 가격(종가) 또는 트리거 가격(R10 손절선)
    reason: dict = field(default_factory=dict)   # 근거 수치 (로깅용)


@dataclass(slots=True)
class Position:
    """페이퍼 포지션 뷰 — 러너가 체결 후 갱신."""
    entry_avg: float          # 평균 진입가
    invested: float           # 목표 비중 대비 투입률 (0~1)
    opened_on: Date
    peak: float = 0.0         # 보유 중 최고가 (엔진이 매 봉 갱신)
    r8_done: bool = False     # R8 목표가 분할매도 1회 소진 여부
    # R14 목표격자: 보유 첫 평가 봉에서 산출·고정하는 저항 레벨 (낮은 것부터).
    # 도달·소비된 레벨은 pop — 재발화 방지. None = 미초기화 / [] = 레벨 없음/소진.
    r14_levels: list[float] | None = None


@dataclass(slots=True)
class WatchState:
    """R3 급등 → R4 눌림 대기 상태."""
    started_on: Date
    watermark: float                       # 급등 이후 최고가
    zone_reached: bool = False             # 눌림이 -min% 존에 도달했는가
    pullback_vols: list[float] = field(default_factory=list)
    age: int = 0                           # 등록 후 경과 거래일


@dataclass(slots=True)
class StockState:
    """종목별 엔진 상태. bars 는 evaluate_day 가 append."""
    code: str
    bars: list[DailyBar] = field(default_factory=list)
    position: Position | None = None
    watch: WatchState | None = None
    hold_until: int = -1      # R11: 이 bar 인덱스까지 R7 유예
    used_pivot_i: int = -1    # R1 이 소비한 피벗 인덱스 (같은 피벗 재발화 방지)
    r13_last_i: int = -10**9  # R13 마지막 매수 봉 인덱스 (쿨다운)
    ma20_broken_i: int = -1   # R16: 20일선 이탈 감지 봉 인덱스 (-1=정상)

    # ---- 러너용 체결 헬퍼 (페이퍼 전용) ----
    def apply_buy(self, price: float, weight: float, day: Date) -> None:
        if self.position is None:
            self.position = Position(entry_avg=price, invested=weight,
                                     opened_on=day, peak=price)
        else:
            # invested 는 자본 비중 → 주수 = w/p, 평단은 조화평균:
            # avg = Σw / Σ(wᵢ/pᵢ)  (산술평균이면 수익률 회계가 왜곡됨)
            p = self.position
            total = p.invested + weight
            p.entry_avg = total / (p.invested / p.entry_avg + weight / price)
            p.invested = total

    def apply_sell(self, frac: float) -> None:
        if self.position is None:
            return
        if frac >= 1.0 or self.position.invested <= 0:
            self.position = None
            self.hold_until = -1
            self.ma20_broken_i = -1   # R16 플래그는 포지션 생애주기와 함께 종료
        else:
            self.position.invested *= (1.0 - frac)
