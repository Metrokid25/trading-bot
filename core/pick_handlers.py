"""섹터 픽 텔레그램 명령어 핸들러.

명령어: /p /picks /extend /archive
인증 데코레이터 적용. SectorStore / StockMaster 주입 기반.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import wraps

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from core.pick_parser import ParseError, parse_pick_input
from core.telegram_bot import TelegramBot
from data.sector_models import SectorPick, SectorStock
from data.sector_store import SectorStore
from data.stock_master import StockMaster

# 텔레그램 단일 메시지 제한 4096자. 여유분 포함해 분할 크기 3500.
_CHUNK_SIZE = 3500

RawHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def require_authorized_user(handler: RawHandler) -> RawHandler:
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if uid is None or uid not in settings.ALLOWED_TELEGRAM_USERS:
            if update.message:
                await update.message.reply_text("❌ 권한 없음")
            return
        await handler(update, context)
    return wrapper


async def _reply(update: Update, text: str) -> None:
    """길이 초과 시 라인 단위 분할 전송."""
    if update.message is None:
        return
    if len(text) <= _CHUNK_SIZE:
        await update.message.reply_text(text)
        return
    chunks: list[str] = []
    buf: list[str] = []
    cur = 0
    for line in text.split("\n"):
        add = len(line) + 1
        if cur + add > _CHUNK_SIZE and buf:
            chunks.append("\n".join(buf))
            buf = []
            cur = 0
        buf.append(line)
        cur += add
    if buf:
        chunks.append("\n".join(buf))
    for c in chunks:
        await update.message.reply_text(c)


def _d_days(now: datetime, expires: datetime) -> int:
    return max(0, (expires.date() - now.date()).days)


def _format_pick_success(
    pick: SectorPick,
    sectors: dict[str, list[SectorStock]],
    failed: list[str],
) -> str:
    lines: list[str] = []
    if failed:
        lines.append(f"⚠️ 변환 실패 ({len(failed)}종목)")
        lines.append("```")
        lines.extend(failed)
        lines.append("```")
        lines.append("위 종목명을 수정 후 다시 /p 명령하십시오.")
        lines.append("")
    lines.append(f"✅ 픽 저장 완료 (ID: {pick.id})")
    lines.append(
        f"📅 {pick.pick_date} 입력 | 만료: {pick.expires_at.strftime('%Y-%m-%d')}"
    )
    for sector_name, stocks in sectors.items():
        lines.append("")
        lines.append(f"[{sector_name}] {len(stocks)}종목")
        for s in stocks:
            lines.append(f"{s.stock_name} ({s.stock_code})")
    return "\n".join(lines)


def _format_picks_list(
    picks: list[SectorPick],
    sector_counts: dict[int, dict[str, int]],
) -> str:
    if not picks:
        return "활성 픽 없음."
    now = datetime.now()
    lines = [f"📋 활성 픽 ({len(picks)}건)"]
    for p in picks:
        d = _d_days(now, p.expires_at)
        lines.append("")
        lines.append(f"[{p.id}] {p.pick_date} | 만료 D-{d}")
        counts = sector_counts.get(p.id or 0, {})
        if counts:
            summary = ", ".join(f"{name} ({cnt})" for name, cnt in counts.items())
            lines.append(summary)
    return "\n".join(lines)


def _format_pick_detail(pick: SectorPick, stocks: list[SectorStock]) -> str:
    now = datetime.now()
    d = _d_days(now, pick.expires_at)
    lines = [
        f"📋 픽 {pick.id} 상세",
        f"📅 {pick.pick_date} 입력 | 만료: {pick.expires_at.strftime('%Y-%m-%d')} (D-{d})",
    ]
    by_sector: dict[str, list[SectorStock]] = {}
    for s in stocks:
        by_sector.setdefault(s.sector_name, []).append(s)
    for sector_name, items in by_sector.items():
        lines.append("")
        lines.append(f"[{sector_name}] {len(items)}종목")
        for s in items:
            lines.append(f"- {s.stock_name} ({s.stock_code})")
    return "\n".join(lines)


def _build_handlers(store: SectorStore, master: StockMaster):

    @require_authorized_user
    async def cmd_p(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.message.text is None:
            return
        text = update.message.text
        try:
            pick_date, sectors_input = parse_pick_input(text)
        except ParseError as e:
            await _reply(update, f"❌ 입력 오류\n{e}")
            return

        flat: list[tuple[str, str]] = [
            (sector, name) for sector, names in sectors_input.items() for name in names
        ]
        results = await asyncio.gather(
            *(master.resolve(n) for _, n in flat),
            return_exceptions=True,
        )

        resolved: dict[str, list[SectorStock]] = {}
        failed: list[str] = []
        order = 0
        for (sector, name), res in zip(flat, results):
            if isinstance(res, Exception) or res is None:
                failed.append(name)
                continue
            code, resolved_name = res
            order += 1
            resolved.setdefault(sector, []).append(
                SectorStock(
                    pick_id=0,  # insert_pick 이후 DB row엔 실제 pick_id 기록됨
                    sector_name=sector,
                    stock_code=code,
                    stock_name=resolved_name or name,
                    added_order=order,
                )
            )

        if not resolved:
            await _reply(
                update,
                "❌ 저장 거부: 한 종목도 변환할 수 없음\n"
                f"입력된 {len(flat)}종목 모두 종목명 식별 실패.",
            )
            return

        pick = SectorPick.create(pick_date, raw_input=text, expires_days=7)
        stocks_flat: list[SectorStock] = []
        for lst in resolved.values():
            stocks_flat.extend(lst)

        try:
            await store.insert_pick(pick, stocks_flat)
        except Exception:
            logger.exception("pick 저장 실패")
            await _reply(update, "❌ 저장 실패 (DB 오류)")
            return

        await _reply(update, _format_pick_success(pick, resolved, failed))

    @require_authorized_user
    async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if args:
            try:
                pick_id = int(args[0])
            except ValueError:
                await _reply(update, "❌ pick_id는 정수여야 합니다")
                return
            picks = await store.get_active_picks()
            target = next((p for p in picks if p.id == pick_id), None)
            if target is None:
                await _reply(update, f"픽 ID {pick_id}를 찾을 수 없음")
                return
            stocks = await store.get_stocks_by_pick(pick_id)
            await _reply(update, _format_pick_detail(target, stocks))
            return

        picks = await store.get_active_picks()
        counts: dict[int, dict[str, int]] = {}
        for p in picks:
            if p.id is None:
                continue
            stocks = await store.get_stocks_by_pick(p.id)
            sec: dict[str, int] = {}
            for s in stocks:
                sec[s.sector_name] = sec.get(s.sector_name, 0) + 1
            counts[p.id] = sec
        await _reply(update, _format_picks_list(picks, counts))

    @require_authorized_user
    async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await _reply(update, "형식: /extend <pick_id> [days=7]")
            return
        try:
            pick_id = int(args[0])
            days = int(args[1]) if len(args) > 1 else 7
        except ValueError:
            await _reply(update, "❌ 인자는 정수여야 합니다")
            return
        try:
            await store.extend_pick(pick_id, days)
        except ValueError as e:
            await _reply(update, f"❌ {e}")
            return
        await _reply(update, f"✅ 픽 {pick_id} 만료일 +{days}일 연장")

    @require_authorized_user
    async def cmd_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await _reply(update, "형식: /archive <pick_id>")
            return
        try:
            pick_id = int(args[0])
        except ValueError:
            await _reply(update, "❌ pick_id는 정수여야 합니다")
            return
        await store.archive_pick(pick_id)
        await _reply(update, f"✅ 픽 {pick_id} 아카이브")

    return cmd_p, cmd_picks, cmd_extend, cmd_archive


def register_pick_handlers(
    bot: TelegramBot, store: SectorStore, master: StockMaster
) -> None:
    cmd_p, cmd_picks, cmd_extend, cmd_archive = _build_handlers(store, master)
    bot.register_raw("p", cmd_p)
    bot.register_raw("picks", cmd_picks)
    bot.register_raw("extend", cmd_extend)
    bot.register_raw("archive", cmd_archive)
