"""눌림목 시그널 알림 — 기본 dry-run(로그). 실제 텔레그램 발송은 승인 게이트.

emit_pullback_alerts(..., dry_run=True) 가 기본. dry_run=True 면 절대 외부로
발송하지 않고 로그만 남긴다. 실제 발송은 dry_run=False + telegram 주입 +
사용자 지시가 있을 때만. 이 분리는 프로젝트 원칙("실매매/텔레그램 발송은
사용자 지시 없이 건드리지 마라")을 코드 레벨에서 강제하기 위함이다.
"""
from __future__ import annotations

from typing import Awaitable, Protocol

from loguru import logger

from core.pullback_detector import PullbackSignal


class Notifier(Protocol):
    async def notify(self, text: str) -> bool:
        ...


def format_pullback_alert(signal: PullbackSignal) -> str:
    """눌림목 시그널을 사람이 읽는 한 줄 메시지로 포맷."""
    low = "?" if signal.window_low is None else f"{signal.window_low:,.0f}"
    close = "?" if signal.last_close is None else f"{signal.last_close:,.0f}"
    value = (
        "?"
        if signal.min_window_value is None
        else f"{signal.min_window_value / 1e8:.1f}억"
    )
    return (
        f"📉➡️📈 눌림목 [{signal.stock_code}] {signal.trading_day} "
        f"{signal.window_start}~{signal.window_end} "
        f"(윈도우최저 {low} / 마지막종가 {close} / 최저거래대금 {value}) "
        f"rule={signal.rule_version}"
    )


async def emit_pullback_alerts(
    signals: list[PullbackSignal],
    *,
    telegram: Notifier | None = None,
    dry_run: bool = True,
) -> list[str]:
    """시그널들을 알림으로 방출. 반환값은 포맷된 메시지 목록.

    dry_run=True(기본): 로그만. 외부 발송 없음.
    dry_run=False: telegram이 주입돼 있으면 notify()로 실제 발송. 발송 실패는
        로그로 남기고 계속 진행한다.
    """
    messages: list[str] = []
    for signal in signals:
        text = format_pullback_alert(signal)
        messages.append(text)
        if dry_run or telegram is None:
            logger.info("[pullback][dry-run] {}", text)
            continue
        sent = await _safe_notify(telegram.notify(text))
        if sent:
            logger.info("[pullback][sent] {}", text)
        else:
            logger.warning("[pullback][send-failed] {}", text)
    return messages


async def _safe_notify(awaitable: Awaitable[bool]) -> bool:
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001 — Notifier 구현체의 임의 예외를 삼켜 알림 루프 보호
        logger.warning("[pullback] notify raised: {}", exc)
        return False
