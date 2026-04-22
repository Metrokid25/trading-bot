"""섹터 픽 도메인 dataclass."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class PickStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    ARCHIVED = "archived"


@dataclass
class SectorPick:
    pick_date: str           # YYYY-MM-DD (스승님이 픽 준 날짜)
    created_at: datetime     # DB 삽입 시각 (KST)
    expires_at: datetime     # 만료 시각 (KST)
    status: PickStatus = PickStatus.ACTIVE
    raw_input: str = ""
    id: int | None = None

    @classmethod
    def create(
        cls,
        pick_date: str,
        raw_input: str = "",
        expires_days: int = 7,
    ) -> "SectorPick":
        """현재 시각 기준으로 created_at/expires_at을 자동 채우는 팩토리."""
        now = datetime.now()
        return cls(
            pick_date=pick_date,
            created_at=now,
            expires_at=now + timedelta(days=expires_days),
            raw_input=raw_input,
        )


@dataclass
class SectorStock:
    pick_id: int
    sector_name: str
    stock_code: str
    stock_name: str
    added_order: int
    id: int | None = None


@dataclass
class UpsertResult:
    pick_id: int
    is_new_pick: bool
    added_count: int
    skipped_stocks: list[SectorStock]
    total_count: int
