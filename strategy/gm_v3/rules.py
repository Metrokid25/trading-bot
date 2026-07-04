"""gm_v3 룰 엔진 — 일봉 스트림을 받아 종목별 BUY/SELL/정보성 시그널 생성.

설계 원칙:
  - 룩어헤드 금지: 봉 i 판정에 i 이후 정보 사용 불가. 스윙 고점은 좌우 k봉이
    지나야 '확정'되는 프랙탈 피벗(확정 지연 = k일)만 사용.
  - 시그널은 종가 확정 후 발동으로 간주(R1 이 종가 기준 조건). 체결 가정
    (다음날 시가/당일 종가)은 백테스트 러너 몫.
  - R10 손절은 예외 없음: R11 홀딩 예외도 R10 을 건드리지 못한다.

분봉 근사 TODO: R9(b) '장초반 급등 후 아래꼬리'는 일봉 프록시(고가/전일종가 +
아래꼬리/몸통 배율)로 구현. pick_minute_raw 축적 후 분봉 정밀 버전으로 교체.
"""
from __future__ import annotations

from strategy.gm_v3.config import GmV3Config
from strategy.gm_v3.models import (
    DailyBar, Signal, SignalType, StockState, WatchState,
)


# ---------------- 지표 헬퍼 ----------------

def _sma(bars: list[DailyBar], i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(b.close for b in bars[i - n + 1:i + 1]) / n


def _vol_trend(bars: list[DailyBar], i: int, days: int,
               cfg: GmV3Config) -> str:
    """'rising' | 'drying' | 'neutral' — 최근 days 평균 vs 직전 days 평균."""
    if i + 1 < 2 * days:
        return "neutral"
    recent = sum(b.volume for b in bars[i - days + 1:i + 1]) / days
    prior = sum(b.volume for b in bars[i - 2 * days + 1:i - days + 1]) / days
    if prior <= 0:
        return "neutral"
    ratio = recent / prior
    if ratio >= cfg.r5_rising_ratio:
        return "rising"
    if ratio <= cfg.r5_dry_ratio:
        return "drying"
    return "neutral"


def _price_downtrend(bars: list[DailyBar], i: int, days: int) -> bool:
    return i >= days and bars[i].close < bars[i - days].close


def _latest_confirmed_pivot(state: StockState,
                            cfg: GmV3Config) -> tuple[int, float] | None:
    """가장 최근의 '확정' 스윙 고점 (인덱스, 고가).

    피벗 p: high[p] 가 좌우 k봉 모두의 고가보다 높음. p+k 봉이 지나야 확정.
    lookback 밖이거나 이미 R1 이 소비한 피벗은 제외.
    """
    bars = state.bars
    i = len(bars) - 1
    k = cfg.swing_pivot_k
    lo = max(k, i - cfg.swing_lookback_days)
    for p in range(i - k, lo - 1, -1):
        if p <= state.used_pivot_i:
            return None            # 더 과거는 전부 소비됐거나 의미 없음
        hp = bars[p].high
        if all(hp > bars[p - j].high for j in range(1, k + 1)) and \
           all(hp > bars[p + j].high for j in range(1, k + 1)):
            return p, hp
    return None


# ---------------- 룰 엔진 본체 ----------------

def evaluate_day(state: StockState, bar: DailyBar,
                 cfg: GmV3Config) -> list[Signal]:
    """봉 하나를 반영하고 발동 시그널 목록 반환. (엔진이 상태를 갱신한다)"""
    state.bars.append(bar)
    bars = state.bars
    i = len(bars) - 1
    prev = bars[i - 1] if i >= 1 else None
    out: list[Signal] = []

    def sig(type_, rule, weight, price, **reason):
        out.append(Signal(bar.day, state.code, type_, rule,
                          weight, price, reason))

    vol_trend = _vol_trend(bars, i, cfg.r5_vol_trend_days, cfg)

    # ============ 청산 룰 (보유 중일 때만) ============
    pos = state.position
    if pos is not None:
        pos.peak = max(pos.peak, bar.high)
        dd_peak = bar.close / pos.peak - 1 if pos.peak > 0 else 0.0

        # R10 손절 — 예외 없음, 최우선. 장중 저가 터치 기준.
        stop_px = pos.entry_avg * (1 - cfg.r10_stop_pct)
        if bar.low <= stop_px:
            sig(SignalType.SELL, "R10", 1.0, stop_px,
                entry_avg=pos.entry_avg, stop_pct=cfg.r10_stop_pct)
            return out           # 즉시 전량 청산 — 이후 룰 평가 불필요

        # R9(a) 폭등 다음날 전일 양봉 몸통보다 큰 장대음봉
        if cfg.r9_enabled and prev is not None:
            prev_prev = bars[i - 2] if i >= 2 else None
            prev_ret = (prev.close / prev_prev.close - 1) if prev_prev else 0.0
            prev_body = prev.close - prev.open
            today_body = bar.open - bar.close
            if (prev_ret >= cfg.r9_surge_pct and prev_body > 0
                    and today_body > prev_body):
                sig(SignalType.SELL, "R9", 1.0, bar.close,
                    variant="a", prev_ret=round(prev_ret, 4),
                    prev_body=prev_body, today_body=today_body)
        # R9(b) 장중 급등 후 긴 아래꼬리 + 거래량 급증 (일봉 근사)
        # TODO(minute): pick_minute_raw 축적 후 '장초반' 시각 조건 정밀화
        if cfg.r9_enabled and prev is not None and not any(
                s.rule == "R9" for s in out):
            body = abs(bar.close - bar.open)
            wick = min(bar.open, bar.close) - bar.low
            days = cfg.r5_vol_trend_days
            avg_vol = (sum(b.volume for b in bars[max(0, i - days):i]) /
                       max(1, min(days, i)))
            if (bar.high >= prev.close * (1 + cfg.r9b_surge_pct)
                    and body > 0 and wick >= cfg.r9b_wick_body_mult * body
                    and avg_vol > 0
                    and bar.volume >= cfg.r9b_vol_mult * avg_vol):
                sig(SignalType.SELL, "R9", 1.0, bar.close,
                    variant="b", wick=wick, body=body,
                    vol_ratio=round(bar.volume / avg_vol, 2))

        # R11 홀딩 예외: 거래량 감소 + 고점 대비 버팀 → R7 유예
        if (cfg.r11_enabled and vol_trend == "drying"
                and dd_peak >= -cfg.r11_hold_dd_pct):
            state.hold_until = i + cfg.r11_hold_days
            sig(SignalType.HOLD, "R11", 0.0, bar.close,
                dd_peak=round(dd_peak, 4), hold_until_bar=state.hold_until)

        # R7 어깨 매도(트레일링): 양봉 진행 중 금지, R11 유예 존중
        rising_candle = bar.close > bar.open and (
            prev is None or bar.close > prev.close)
        if (cfg.r7_enabled and dd_peak <= -cfg.r7_trail_pct
                and not rising_candle and i > state.hold_until):
            sig(SignalType.SELL, "R7", 1.0, bar.close,
                peak=pos.peak, dd_peak=round(dd_peak, 4))

        # R8 목표가 분할매도 (1회)
        if (cfg.r8_enabled and not pos.r8_done
                and bar.close >= pos.entry_avg * (1 + cfg.r8_target_pct)):
            pos.r8_done = True
            sig(SignalType.SELL, "R8", cfg.r8_sell_frac, bar.close,
                entry_avg=pos.entry_avg, target_pct=cfg.r8_target_pct)

    # ============ 진입 룰 ============
    if prev is None:
        return out

    day_ret = bar.close / prev.close - 1
    surge_today = day_ret >= cfg.r3_chase_pct

    # R3 추격매수 금지: 급등일 진입 차단 + 눌림 대기 등록
    entry_blocked = False
    if cfg.r3_enabled and surge_today:
        entry_blocked = True
        if state.watch is None:
            state.watch = WatchState(started_on=bar.day, watermark=bar.high)
            sig(SignalType.WATCH, "R3", 0.0, bar.close,
                day_ret=round(day_ret, 4))
        else:
            state.watch.watermark = max(state.watch.watermark, bar.high)
            state.watch.age = 0        # 급등 지속 → 대기 갱신

    # R5 하락 중 거래량 필터
    if cfg.r5_enabled and _price_downtrend(bars, i, cfg.r5_vol_trend_days):
        if vol_trend == "rising":
            entry_blocked = True       # 추가 하락 가능성 → 진입 전면 차단
        elif vol_trend == "drying":
            sig(SignalType.MARK, "R5", 0.0, bar.close,
                note="분할매수 후보(매도세 부재)", vol_trend=vol_trend)

    def buy_weight() -> float:
        """R6 분할매수 비중: 첫 진입 선발대 20%, 재신호 시에만 증액."""
        if state.position is None:
            return cfg.r6_scout_weight
        return min(cfg.r6_add_weight,
                   max(0.0, 1.0 - state.position.invested))

    # R4 눌림목 재진입 (급등 대기 종목)
    if state.watch is not None and not surge_today:
        w = state.watch
        w.age += 1
        dd = bar.close / w.watermark - 1
        if dd <= -cfg.r4_pullback_max_pct or w.age > cfg.r4_watch_expiry_days:
            state.watch = None          # 너무 깊은 눌림/만료 → 구조 무효
        else:
            if dd <= -cfg.r4_pullback_min_pct:
                w.zone_reached = True
            rebreak = (w.zone_reached and w.pullback_vols
                       and bar.close > prev.high)
            if rebreak and cfg.r4_enabled and not entry_blocked:
                avg_pb_vol = sum(w.pullback_vols) / len(w.pullback_vols)
                if avg_pb_vol > 0 and bar.volume >= cfg.r4_vol_mult * avg_pb_vol:
                    wgt = buy_weight()
                    if wgt > 0:
                        sig(SignalType.BUY, "R4", wgt, bar.close,
                            watermark=w.watermark, dd=round(dd, 4),
                            vol_ratio=round(bar.volume / avg_pb_vol, 2))
                    state.watch = None
            if state.watch is not None:
                w.pullback_vols.append(bar.volume)

    # R1 무릎 매수: 확정 피벗 고점 종가 첫 돌파 (+ R2 보수 필터)
    if cfg.r1_enabled and not entry_blocked:
        pivot = _latest_confirmed_pivot(state, cfg)
        if pivot is not None:
            p_i, p_high = pivot
            first_break = bar.close > p_high and prev.close <= p_high
            if first_break:
                r2_ok = True
                if cfg.r2_trend_filter_enabled:
                    ma5, ma20, ma60 = (_sma(bars, i, 5), _sma(bars, i, 20),
                                       _sma(bars, i, 60))
                    r2_ok = (ma5 is not None and ma20 is not None
                             and ma60 is not None and ma5 > ma20 > ma60)
                if r2_ok:
                    wgt = buy_weight()
                    if wgt > 0:
                        state.used_pivot_i = p_i
                        sig(SignalType.BUY, "R1", wgt, bar.close,
                            pivot_high=p_high, pivot_index=p_i,
                            r2_filter=cfg.r2_trend_filter_enabled)

    return out


# ---------------- R12 포트폴리오 룰 ----------------

def liquidation_order(
        holdings: list[tuple[str, float, float]]) -> list[tuple[str, float]]:
    """R12 손실 종목 우선 정리 순서.

    holdings: [(종목코드, 평균진입가, 현재가)] → [(종목코드, 수익률)] 을
    손실 큰 순으로 정렬해 반환. (포트 축소는 수동 트리거로 시작 — 스펙)
    """
    ranked = [(code, cur / entry - 1) for code, entry, cur in holdings
              if entry > 0]
    ranked.sort(key=lambda x: x[1])
    return ranked
