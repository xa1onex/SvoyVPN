"""
Middleware: блокирует обработку апдейтов от заблокированных пользователей.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import AppConfig
from .user_block import BLOCKED_USER_MESSAGE, is_user_blacklisted

logger = logging.getLogger(__name__)


class BlacklistMiddleware(BaseMiddleware):
    def __init__(self, config: AppConfig) -> None:
        self._admin_ids = frozenset(config.bot.admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user and not event.from_user.is_bot:
            user_id = event.from_user.id

        if user_id is not None and user_id not in self._admin_ids:
            if await is_user_blacklisted(user_id):
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer(
                            "Доступ ограничен. См. сообщение в чате.",
                            show_alert=True,
                        )
                    except Exception as e:
                        logger.debug("blacklist callback answer: %s", e)
                    try:
                        await event.message.answer(
                            BLOCKED_USER_MESSAGE,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.debug("blacklist callback message: %s", e)
                elif isinstance(event, Message):
                    await event.answer(
                        BLOCKED_USER_MESSAGE,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                return None

        return await handler(event, data)
