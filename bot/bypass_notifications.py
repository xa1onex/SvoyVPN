"""
Уведомления о bypass-трафике: 20%, 10%, 0% оставшегося лимита.
Запускается периодически через APScheduler.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .database import get_connection
from .traffic import BYTES_PER_GB, ensure_bypass_period

logger = logging.getLogger(__name__)

THRESHOLDS = [
    ("bypass_20pct", 0.20),   # 20% remaining
    ("bypass_10pct", 0.10),   # 10% remaining
    ("bypass_0pct", 0.00),    # 0% remaining (exhausted)
]


async def check_bypass_traffic_notifications(bot: Bot) -> None:
    """
    Check all active users with tier subscriptions for bypass traffic thresholds.
    Send notifications at 20%, 10%, and 0% remaining.
    """
    try:
        async with get_connection() as conn:
            users = await conn.fetch(
                """
                SELECT user_id, subscription_tier, bypass_traffic_used_bytes,
                       bypass_traffic_limit_gb, bypass_bonus_gb,
                       bypass_period_start
                FROM users
                WHERE pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) >= CURRENT_DATE
                  AND subscription_tier IS NOT NULL
                  AND bypass_traffic_limit_gb IS NOT NULL
                  AND bypass_traffic_limit_gb > 0
                """
            )

        for user_row in users:
            user_id = user_row["user_id"]
            used_bytes = int(user_row["bypass_traffic_used_bytes"] or 0)
            limit_gb = int(user_row["bypass_traffic_limit_gb"] or 0)
            pack_gb = int(user_row["bypass_bonus_gb"] or 0)
            period_start = user_row["bypass_period_start"]

            if limit_gb <= 0:
                continue

            total_limit_bytes = (limit_gb + pack_gb) * BYTES_PER_GB
            if total_limit_bytes <= 0:
                continue

            remaining_fraction = max(0, (total_limit_bytes - used_bytes)) / total_limit_bytes

            for notif_type, threshold in THRESHOLDS:
                if remaining_fraction <= threshold:
                    await _maybe_send_notification(
                        bot, user_id, notif_type, period_start,
                        used_bytes, total_limit_bytes, limit_gb, pack_gb,
                    )
                    break

    except Exception as e:
        logger.error("bypass notifications check failed: %s", e, exc_info=True)


async def _maybe_send_notification(
    bot: Bot,
    user_id: int,
    notif_type: str,
    period_start,
    used_bytes: int,
    limit_bytes: int,
    limit_gb: int,
    bonus_gb: int,
) -> None:
    """Send bypass notification if not already sent for this period."""
    if period_start is None:
        return

    try:
        async with get_connection() as conn:
            already_sent = await conn.fetchval(
                """
                SELECT 1 FROM bypass_traffic_notifications
                WHERE user_id = $1 AND notification_type = $2 AND bypass_period_start = $3
                """,
                user_id, notif_type, period_start,
            )
            if already_sent:
                return

            await conn.execute(
                """
                INSERT INTO bypass_traffic_notifications (user_id, notification_type, bypass_period_start)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, notification_type, bypass_period_start) DO NOTHING
                """,
                user_id, notif_type, period_start,
            )

        remaining_gb = max(0, (limit_bytes - used_bytes)) / BYTES_PER_GB
        total_gb = limit_gb + bonus_gb

        if notif_type == "bypass_0pct":
            text = (
                "🚫 <b>Bypass-лимит исчерпан</b>\n\n"
                f"Использовано: {used_bytes / BYTES_PER_GB:.1f} / {total_gb} ГБ\n\n"
                "Обычный VPN продолжает работать без ограничений.\n"
                "Bypass-сервера временно недоступны.\n\n"
                "Что можно сделать:"
            )
        elif notif_type == "bypass_10pct":
            text = (
                "⚠️ <b>Bypass трафик почти закончился</b>\n\n"
                f"Осталось: <b>{remaining_gb:.1f} ГБ</b> из {total_gb} ГБ (10%)\n\n"
                "Скоро bypass-сервера станут недоступны.\n"
                "Обычный VPN продолжит работать."
            )
        else:
            text = (
                "📊 <b>Bypass трафик заканчивается</b>\n\n"
                f"Осталось: <b>{remaining_gb:.1f} ГБ</b> из {total_gb} ГБ (20%)\n\n"
                "Рекомендуем докупить трафик или повысить тариф."
            )

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📶 Докупить ГБ", callback_data="open_bypass_packs")
        )
        builder.row(
            InlineKeyboardButton(text="⬆️ Повысить тариф", callback_data="open_tiers")
        )

        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=builder.as_markup()
        )
        logger.info("Sent bypass %s notification to user %s", notif_type, user_id)

    except Exception as e:
        logger.error("Failed to send bypass notification to %s: %s", user_id, e)
