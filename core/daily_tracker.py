"""픽 종목 일봉 추적기.

픽 시점(pick_date)부터 D+N 일봉 데이터를 KIS에서 가져와 DailyOHLCV 리스트로 반환한다.
DailyTracker 클래스(D3)가 pick_daily_tracking INSERT/UPSERT를 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
from loguru import logger

from core.market_calendar import add_trading_days
from core.time_utils import now_kst, to_db_iso

if TYPE_CHECKING:
    from core.kis_api import KISClient


@dataclass(frozen=True, slots=True)
class DailyOHLCV:
    trade_date: str  # 'YYYY-MM-DD' (KIS 'YYYYMMDD' 변환)
    open: int
    high: int
    low: int
    close: int
    volume: int   # 누적 거래량 (KIS acml_vol)
    value: int    # 누적 거래대금, KRW (KIS acml_tr_pbmn)


async def fetch_daily_candles_for_pick(
    client: KISClient,
    ticker: str,
    pick_date: str,
    lookback_days: int = 20,
) -> list[DailyOHLCV]:
    """픽 시점(pick_date)부터 D+lookback_days까지의 일봉을 KIS에서 조회.

    영업일/비영업일 필터링은 하지 않음 — KIS가 거래일만 반환함.

    Returns:
        list[DailyOHLCV]: trade_date 오름차순. 빈 list 가능(휴장일 직후 등).

    Raises:
        ValueError: ticker 형식 불량, pick_date 형식 불량, lookback_days < 0
        httpx.HTTPError: KIS 4회 재시도 후에도 실패 (KIS 클라이언트 전파)

    Notes:
        - KIS 일봉 응답이 rt_cd != "0"이어도 현재 raise하지 않음
          (TODO: rt_cd 검증은 별도 위생 작업, 빈 list로 간주됨).
        - lookback_days=20이면 pick_date 포함 21일치 범위 요청
          (캘린더 일수 기준, KIS는 거래일만 반환하므로 실제 응답 수는 더 적음).
    """
    if not ticker or not ticker.isdigit() or len(ticker) != 6:
        raise ValueError(f"ticker must be 6-digit numeric string: {ticker!r}")

    if lookback_days < 0:
        raise ValueError(f"lookback_days must be >= 0, got {lookback_days}")

    try:
        start_dt = datetime.strptime(pick_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"pick_date must be 'YYYY-MM-DD', got {pick_date!r}")

    end_dt = start_dt + timedelta(days=lookback_days)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")

    rows = await client.get_daily_candles(ticker, start_str, end_str, period="D")

    result: list[DailyOHLCV] = []
    for row in rows:
        try:
            date_raw = row.get("stck_bsop_date", "")
            trade_date = datetime.strptime(date_raw, "%Y%m%d").strftime("%Y-%m-%d")
            candle = DailyOHLCV(
                trade_date=trade_date,
                open=int(row["stck_oprc"]),
                high=int(row["stck_hgpr"]),
                low=int(row["stck_lwpr"]),
                close=int(row["stck_clpr"]),
                volume=int(row["acml_vol"]),
                value=int(row["acml_tr_pbmn"]),
            )
            result.append(candle)
        except (KeyError, ValueError, TypeError):
            logger.warning(f"malformed daily candle row: ticker={ticker!r} row={row!r}")
            continue

    result.sort(key=lambda c: c.trade_date)
    return result


class DailyTracker:
    """픽 이벤트별 일봉 추적: pick_daily_tracking 사전 행 생성 및 UPSERT."""

    def __init__(self, db_path: str, kis_client: KISClient) -> None:
        self.db_path = db_path
        self.kis_client = kis_client

    def _trading_days_for_pick(self, pick_date: date) -> list[date]:
        """D+0 ~ D+20 거래일 목록 (총 21개). D+0 = pick_date."""
        days = [pick_date]
        for n in range(1, 21):
            days.append(add_trading_days(pick_date, n))
        return days

    async def ensure_tracking_rows(self, event_id: int) -> int:
        """주어진 event_id의 종목별 빈 추적 행 21개를 사전 생성.

        반환값: 새로 생성된 행 수 (INSERT OR IGNORE, 중복 행 제외).
        """
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                "SELECT pick_date FROM sector_pick_events WHERE event_id = ?",
                (event_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"event_id={event_id} not found in sector_pick_events")
            pick_date_str = row[0]
            if pick_date_str is None:
                raise ValueError(f"event_id={event_id} has NULL pick_date")
            pick_date = date.fromisoformat(pick_date_str)

            cur = await db.execute(
                """
                SELECT DISTINCT ss.id
                FROM sector_pick_events spe
                JOIN sector_stocks ss
                    ON ss.pick_id = spe.pick_id AND ss.sector_name = spe.sector_name
                WHERE spe.event_id = ? AND ss.tracking_status = 'active'
                """,
                (event_id,),
            )
            stock_rows = await cur.fetchall()
            if not stock_rows:
                return 0

            trading_days = self._trading_days_for_pick(pick_date)
            now_iso = to_db_iso(now_kst())

            params = [
                (stock_pick_id, td.isoformat(), offset, "pending", 0, event_id, now_iso)
                for (stock_pick_id,) in stock_rows
                for offset, td in enumerate(trading_days)
            ]

            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.executemany(
                    """
                    INSERT OR IGNORE INTO pick_daily_tracking
                        (stock_pick_id, trading_day, day_offset,
                         status, retry_count, event_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
                inserted = cursor.rowcount
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

        return inserted

    async def collect_daily(
        self, event_id: int, ticker: str, target_date: date
    ) -> bool:
        """event_id + ticker + target_date 조합의 일봉을 KIS에서 수집해 DB에 UPSERT.

        반환값: True(수집 성공), False(데이터 없음 또는 종목 미조회).
        target_date 데이터 없으면 False — DB 변경 없음, status는 D5 담당.
        """
        # 1단계: 메타 조회 (DB 커넥션 짧게 유지)
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                "SELECT pick_date FROM sector_pick_events WHERE event_id = ?",
                (event_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            pick_date_str = row[0]
            if pick_date_str is None:
                return False

            cur = await db.execute(
                """
                SELECT DISTINCT ss.id
                FROM sector_pick_events spe
                JOIN sector_stocks ss
                    ON ss.pick_id = spe.pick_id AND ss.sector_name = spe.sector_name
                WHERE spe.event_id = ? AND ss.stock_code = ? AND ss.tracking_status = 'active'
                LIMIT 1
                """,
                (event_id, ticker),
            )
            spid_row = await cur.fetchone()
            if spid_row is None:
                logger.warning(
                    "collect_daily: stock not found event_id=%d ticker=%s", event_id, ticker
                )
                return False
            stock_pick_id = spid_row[0]

        # 2단계: 날짜 변환 + KIS 호출 (DB 커넥션 닫은 후)
        # fetch_daily_candles_for_pick은 str 타입 pick_date를 요구하므로 pick_date_str 전달
        pick_date_obj = date.fromisoformat(pick_date_str)
        candles = await fetch_daily_candles_for_pick(self.kis_client, ticker, pick_date_str)

        target_date_str = target_date.isoformat()
        target_candle = next((c for c in candles if c.trade_date == target_date_str), None)
        if target_candle is None:
            return False

        pick_date_candle = next((c for c in candles if c.trade_date == pick_date_str), None)

        sorted_candles = sorted(candles, key=lambda c: c.trade_date)
        prev_candle = None
        for c in sorted_candles:
            if c.trade_date < target_date_str:
                prev_candle = c

        return_vs_pick: float | None = None
        if pick_date_candle and pick_date_candle.close > 0:
            return_vs_pick = (
                (target_candle.close - pick_date_candle.close) / pick_date_candle.close
            )

        return_vs_prev_close: float | None = None
        if prev_candle and prev_candle.close > 0:
            return_vs_prev_close = (
                (target_candle.close - prev_candle.close) / prev_candle.close
            )

        # day_offset: target_date가 D+0~D+20 목록에 없으면 False 반환 (무결성 보호)
        trading_days = self._trading_days_for_pick(pick_date_obj)
        try:
            day_offset = trading_days.index(target_date)
        except ValueError:
            logger.warning(
                "collect_daily: target_date=%s not in D+0~D+20 trading_days"
                " event_id=%d ticker=%s",
                target_date, event_id, ticker,
            )
            return False

        # 3단계: UPSERT
        now_iso = to_db_iso(now_kst())
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            await db.execute("BEGIN")
            try:
                await db.execute(
                    """
                    INSERT INTO pick_daily_tracking
                        (stock_pick_id, trading_day, day_offset,
                         open, high, low, close, volume, transaction_amount,
                         return_vs_pick, return_vs_prev_close,
                         status, event_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?)
                    ON CONFLICT(event_id, stock_pick_id, trading_day) DO UPDATE SET
                        open                 = excluded.open,
                        high                 = excluded.high,
                        low                  = excluded.low,
                        close                = excluded.close,
                        volume               = excluded.volume,
                        transaction_amount   = excluded.transaction_amount,
                        return_vs_pick       = excluded.return_vs_pick,
                        return_vs_prev_close = excluded.return_vs_prev_close,
                        status               = 'success'
                    """,
                    (
                        stock_pick_id, target_date_str, day_offset,
                        target_candle.open, target_candle.high,
                        target_candle.low, target_candle.close,
                        target_candle.volume, target_candle.value,
                        return_vs_pick, return_vs_prev_close,
                        event_id, now_iso,
                    ),
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

        return True
