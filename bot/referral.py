"""
Реферальная программа: ссылка и экран «Подарок».
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any
from urllib.parse import quote

from aiogram import Bot
from aiogram.types import User

from .database import generate_subscription_token, get_connection
from .help_urls import earn_help_link_html
from .menu_labels import GIFT_BUTTON
from .plans import (
    FREE_SUBSCRIPTION_END,
    FREE_TIER_ID,
    get_tier_bypass_gb,
    get_tier_max_devices,
)
from .referral_purchases import TG_GIFT_RUB_RANGE, get_referrer_purchase_stats
from .referral_rewards import get_referral_bonus_days
from .subscriptions import create_or_activate_keys_for_all_servers

logger = logging.getLogger(__name__)

SHARE_TEXT = "Рабочий VPN c обходом Белых Списков - бесплатно по моей ссылке!"


def _ru_days_word(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "дня"
    return "дней"


def _ru_tg_gifts_phrase(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} подарок TG"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return f"{n} подарка TG"
    return f"{n} подарков TG"


async def track_referral_page_open(user_id: int) -> None:
    try:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO user_notifications (user_id, notification_type)
                SELECT $1, 'referral_opened'
                WHERE NOT EXISTS (
                    SELECT 1 FROM user_notifications
                    WHERE user_id = $1 AND notification_type = 'referral_opened'
                      AND created_at > NOW() - INTERVAL '2 hours'
                )
                """,
                user_id,
            )
    except Exception as e:
        logger.debug("track_referral_page_open: %s", e)


async def _ensure_referral_code(
    conn,
    user_id: int,
    username: str | None,
    first_name: str,
) -> dict[str, Any]:
    user_data = await conn.fetchrow(
        """
        SELECT referral_code, referral_count
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not user_data:
        referral_code = secrets.token_hex(4)
        sub_token = generate_subscription_token()
        await conn.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, registration_date, last_activity,
                referral_code, pay_subscribed, subscription_end, subscription_token,
                subscription_tier, bypass_traffic_limit_gb, device_limit
            ) VALUES (
                $1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                $4, TRUE, $5, $6, $7, $8, $9
            )
            """,
            user_id,
            username,
            first_name,
            referral_code,
            FREE_SUBSCRIPTION_END,
            sub_token,
            FREE_TIER_ID,
            get_tier_bypass_gb(FREE_TIER_ID),
            get_tier_max_devices(FREE_TIER_ID),
        )
        asyncio.create_task(create_or_activate_keys_for_all_servers(user_id))
        return {"referral_code": referral_code, "referral_count": 0}

    referral_code = user_data.get("referral_code") or ""
    if not referral_code:
        referral_code = secrets.token_hex(4)
        await conn.execute(
            "UPDATE users SET referral_code = $1 WHERE user_id = $2",
            referral_code,
            user_id,
        )
    return {
        "referral_code": referral_code,
        "referral_count": int(user_data.get("referral_count") or 0),
    }


async def build_referral_context(bot: Bot, actor: User) -> dict[str, Any]:
    user_id = actor.id
    username = actor.username
    first_name = actor.first_name or "Пользователь"

    async with get_connection() as conn:
        stats = await _ensure_referral_code(conn, user_id, username, first_name)

    bot_username = (await bot.get_me()).username
    referral_code = stats["referral_code"]
    ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    share_url = (
        "https://t.me/share/url?"
        f"url={quote(ref_link)}&text={quote(SHARE_TEXT)}"
    )

    bonus_days = await get_referral_bonus_days()
    purchase_stats = await get_referrer_purchase_stats(user_id)

    return {
        **stats,
        "ref_link": ref_link,
        "share_url": share_url,
        "bonus_days": bonus_days,
        **purchase_stats,
    }


def format_earn_screen(ctx: dict[str, Any]) -> str:
    referral_count = ctx.get("referral_count", 0)
    bonus_days = int(ctx.get("bonus_days") or 7)
    ref_link = (ctx.get("ref_link") or "").strip()
    pct = int(ctx.get("purchase_bonus_percent") or 10)
    every_n = int(ctx.get("yearly_gift_every_n") or 3)
    until_gift = int(ctx.get("until_tg_gift") or every_n)
    pending_gifts = int(ctx.get("pending_tg_gifts") or 0)
    fulfilled_gifts = int(ctx.get("fulfilled_tg_gifts") or 0)
    total_earned_days = int(ctx.get("total_earned_days") or 0)

    if ref_link:
        step1 = (
            f'1. Перешлите ссылку <code><a href="{ref_link}">{ref_link}</a></code> '
            "или нажмите «Пригласить друга»"
        )
    else:
        step1 = "1. Нажмите «Пригласить друга»"

    how = (
        "<b>Как пригласить друга</b>\n"
        "<blockquote>"
        f"{step1}\n"
        "2. Друг переходит по вашей ссылке в бота и регистрируется\n"
        f"3. Вам и другу — по <b>{bonus_days} дн.</b> SvoyVPN <b>Plus</b>"
        "</blockquote>"
    )

    rewards = (
        "<b>Бонусы за оплаты друзей</b>\n"
        "<blockquote>"
        f"• За каждую оплату друга — <b>{pct}%</b> дней подписки Plus вам\n"
        f"• За каждую <b>{every_n}-ю</b> годовую Plus — подарок в TG "
        f"<b>{TG_GIFT_RUB_RANGE}</b> (свяжемся с вами)\n"
        f"• До следующего подарка: <b>{until_gift}</b> × Plus год"
        "</blockquote>"
    )

    earned_parts = [f"{total_earned_days} {_ru_days_word(total_earned_days)} подписки"]
    if fulfilled_gifts > 0:
        earned_parts.append(_ru_tg_gifts_phrase(fulfilled_gifts))

    stats_lines = [
        f"Приглашено друзей: <b>{referral_count}</b>",
        f"До следующего TG подарка: <b>{until_gift}</b> × Plus",
        f"Заработано: <b>{'; '.join(earned_parts)}</b>",
    ]
    if pending_gifts > 0:
        stats_lines.append(f"🎁 На проверке: <b>{_ru_tg_gifts_phrase(pending_gifts)}</b>")

    return (
        f"{GIFT_BUTTON}{earn_help_link_html()}\n\n"
        f"{how}\n\n"
        f"{rewards}\n\n"
        + "\n".join(stats_lines)
    )
