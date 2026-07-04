"""gm_v3 룰 엔진 설정 — 모든 임계값·on/off 플래그 (하드코딩 금지 원칙).

값 단위: *_pct 는 소수 비율(0.10 = 10%), *_days 는 거래일 수.
"""
from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class GmV3Config:
    # R1 무릎 매수: 직전 스윙 고점 종가 돌파
    r1_enabled: bool = True
    swing_lookback_days: int = 20     # 스윙 고점 탐지 lookback (튜닝 대상)
    swing_pivot_k: int = 3            # 피벗 확인 좌우 봉 수 (확정 지연 = k일)

    # R2 보수적 진입 확인: 골든크로스 + 정배열(5>20>60)일 때만 R1 유효
    r2_trend_filter_enabled: bool = False   # True = 보수 모드 / False = 공격 모드

    # R3 추격매수 금지: 당일 급등 종목 신규 진입 차단 → 눌림 대기 등록
    r3_enabled: bool = True
    r3_chase_pct: float = 0.10

    # R4 눌림목 재진입: 고점 대비 -min~-max 눌림 + 거래량 동반 재돌파
    r4_enabled: bool = True
    r4_pullback_min_pct: float = 0.03
    r4_pullback_max_pct: float = 0.08
    r4_vol_mult: float = 2.0          # 재돌파 거래량 ≥ 조정기 평균 × N
    r4_watch_expiry_days: int = 10    # 눌림 대기 유효 기간(재돌파 없으면 해제)

    # R5 하락 중 거래량 필터
    r5_enabled: bool = True
    r5_vol_trend_days: int = 5        # 가격/거래량 추세 판정 기간
    r5_rising_ratio: float = 1.2      # 최근/직전 평균 거래량 ≥ 이 값 → 증가 추세(차단)
    r5_dry_ratio: float = 0.8         # 최근/직전 평균 거래량 ≤ 이 값 → 축소 추세(후보 마킹)

    # R6 분할매수 비중
    r6_scout_weight: float = 0.2      # 1차 진입(선발대) 비중 (< 20% 원칙)
    r6_add_weight: float = 0.2        # 추가매수 1회당 비중 (R1/R4 재발생 시만)

    # R7 어깨 매도(트레일링): 보유 최고가 대비 하락 시. 양봉 진행 중 금지
    r7_enabled: bool = True
    r7_trail_pct: float = 0.05

    # R8 목표가 분할매도
    r8_enabled: bool = True
    r8_target_pct: float = 0.10
    r8_sell_frac: float = 0.5

    # R9 위험 신호 즉시 청산
    r9_enabled: bool = True
    r9_surge_pct: float = 0.10        # (a) '폭등'의 전일 등락 기준
    r9b_wick_body_mult: float = 2.0   # (b) 아래꼬리 ≥ 몸통 × N
    r9b_vol_mult: float = 2.0         # (b) 거래량 ≥ 최근 평균 × N
    r9b_surge_pct: float = 0.05       # (b) '장초반 급등' 일봉 근사: 고가 ≥ 전일종가×(1+x)

    # R10 손절 (예외 없음)
    r10_stop_pct: float = 0.04

    # R11 홀딩 예외: 급등 후 거래량 감소 + 가격 버팀 → R7 일시 완화
    r11_enabled: bool = True
    r11_hold_dd_pct: float = 0.02     # 고점 대비 이 이내로 버티면 홀딩 플래그
    r11_hold_days: int = 5            # 플래그 지속 거래일 (동안 R7 트레일링 유예, R10 은 불가침)

    def validated(self) -> "GmV3Config":
        """단순 무결성 체크 후 자신 반환 (엔진 진입 전 1회 호출)."""
        assert 0 < self.r6_scout_weight < 0.2001, "선발대 비중은 20% 미만 원칙(R6)"
        assert self.r4_pullback_min_pct < self.r4_pullback_max_pct
        assert self.swing_pivot_k >= 1 and self.swing_lookback_days > self.swing_pivot_k
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name.endswith("_pct"):
                assert 0 <= v < 1, f"{f.name} 은 소수 비율이어야 함(예: 0.10)"
        return self
