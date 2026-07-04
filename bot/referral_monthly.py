"""
Итоги реферальной программы за прошлый календарный месяц (MSK).
"""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from .database import get_connection
from .referral_rewards import get_referral_bonus_days
from .referral_tiers import MSK, month_label_ru
from .custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)


async def send_referral_monthly_summaries(bot: Bot) -> None:
    """Запускать 1-го числа: итоги за предыдущий месяц всем, кто пригласил друзей."""
    now = datetime.now(MSK)
    if now.month == 1:
        year, month = now.year - 1, 12
    else:
        year, month = now.year, now.month - 1

    ntype = f"referral_monthly_summary_{year:04d}_{month:02d}"
    bonus_days = await get_referral_bonus_days()

    async with get_connection() as conn:
        referrers = await conn.fetch(
            """
            SELECT DISTINCT inviter_id AS referrer_id
            FROM referral_invite_rewards
            WHERE inviter_reward_at IS NOT NULL
            """,
        )

    for row in referrers:
        referrer_id = row["referrer_id"]
        if not referrer_id:
            continue
        try:
            async with get_connection() as conn:
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM user_notifications
                    WHERE user_id = $1 AND notification_type = $2
                    """,
                    referrer_id,
                    ntype,
                )
                if exists:
                    continue

                invited_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM referral_invite_rewards
                    WHERE inviter_id = $1
                      AND inviter_reward_at IS NOT NULL
                      AND EXTRACT(YEAR FROM inviter_reward_at AT TIME ZONE 'Europe/Moscow') = $2
                      AND EXTRACT(MONTH FROM inviter_reward_at AT TIME ZONE 'Europe/Moscow') = $3
                    """,
                    referrer_id,
                    year,
                    month,
                )
                invited_count = int(invited_count or 0)
                if invited_count <= 0:
                    continue

                await conn.execute(
                    """
                    INSERT INTO user_notifications (user_id, notification_type)
                    VALUES ($1, $2)
                    """,
                    referrer_id,
                    ntype,
                )

            days_total = invited_count * bonus_days
            label = month_label_ru(year, month)
            b = InlineKeyboardBuilder()
            b.row(btn("Подарок", "gift", callback_data="open_invite"))

            await bot.send_message(
                referrer_id,
                f"{E.chart} <b>Итоги подарков за {label}</b>\n\n"
                f"Приглашено друзей: <b>{invited_count}</b>\n"
                f"Начислено Plus: <b>{days_total} дн.</b> "
                f"(по <b>{bonus_days}</b> за каждого)\n\n"
                f"Приглашайте ещё — за каждого друга вы и он получаете "
                f"<b>{bonus_days} дн.</b> SvoyVPN Plus.",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug("referral monthly summary user=%s: %s", referrer_id, e)
