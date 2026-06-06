"""Уведомления администраторам в Telegram."""

from __future__ import annotations

import logging

from aiogram import Bot

logger = logging.getLogger(__name__)


async def notify_admins_html(
    bot: Bot,
    admin_ids: list[int],
    text: str,
    *,
    exclude_user_id: int | None = None,
) -> None:
    for admin_id in admin_ids:
        if exclude_user_id and admin_id == exclude_user_id:
            continue
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error("notify_admins_html to %s: %s", admin_id, e)
