"""
Middleware: логирует действия пользователей (не админов) в боте.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .activity_log import (
    describe_callback_event,
    describe_message_event,
    schedule_bot_activity,
)
from .config import AppConfig

logger = logging.getLogger(__name__)

# Префиксы callback_data админки / служебных кнопок — не пишем в пользовательскую аналитику
_ADMIN_CALLBACK_PREFIXES = ("admin_", "wd_approve:", "wd_reject:", "broadcast_")


class BotActivityMiddleware(BaseMiddleware):
    def __init__(self, config: AppConfig) -> None:
        self._admin_ids = frozenset(config.bot.admin_ids)

    def _should_skip(self, user_id: int, callback_data: str | None = None) -> bool:
        if user_id in self._admin_ids:
            return True
        if callback_data and callback_data.startswith(_ADMIN_CALLBACK_PREFIXES):
            return True
        return False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            user_id = event.from_user.id
            if not self._should_skip(user_id):
                kind, action, detail = describe_message_event(event)
                schedule_bot_activity(user_id, kind, action, detail=detail)
        elif isinstance(event, CallbackQuery) and event.from_user and not event.from_user.is_bot:
            user_id = event.from_user.id
            if not self._should_skip(user_id, event.data):
                kind, action, detail = describe_callback_event(event)
                schedule_bot_activity(user_id, kind, action, detail=detail)

        return await handler(event, data)
