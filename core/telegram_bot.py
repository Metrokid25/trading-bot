"""텔레그램 공용 클라이언트.

- 명령 핸들러: /add /remove /seed /weight /status /halt /resume
- 알림 헬퍼: notify(), alert()(비상)
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings

CommandFn = Callable[[list[str]], Awaitable[str]]


class TelegramBot:
    def __init__(self) -> None:
        self._app: Application | None = None
        self._handlers: dict[str, CommandFn] = {}

    def register(self, cmd: str, fn: CommandFn) -> None:
        self._handlers[cmd] = fn

    async def start(self) -> None:
        token = settings.TELEGRAM_BOT_TOKEN
        if not token or "your_bot_token" in token.lower() or ":" not in token:
            logger.warning("TELEGRAM_BOT_TOKEN 미설정/placeholder — 텔레그램 비활성화")
            return
        try:
            self._app = Application.builder().token(token).build()
            for cmd in self._handlers:
                self._app.add_handler(CommandHandler(cmd, self._wrap(cmd)))
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling()
            logger.info("텔레그램 봇 시작")
        except Exception as e:
            logger.error(f"텔레그램 시작 실패({type(e).__name__}): {e} — 비활성화로 진행")
            self._app = None

    async def stop(self) -> None:
        if not self._app:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def _wrap(self, cmd: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            fn = self._handlers[cmd]
            try:
                reply = await fn(context.args or [])
            except Exception as e:
                reply = f"❌ 오류: {e}"
            if update.message:
                await update.message.reply_text(reply)
        return handler

    async def notify(self, text: str) -> None:
        if not self._app or not settings.TELEGRAM_CHAT_ID:
            return
        try:
            await self._app.bot.send_message(chat_id=settings.TELEGRAM_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"telegram notify failed: {e}")

    async def alert(self, text: str) -> None:
        """비상 알림 — 별도 채널 있으면 그쪽, 없으면 일반 채널."""
        chat = settings.TELEGRAM_ALERT_CHAT_ID or settings.TELEGRAM_CHAT_ID
        if not self._app or not chat:
            return
        try:
            await self._app.bot.send_message(chat_id=chat, text=f"🚨 {text}")
        except Exception as e:
            logger.error(f"telegram alert failed: {e}")
