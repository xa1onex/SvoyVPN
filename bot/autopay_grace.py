"""
Отсрочка доступа после неудачного автоплатежа (только yookassa_autopay / привязанная карта).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .database import get_connection
from .plans import ALL_PAID_TIER_IDS, PAID_TIER_IDS, TIERS

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "autopay_payment_failed"


def autopay_grace_days() -> int:
    try:
        return max(1, int(os.getenv("SVOYVPN_AUTOPAY_GRACE_DAYS", "3")))
    except ValueError:
        return 3


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return datetime.strptime(value.split()[0], "%Y-%m-%d").date()
    return None


async def is_in_autopay_grace(conn, user_id: int) -> bool:
    row = await conn.fetchrow(
        "SELECT autopay_grace_until FROM users WHERE user_id = $1",
        user_id,
    )
    until = _as_date(row["autopay_grace_until"]) if row else None
    return bool(until and until >= datetime.now().date())


async def clear_autopay_grace(conn, user_id: int) -> None:
    await conn.execute(
        """
        UPDATE users SET
            autopay_grace_until = NULL,
            autopay_failed_at = NULL
        WHERE user_id = $1
        """,
        user_id,
    )
    await conn.execute(
        """
        DELETE FROM user_notifications
        WHERE user_id = $1 AND notification_type = $2
        """,
        user_id,
        NOTIFICATION_TYPE,
    )


async def start_autopay_grace(
    conn,
    user_id: int,
    *,
    subscription_end: object | None = None,
) -> date:
    """
    Продлевает subscription_end на период оплаты; тариф и ключи не трогаем.
    """
    today = datetime.now().date()
    days = autopay_grace_days()
    end_d = _as_date(subscription_end)
    grace_until = today + timedelta(days=days)
    if end_d and end_d > grace_until:
        grace_until = end_d

    grace_dt = datetime.combine(grace_until, datetime.max.time().replace(microsecond=0))

    await conn.execute(
        """
        UPDATE users SET
            subscription_end = $2,
            autopay_grace_until = $2,
            autopay_failed_at = COALESCE(autopay_failed_at, NOW())
        WHERE user_id = $1
        """,
        user_id,
        grace_dt,
    )
    logger.info(
        "autopay grace started user=%s until=%s",
        user_id,
        grace_until.isoformat(),
    )
    return grace_until


async def notify_autopay_failed(
    bot: Bot,
    user_id: int,
    *,
    tier: str,
    grace_until: date,
) -> bool:
    """Одно уведомление на период отсрочки."""
    async with get_connection() as conn:
        exists = await conn.fetchval(
            """
            SELECT 1 FROM user_notifications
            WHERE user_id = $1 AND notification_type = $2
            """,
            user_id,
            NOTIFICATION_TYPE,
        )
        if exists:
            return False

        tier_name = TIERS.get(tier, {}).get("name", tier.capitalize())

    from .plans import build_expiry_reminder_markup

    builder, _ = await build_expiry_reminder_markup(user_id)
    grace_str = grace_until.strftime("%d.%m.%Y")

    text = (
        "⚠️ <b>Не удалось списать оплату с привязанной карты</b>\n\n"
        f"Тариф <b>{tier_name}</b> остаётся подключен до <b>{grace_str}</b> — "
        f"у вас есть время оплатить вручную.\n\n"
        "После этой даты доступ перейдёт на тариф <b>Free</b>, "
        "если оплата не поступит."
    )

    try:
        await bot.send_message(
            user_id,
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO user_notifications (user_id, notification_type)
                VALUES ($1, $2)
                """,
                user_id,
                NOTIFICATION_TYPE,
            )
        return True
    except Exception as e:
        logger.error("autopay failed notify user=%s: %s", user_id, e)
        return False


async def handle_autopay_payment_failed(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Bot | None,
) -> None:
    """Вебхук ЮKassa: автоплатёж не прошёл."""
    user_id = metadata.get("user_id")
    if user_id is None:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id FROM payments
                WHERE yookassa_payment_id = $1
                ORDER BY id DESC LIMIT 1
                """,
                payment_id,
            )
            if row:
                user_id = row["user_id"]
    if user_id is None:
        return
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return

    async with get_connection() as conn:
        user = await conn.fetchrow(
            """
            SELECT subscription_tier, subscription_end,
                   yookassa_recurring_payment_method_id
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
        if not user or not user["yookassa_recurring_payment_method_id"]:
            return
        tier = (user["subscription_tier"] or "").strip()
        if tier not in ALL_PAID_TIER_IDS:
            return

        await conn.execute(
            """
            UPDATE payments SET status = 'failed'
            WHERE yookassa_payment_id = $1 AND status = 'pending'
            """,
            payment_id,
        )

        grace_until = await start_autopay_grace(
            conn, user_id, subscription_end=user["subscription_end"]
        )

    if bot:
        await notify_autopay_failed(
            bot, user_id, tier=tier, grace_until=grace_until
        )


async def try_start_grace_for_expired_autopay_user(
    conn,
    user_id: int,
    subscription_end: object,
    bot: Bot | None,
) -> bool:
    """
    Пользователь с картой, срок в БД истёк, автоплатёж не продлил — даём отсрочку.
    Returns True если отсрочка начата (не переводить на Free сейчас).
    """
    has_card = await conn.fetchval(
        """
        SELECT yookassa_recurring_payment_method_id IS NOT NULL
        FROM users
        WHERE user_id = $1
          AND COALESCE(subscription_tier, '') IN ('plus', 'lite', 'standard', 'pro')
        """,
        user_id,
    )
    if not has_card:
        return False

    grace_until_row = await conn.fetchval(
        "SELECT autopay_grace_until FROM users WHERE user_id = $1",
        user_id,
    )
    grace_d = _as_date(grace_until_row)
    today = datetime.now().date()

    if grace_d and grace_d >= today:
        return True

    if grace_d and grace_d < today:
        return False

    tier = await conn.fetchval(
        "SELECT subscription_tier FROM users WHERE user_id = $1",
        user_id,
    )
    tier = (tier or "plus").strip()

    grace_until = await start_autopay_grace(
        conn, user_id, subscription_end=subscription_end
    )
    if bot and tier in ALL_PAID_TIER_IDS:
        await notify_autopay_failed(
            bot, user_id, tier=tier, grace_until=grace_until
        )
    return True
