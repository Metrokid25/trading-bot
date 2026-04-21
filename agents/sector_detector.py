"""섹터 쏠림 감지기 (Phase 2 Stage 1: 조건 A + B).

흐름:
 1. active sector picks 로드
 2. 섹터별 종목을 세마포어로 직렬화된 병렬 호출
    - 1분봉 30개 조회 → 현재봉/직전 N봉 거래량 평균 계산
    - 당일 시가(_fetch_day_open) → 캐시 miss 시 일봉 1행 조회
 3. 조건 A: 거래량≥(시간대별 배수)×avg, 당일 시가 대비 수익률≥(임계), 양봉
 4. 조건 B: 같은 섹터 내 A 통과 ≥ SECTOR_B_MIN_PASSED
 5. sector_store.should_alert 로 쿨다운 판정 → 통과 시 DB 기록 + 텔레그램

설계 결정:
 - 쿨다운은 DB(alert_history) 기반. 재시작/다중 워커에도 일관.
 - 일봉 시가는 장중 불변 → (yyyymmdd, open) 메모리 캐시.
 - KIS 동시 호출은 asyncio.Semaphore(8)로 제한 (v6 래퍼에 리미터 없음).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

from loguru import logger

from config import constants as C
from core.kis_api import KISClient
from core.telegram_bot import TelegramBot
from data.sector_models import SectorStock
from data.sector_store import SectorStore

_KIS_CONCURRENCY = 8


class SectorDetector:
    def __init__(
        self,
        kis: KISClient,
        sector_store: SectorStore,
        telegram: TelegramBot,
    ) -> None:
        self.kis = kis
        self.sector_store = sector_store
        self.telegram = telegram
        self._sema = asyncio.Semaphore(_KIS_CONCURRENCY)
        # {code: (yyyymmdd, day_open)}  — 날짜 바뀌면 miss 처리
        self._day_open_cache: dict[str, tuple[str, float]] = {}

    # ---------- 시간대 ----------
    def pick_thresholds(self, now: datetime) -> dict[str, float]:
        t = now.time()
        vol_mult = C.SECTOR_A_VOL_MULT_DEFAULT
        ret_thr = C.SECTOR_A_RETURN_DEFAULT
        if t < C.SECTOR_EARLY_END:
            vol_mult = C.SECTOR_A_VOL_MULT_EARLY
            ret_thr = C.SECTOR_A_RETURN_EARLY
        elif t >= C.SECTOR_LATE_START:
            vol_mult = C.SECTOR_A_VOL_MULT_LATE
        return {"vol_mult": vol_mult, "return": ret_thr}

    def is_blocked_window(self, now: datetime) -> bool:
        t = now.time()
        if t < C.MARKET_OPEN or t >= C.MARKET_CLOSE:
            return True
        if C.SECTOR_BLOCK_START <= t < C.SECTOR_BLOCK_END:
            return True
        return False

    # ---------- 당일 시가 (캐시) ----------
    async def _fetch_day_open(self, code: str) -> float | None:
        today = datetime.now().strftime("%Y%m%d")
        cached = self._day_open_cache.get(code)
        if cached and cached[0] == today and cached[1] > 0:
            return cached[1]

        async with self._sema:
            try:
                rows = await self.kis.get_daily_candles(code, today, today, "D")
            except Exception as e:
                logger.warning(f"[sector] {code} 일봉 조회 실패: {e}")
                return None

        # 응답이 오늘 1행이 아닐 수도 있으니 날짜 매칭으로 선택
        for r in rows:
            if r.get("stck_bsop_date") == today:
                try:
                    op = float(r.get("stck_oprc") or 0)
                    if op > 0:
                        self._day_open_cache[code] = (today, op)
                        return op
                except (TypeError, ValueError):
                    pass
        # 오늘 봉 없으면 가장 최근 행의 시가를 fallback
        if rows:
            try:
                op = float(rows[0].get("stck_oprc") or 0)
                if op > 0:
                    # 날짜 미스매치는 캐시하지 않음 (다음 스캔에서 재시도)
                    logger.debug(f"[sector] {code} 오늘 일봉 없음 — fallback={op}")
                    return op
            except (TypeError, ValueError):
                pass
        return None

    # ---------- 조건 A ----------
    async def evaluate_stock(
        self,
        code: str,
        thresholds: dict[str, float],
    ) -> tuple[bool, dict[str, Any]]:
        """종목 1개에 대해 조건 A 판정. (passed, metrics)."""
        async with self._sema:
            try:
                bars = await self.kis.get_minute_candles(code, interval="1")
            except Exception as e:
                logger.warning(f"[sector] {code} 1분봉 조회 실패: {e}")
                return False, {"error": str(e)}

        if len(bars) < C.VOLUME_LOOKBACK + 1:
            return False, {"reason": "insufficient_bars", "n": len(bars)}

        # KIS output2: 최신봉 [0], 직전 N봉 [1:1+LOOKBACK]
        cur = bars[0]
        try:
            cur_open = float(cur.get("stck_oprc") or 0)
            cur_close = float(cur.get("stck_prpr") or 0)
            cur_vol = float(cur.get("cntg_vol") or 0)
        except (TypeError, ValueError) as e:
            return False, {"reason": "parse_error", "error": str(e)}

        past = bars[1:1 + C.VOLUME_LOOKBACK]
        vols = [float(b.get("cntg_vol") or 0) for b in past]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 0.0

        day_open = await self._fetch_day_open(code)
        if not day_open:
            return False, {"reason": "no_day_open"}
        day_return = (cur_close - day_open) / day_open
        is_bullish = cur_close > cur_open

        metrics = {
            "vol_ratio": round(vol_ratio, 2),
            "return": round(day_return, 4),
            "bullish": is_bullish,
            "close": cur_close,
            "open": cur_open,
            "day_open": day_open,
        }
        passed = (
            vol_ratio >= thresholds["vol_mult"]
            and day_return >= thresholds["return"]
            and is_bullish
        )
        logger.debug(
            f"[sector] A-check {code}: passed={passed} "
            f"vol×{vol_ratio:.2f} (≥{thresholds['vol_mult']}) "
            f"ret={day_return*100:+.2f}% (≥{thresholds['return']*100:.1f}%) "
            f"bull={is_bullish}"
        )
        return passed, metrics

    # ---------- 1회 스캔 ----------
    async def scan_once(self) -> None:
        now = datetime.now()
        if self.is_blocked_window(now):
            logger.debug(f"[sector] blocked window: {now.time()}")
            return

        picks = await self.sector_store.get_active_picks()
        if not picks:
            logger.debug("[sector] active pick 없음 — 스캔 스킵")
            return

        thresholds = self.pick_thresholds(now)
        logger.info(
            f"[sector] scan @ {now.isoformat(timespec='seconds')}, "
            f"picks={len(picks)}, vol×{thresholds['vol_mult']}, "
            f"ret≥{thresholds['return']*100:.1f}%"
        )

        for pick in picks:
            if pick.id is None:
                continue
            stocks = await self.sector_store.get_stocks_by_pick(pick.id)
            by_sector: dict[str, list[SectorStock]] = defaultdict(list)
            for s in stocks:
                by_sector[s.sector_name].append(s)
            for sector_name, sector_stocks in by_sector.items():
                await self._scan_sector(sector_name, sector_stocks, thresholds, now)

    async def _scan_sector(
        self,
        sector_name: str,
        stocks: list[SectorStock],
        thresholds: dict[str, float],
        now: datetime,
    ) -> None:
        results = await asyncio.gather(
            *[self.evaluate_stock(s.stock_code, thresholds) for s in stocks],
            return_exceptions=True,
        )
        passed: list[dict[str, Any]] = []
        for s, result in zip(stocks, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(f"[sector] {s.stock_code} 평가 실패: {result}")
                continue
            ok, metrics = result
            if ok:
                passed.append({
                    "code": s.stock_code,
                    "name": s.stock_name,
                    **metrics,
                })

        if len(passed) >= C.SECTOR_B_MIN_PASSED:
            await self._emit_alert(sector_name, passed, thresholds, now)
        else:
            logger.debug(
                f"[sector] {sector_name}: {len(passed)}/{len(stocks)} passed "
                f"(need ≥{C.SECTOR_B_MIN_PASSED})"
            )

    # ---------- 알림 + DB ----------
    async def _emit_alert(
        self,
        sector_name: str,
        passed_stocks: list[dict[str, Any]],
        thresholds: dict[str, float],
        now: datetime,
    ) -> None:
        # DB 기반 쿨다운 — 재시작에도 일관
        if not await self.sector_store.should_alert(
            sector_name, stage=1, cooldown_min=C.SECTOR_ALERT_COOLDOWN_MIN,
        ):
            logger.debug(f"[sector] {sector_name} cooldown (stage=1)")
            return

        passed_summary = [
            {
                "code": p["code"],
                "name": p["name"],
                "vol_ratio": p["vol_ratio"],
                "return": p["return"],
            }
            for p in passed_stocks
        ]
        try:
            await self.sector_store.insert_alert(
                sector_name=sector_name,
                stage=1,
                triggered_at=now,
                passed_stocks=passed_summary,
                metrics={"passed_count": len(passed_stocks)},
                threshold_used=thresholds,
            )
        except Exception as e:
            logger.error(f"[sector] alert DB 기록 실패: {e}")

        lines = [f"[Stage1] {sector_name} 쏠림 감지 - {len(passed_stocks)}종목 통과"]
        for p in passed_stocks:
            lines.append(
                f"  · {p['name']}({p['code']}) "
                f"vol×{p['vol_ratio']} / {p['return']*100:+.1f}%"
            )
        lines.append(
            f"threshold: vol×{thresholds['vol_mult']} / ret≥{thresholds['return']*100:.1f}%"
        )
        try:
            await self.telegram.notify("\n".join(lines))
        except Exception as e:
            logger.error(f"[sector] 텔레그램 알림 실패: {e}")

        logger.info(f"[sector] alert emitted: {sector_name} ({len(passed_stocks)}종목)")
