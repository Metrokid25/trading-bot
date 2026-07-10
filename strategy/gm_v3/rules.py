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
    SELL_PRIORITY, DailyBar, Signal, SignalType, StockState, WatchState,
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


def _wave_up(bars: list[DailyBar], i: int, lookback: int,
             min_up: float) -> tuple[float, float] | None:
    """최근 상승 파동 (저점 L → 고점 H). 유의성 미달이면 None. (R13, p15-16)"""
    lo = max(0, i - lookback)
    win = bars[lo:i + 1]
    hi_j = max(range(len(win)), key=lambda j: win[j].high)
    if hi_j == 0:
        return None                    # 창 시작이 고점 = 하락만 있음
    high = win[hi_j].high
    low = min(b.low for b in win[:hi_j])
    if low <= 0 or high / low - 1 < min_up:
        return None
    return high, low


def _wave_down(bars: list[DailyBar], i: int, lookback: int,
               min_down: float) -> tuple[float, float] | None:
    """최근 하락 파동 (고점 H → 저점 L). 유의성 미달이면 None. (R14, p15-16)"""
    lo = max(0, i - lookback)
    win = bars[lo:i + 1]
    hi_j = max(range(len(win)), key=lambda j: win[j].high)
    tail = win[hi_j:]
    if len(tail) < 2:
        return None                    # 고점 직후 = 하락 파동 없음
    high = win[hi_j].high
    low = min(b.low for b in tail)
    if high <= 0 or 1 - low / high < min_down:
        return None
    return high, low


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

        # R16 이동평균 구조 손절 (Tier1 툴4, p16·19): 20일선 이탈 →
        # 유예 내 회복 실패 상태에서 60일선까지 이탈하면 전량 손절
        if cfg.r16_enabled:
            ma20 = _sma(bars, i, 20)
            ma60 = _sma(bars, i, 60)
            if ma20 is not None:
                if state.ma20_broken_i < 0:
                    if bar.close < ma20:
                        state.ma20_broken_i = i        # 이탈 감지 (경계 진입)
                elif bar.close > ma20:
                    state.ma20_broken_i = -1           # 회복 → 경계 해제
                elif (i - state.ma20_broken_i >= cfg.r16_recover_days
                      and ma60 is not None and bar.close < ma60):
                    sig(SignalType.SELL, "R16", 1.0, bar.close,
                        ma20=round(ma20, 1), ma60=round(ma60, 1),
                        broken_bars=i - state.ma20_broken_i)

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

        # R15 반전캔들 청산 (Tier1 툴3, p45-46)
        if cfg.r15_enabled and prev is not None:
            body = abs(bar.close - bar.open)
            upper_wick = bar.high - max(bar.open, bar.close)
            # (a) 긴 윗꼬리 = 비중축소 준비 경고 — 가장 먼저 오는 신호 (정보성)
            if body > 0 and upper_wick >= cfg.r15_wick_body_mult * body:
                sig(SignalType.MARK, "R15", 0.0, bar.close,
                    variant="a", note="윗꼬리 경고(비중축소 준비)",
                    wick_body=round(upper_wick / body, 2))
            # (b) 시초가 슛팅 후 음봉 마감 → 절반 이상 매도 (보편 원칙)
            if (bar.open >= prev.close * (1 + cfg.r15_shoot_gap_pct)
                    and bar.close < bar.open):
                sig(SignalType.SELL, "R15", cfg.r15_shoot_sell_frac, bar.close,
                    variant="b", gap=round(bar.open / prev.close - 1, 4))
            # (c) 음봉 거래량이 직전 거래량 초과 (수익 중) → 익절 후 관망
            elif (bar.close < bar.open and prev.volume > 0
                  and bar.volume > prev.volume * cfg.r15_vol_exceed_mult
                  and bar.close > pos.entry_avg):
                sig(SignalType.SELL, "R15", 1.0, bar.close,
                    variant="c", vol_ratio=round(bar.volume / prev.volume, 2))

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

        # R14 목표격자 익절 (Tier1 툴2, p15-16·40): 하락 파동 회복 레벨
        # (저점+1/3, 저점+1/2=중간값) 도달 후 종가 돌파 실패 시 일부 매도
        r14_popped: float | None = None      # 우선순위 패배 시 복원용
        if cfg.r14_enabled:
            if pos.r14_levels is None:      # 보유 첫 평가 봉에서 산출·고정
                wave = _wave_down(bars, i, cfg.r14_lookback_days,
                                  cfg.r14_min_downmove_pct)
                if wave:
                    high, low = wave
                    span = high - low
                    pos.r14_levels = sorted(
                        lvl for lvl in (low + span / 3, low + span / 2)
                        if lvl > bar.close)  # 현재가 위 저항만 유효
                else:
                    pos.r14_levels = []
            while pos.r14_levels and bar.high >= pos.r14_levels[0]:
                lvl = pos.r14_levels.pop(0)
                if bar.close < lvl:          # 저항 돌파 실패 → 일부 매도
                    r14_popped = lvl
                    sig(SignalType.SELL, "R14", cfg.r14_sell_frac, bar.close,
                        level=round(lvl, 1))
                    break                    # 하루 1레벨만 매도
                # 종가 강돌파 → 홀딩, 다음 레벨로 (소비만)

        # ---- 동일 봉 SELL 중재와 원샷 상태 정합 (리뷰 반영) ----
        # 러너는 우선순위 최상위 SELL 하나만 체결한다 — 밀려서 버려질 R8/R14
        # 시그널이 r8_done/레벨 소비를 남기면 그 매도가 영구 소실되므로 복원.
        sells = [s for s in out if s.type == SignalType.SELL]
        if len(sells) > 1:
            winner = min(sells,
                         key=lambda s: SELL_PRIORITY.get(s.rule, 9)).rule
            if winner != "R8" and any(s.rule == "R8" for s in sells):
                pos.r8_done = False
            if winner != "R14" and r14_popped is not None:
                pos.r14_levels.insert(0, r14_popped)

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
        if bar.high > w.watermark:      # 신고가 → 기준 갱신 + 눌림 구조 리셋
            w.watermark = bar.high
            w.zone_reached = False
            w.pullback_vols.clear()
        dd = bar.close / w.watermark - 1
        if dd <= -cfg.r4_pullback_max_pct or w.age > cfg.r4_watch_expiry_days:
            state.watch = None          # 너무 깊은 눌림/만료 → 구조 무효
        else:
            if dd <= -cfg.r4_pullback_min_pct:
                w.zone_reached = True
            rebreak = (w.zone_reached and w.pullback_vols
                       and bar.close > prev.high
                       and bar.close < w.watermark)   # 고점 위 추격 금지
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
    # 같은 봉에서 R4 가 이미 매수했으면 스킵 — 동일봉 이중 진입(선발대 0.4) 방지
    already_bought = any(s.type == SignalType.BUY for s in out)
    if cfg.r1_enabled and not entry_blocked and not already_bought:
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

    # R13 지지레벨 분할매수 (Tier1 툴1, p15-16·19): 상승 파동 조정 중
    # 되돌림 30/50% 또는 이동평균(20일선, 붕괴 시 60일선) 지지 + 거래량 축소 + 양봉 확인
    already_bought = any(s.type == SignalType.BUY for s in out)
    if (cfg.r13_enabled and not entry_blocked and not already_bought
            and i - state.r13_last_i > cfg.r13_cooldown_days):
        wave = _wave_up(bars, i, cfg.r13_lookback_days, cfg.r13_min_upmove_pct)
        vol_ok = (vol_trend == "drying" if cfg.r13_require_drying
                  else vol_trend != "rising")   # 하락 중 거래량 증가 시 진입 금지 (p8-9)
        if wave and vol_ok and bar.close > bar.open:
            high, low = wave
            span = high - low
            levels = [("r30", high - 0.30 * span), ("r50", high - 0.50 * span)]
            ma20 = _sma(bars, i, 20)
            ma60 = _sma(bars, i, 60)
            if ma20 is not None:
                if bar.close < ma20:
                    if ma60 is not None:
                        levels.append(("ma60", ma60))  # 20일선 붕괴 → 60일선 지지
                else:
                    levels.append(("ma20", ma20))
            tol = cfg.r13_level_tol_pct
            hit = next(((nm, lv) for nm, lv in levels
                        if lv > 0 and bar.low <= lv * (1 + tol)
                        and bar.close >= lv), None)
            if hit is not None and bar.close < high:   # 고점 위 추격 금지
                wgt = buy_weight()
                if wgt > 0:
                    state.r13_last_i = i
                    sig(SignalType.BUY, "R13", wgt, bar.close,
                        level=hit[0], level_px=round(hit[1], 1),
                        wave_high=high, wave_low=low)

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
