"""전략 5·6단계: 등록 풀 내 섹터 강도 확인 + 섹터 내 최강 종목 선택.

Phase 2.5 픽 파이프라인 전용. 눌림목 신호(2~4단계)가 난 종목을 sector_name으로
묶어 섹터 강도(후보 수 ≥ 임계)를 확인하고(5단계), 섹터별 최강 1종목을 고른다
(6단계). 강도 점수는 강세 마크(pick_breakout_marks)의 당일시가 대비 상승률 최대값,
동점이면 거래대금으로 가른다.

기존 agents/sector_detector.py(실시간 KIS 조회 + 텔레그램 실발송, main.py 트랙)와는
완전히 별개의 main_tracker 파이프라인 전용 구조적 랭킹이다. DB에 쓰지 않고
in-memory 결과를 반환한다(알림도 호출측에서 dry-run으로 처리).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import aiosqlite
from loguru import logger

from core.pullback_detector import PullbackDetector, PullbackRuleConfig


@dataclass(frozen=True, slots=True)
class SectorStrengthConfig:
    rule_version: str = "phase25_sector_v1"
    # 섹터 강도: 섹터 내 눌림목 후보가 이 수 이상이어야 "강한 섹터"로 인정.
    min_sector_candidates: int = 2


@dataclass(frozen=True, slots=True)
class SectorCandidate:
    daily_tracking_id: int
    event_id: int
    stock_pick_id: int
    stock_code: str
    sector_name: str
    trading_day: str
    # 강세 마크의 당일시가 대비 상승률 최대값(%). 강도 점수.
    strength_score: float | None
    # 강세 마크의 거래대금 최대값(원). 동점 tie-break.
    value: int | None


@dataclass(frozen=True, slots=True)
class SectorSelection:
    sector_name: str
    candidate_count: int
    best: SectorCandidate
    candidates: tuple[SectorCandidate, ...]
    rule_version: str


def _validate_config(config: SectorStrengthConfig) -> None:
    if not config.rule_version:
        raise ValueError("rule_version must not be empty")
    if config.min_sector_candidates < 1:
        raise ValueError("min_sector_candidates must be >= 1")


class SectorStrengthRanker:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @staticmethod
    def _config(config: SectorStrengthConfig | None) -> SectorStrengthConfig:
        return config or SectorStrengthConfig()

    @staticmethod
    def _score_key(candidate: SectorCandidate) -> tuple[float, int]:
        """최강 선택·정렬용 정렬키. None은 최하위로 취급."""
        strength = (
            candidate.strength_score
            if candidate.strength_score is not None
            else float("-inf")
        )
        value = candidate.value if candidate.value is not None else -1
        return (strength, value)

    def rank(
        self,
        candidates: list[SectorCandidate],
        config: SectorStrengthConfig | None = None,
    ) -> list[SectorSelection]:
        cfg = self._config(config)
        by_sector: dict[str, list[SectorCandidate]] = defaultdict(list)
        for candidate in candidates:
            by_sector[candidate.sector_name].append(candidate)

        selections: list[SectorSelection] = []
        for sector_name, members in by_sector.items():
            # 5단계: 섹터 강도 — 후보 수 게이트.
            if len(members) < cfg.min_sector_candidates:
                continue
            ordered = tuple(sorted(members, key=self._score_key, reverse=True))
            # 6단계: 섹터 내 최강 1종목.
            best = ordered[0]
            selections.append(
                SectorSelection(
                    sector_name=sector_name,
                    candidate_count=len(members),
                    best=best,
                    candidates=ordered,
                    rule_version=cfg.rule_version,
                )
            )
        # 강한 섹터(최강 종목 강도 높은 순)부터.
        selections.sort(key=lambda sel: self._score_key(sel.best), reverse=True)
        return selections

    async def _load_candidates(
        self,
        trading_day: str,
        daily_tracking_ids: list[int],
    ) -> list[SectorCandidate]:
        if not daily_tracking_ids:
            return []

        placeholders = ",".join("?" for _ in daily_tracking_ids)
        async with aiosqlite.connect(self.db_path, isolation_level=None) as db:
            cur = await db.execute(
                f"""
                SELECT
                    pdt.id,
                    pdt.event_id,
                    pdt.stock_pick_id,
                    ss.stock_code,
                    ss.sector_name,
                    pdt.trading_day,
                    MAX(bm.day_open_change_rate),
                    MAX(bm.value)
                FROM pick_daily_tracking pdt
                JOIN sector_stocks ss ON ss.id = pdt.stock_pick_id
                JOIN pick_breakout_marks bm ON bm.daily_tracking_id = pdt.id
                    AND bm.trading_day = pdt.trading_day
                WHERE pdt.id IN ({placeholders})
                  AND pdt.trading_day = ?
                GROUP BY pdt.id
                ORDER BY ss.sector_name, pdt.id
                """,
                (*daily_tracking_ids, trading_day),
            )
            rows = await cur.fetchall()

        # 주의: MAX(day_open_change_rate)와 MAX(value)는 서로 다른 마크에서 올 수
        # 있다(SQLite 집계). 강도/동점지표를 각각 "그 종목의 가장 강한 값"으로
        # 보는 근사이며, tie-break 보조 지표라 허용한다.
        return [
            SectorCandidate(
                daily_tracking_id=int(row[0]),
                event_id=int(row[1]),
                stock_pick_id=int(row[2]),
                stock_code=str(row[3]),
                sector_name=str(row[4]),
                trading_day=str(row[5]),
                strength_score=None if row[6] is None else float(row[6]),
                value=None if row[7] is None else int(row[7]),
            )
            for row in rows
        ]

    async def select_for_day(
        self,
        trading_day: str,
        config: SectorStrengthConfig | None = None,
        pullback_config: PullbackRuleConfig | None = None,
    ) -> list[SectorSelection]:
        cfg = self._config(config)
        try:
            _validate_config(cfg)
        except ValueError as exc:
            logger.warning("[sector_strength] invalid config error={}", exc)
            return []

        detector = PullbackDetector(self.db_path)
        _, signals = await detector.detect_all_d0(
            trading_day=trading_day, rule_config=pullback_config
        )
        # 신호의 daily_tracking_id만 사용해 후보를 DB에서 재로드한다. 섹터명·강도
        # 지표는 신호에 없고 sector_stocks/pick_breakout_marks 조인이 필요하므로.
        # 픽 규모가 작아 재조회 비용은 무시 가능.
        daily_tracking_ids = [signal.daily_tracking_id for signal in signals]
        candidates = await self._load_candidates(trading_day, daily_tracking_ids)
        return self.rank(candidates, cfg)


def format_sector_selection(selection: SectorSelection) -> str:
    """섹터 최강 종목 선택 결과를 사람이 읽는 한 줄 메시지로 포맷."""
    best = selection.best
    strength = (
        "?" if best.strength_score is None else f"{best.strength_score:+.2f}%"
    )
    others = ", ".join(
        c.stock_code for c in selection.candidates if c is not best
    )
    others_str = f" / 동섹터후보 [{others}]" if others else ""
    return (
        f"🏆 [{selection.sector_name}] 최강 {best.stock_code} "
        f"(강도 {strength}, 섹터후보 {selection.candidate_count}개){others_str} "
        f"rule={selection.rule_version}"
    )
