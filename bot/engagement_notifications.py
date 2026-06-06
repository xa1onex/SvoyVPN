"""
Система engagement-уведомлений.

Периодические проверки поведения пользователей и отправка
мотивирующих уведомлений/предложений.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import AppConfig
from .database import get_connection
from .plans import ALL_PAID_TIER_IDS, TIERS, TIER_PLANS_BASE, get_tier_plans, TIER_ORDER

logger = logging.getLogger(__name__)


async def run_engagement_notifications(bot: Bot, config: AppConfig) -> None:
    """Main entry point — run all engagement notification checks."""
    try:
        await _notify_idle_new_users(bot)
    except Exception as e:
        logger.error("engagement: idle_new error: %s", e, exc_info=True)

    try:
        await _notify_trial_inactive(bot, config)
    except Exception as e:
        logger.error("engagement: trial_inactive error: %s", e, exc_info=True)

    try:
        await _notify_cancelled_users(bot, config)
    except Exception as e:
        logger.error("engagement: cancelled error: %s", e, exc_info=True)

    try:
        await _reset_trial_for_lapsed_paid_users(bot)
    except Exception as e:
        logger.error("engagement: lapsed_paid_trial_reset error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# 1. Registered but idle — send gift + trial button
# ---------------------------------------------------------------------------

async def _notify_idle_new_users(bot: Bot) -> None:
    """Users who registered 1+ day ago, on Free tier, never used trial,
    and haven't been notified yet."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.trial_used = FALSE
              AND COALESCE(u.subscription_tier, 'free') NOT IN ('plus', 'lite', 'standard', 'pro')
              AND u.registration_date < NOW() - INTERVAL '1 day'
              AND u.registration_date > NOW() - INTERVAL '7 days'
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = 'idle_new_gift'
              )
            LIMIT 50
            """
        )

    for row in rows:
        user_id = row["user_id"]
        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text="🎁 Plus за 1₽ — попробовать",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                "🎁 <b>У нас для тебя подарок!</b>\n\n"
                "Мы подготовили VPN, который работает быстро и стабильно — "
                "YouTube, TikTok, ChatGPT без блокировок.\n\n"
                "🔓 50 ГБ bypass в месяц\n"
                "📱 Безлимит устройств\n"
                "⚡ Подключение за 30 сек\n\n"
                "Попробуй тариф <b>Plus</b> за <b>1₽</b> — никаких обязательств!",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id, "idle_new_gift",
                )
        except Exception as e:
            logger.debug("engagement idle_new: user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# 2. Activated trial, 3 days later still inactive — 30% discount on Lite
# ---------------------------------------------------------------------------

async def _notify_trial_inactive(bot: Bot, config: AppConfig) -> None:
    """Users who used trial, trial ended 3+ days ago, never bought anything."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.trial_used = TRUE
              AND (u.pay_subscribed = FALSE OR u.subscription_end < CURRENT_DATE)
              AND u.subscription_end IS NOT NULL
              AND u.subscription_end < CURRENT_DATE - INTERVAL '3 days'
              AND NOT EXISTS (
                  SELECT 1 FROM payments p
                  WHERE p.user_id = u.user_id AND p.status = 'completed'
                    AND p.amount > 100
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = 'trial_inactive_discount'
              )
            LIMIT 50
            """
        )

    if not rows:
        return

    plans = await get_tier_plans()
    plus_plan = plans.get("plus_1m")
    if not plus_plan:
        return
    full_price = plus_plan["price_rub"]
    discount_price = int(full_price * 0.7)
    discount_price_rub = discount_price / 100.0

    for row in rows:
        user_id = row["user_id"]
        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text=f"🔥 Plus за {discount_price_rub:.0f}₽/мес (-30%)",
                callback_data="promo_plus_30",
            ))
            await bot.send_message(
                user_id,
                "👋 <b>Привет!</b>\n\n"
                "Мы видим, что пробный период закончился. "
                "Наш VPN — YouTube, TikTok, AI без блокировок.\n\n"
                f"Специально для тебя — <b>скидка 30%</b> на тариф Plus:\n"
                f"<s>{full_price / 100:.0f}₽</s> → <b>{discount_price_rub:.0f}₽/мес</b>\n\n"
                "• 50 ГБ bypass/мес\n"
                "• YouTube / TikTok / AI работают\n"
                "• Безлимит устройств",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id, "trial_inactive_discount",
                )
        except Exception as e:
            logger.debug("engagement trial_inactive: user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# 3. Cancelled expensive tier, not using VPN — personalized offer
# ---------------------------------------------------------------------------

async def _notify_cancelled_users(bot: Bot, config: AppConfig) -> None:
    """Users who had a paid tier, cancelled (no card), subscription ended,
    not using VPN — send personalized offer based on their historical usage."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_tier
            FROM users u
            WHERE u.subscription_tier IN ('plus', 'standard', 'pro', 'lite')
              AND u.yookassa_recurring_payment_method_id IS NULL
              AND (u.pay_subscribed = FALSE OR u.subscription_end < CURRENT_DATE)
              AND u.subscription_end IS NOT NULL
              AND u.subscription_end < CURRENT_DATE
              AND u.subscription_end > CURRENT_DATE - INTERVAL '30 days'
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = 'cancelled_personal_offer'
              )
            LIMIT 30
            """
        )

    if not rows:
        return

    plans = await get_tier_plans()

    offer_plan = plans.get("plus_1m")
    if not offer_plan:
        return

    for row in rows:
        user_id = row["user_id"]
        offer_price = offer_plan["price_rub"] / 100.0

        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text=f"💎 Plus за {offer_price:.0f}₽/мес",
                callback_data="tier_select:plus:plus_1m",
            ))
            await bot.send_message(
                user_id,
                "👋 <b>Мы по тебе скучаем!</b>\n\n"
                "YouTube, TikTok и AI снова в зоне доступа — "
                "оформите тариф <b>Plus</b> и пользуйтесь без ограничений:\n\n"
                "• 50 ГБ bypass/мес\n"
                "• YouTube / TikTok / AI работают\n"
                "• Безлимит устройств\n"
                f"• {offer_price:.0f}₽/мес или 999₽/год",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id, "cancelled_personal_offer",
                )
        except Exception as e:
            logger.debug("engagement cancelled: user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# 4. Lapsed paid users — 30 days after paid sub ended: reset trial, notify
# ---------------------------------------------------------------------------

async def _reset_trial_for_lapsed_paid_users(bot: Bot) -> None:
    """
    Если пользователь имел платный тариф (lite/standard/pro), он истёк
    30+ дней назад и за это время не было новой покупки — сбрасываем
    trial_used обратно в FALSE и отправляем уведомление с предложением Pro за 1₽.
    """
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.last_paid_sub_ended_at IS NOT NULL
              AND u.last_paid_sub_ended_at < NOW() - INTERVAL '30 days'
              AND COALESCE(u.subscription_tier, 'free') NOT IN ('plus', 'lite', 'standard', 'pro')
              AND u.trial_used = TRUE
              AND u.blacklisted = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM payments p
                  WHERE p.user_id = u.user_id
                    AND p.status = 'completed'
                    AND p.amount > 100
                    AND p.timestamp > u.last_paid_sub_ended_at
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id
                    AND n.notification_type = 'lapsed_paid_trial_reset'
                    AND n.created_at > NOW() - INTERVAL '30 days'
              )
            LIMIT 50
            """
        )

    for row in rows:
        user_id = row["user_id"]
        try:
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE users SET trial_used = FALSE WHERE user_id = $1",
                    user_id,
                )
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id, "lapsed_paid_trial_reset",
                )

            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text="🎁 Plus за 1₽ — попробовать снова",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                "👋 <b>Давно не виделись!</b>\n\n"
                "Прошёл месяц с момента окончания вашей подписки.\n\n"
                "Специально для вас мы возвращаем <b>предложение Plus за 1₽</b> — "
                "попробуйте снова:\n\n"
                "🔓 50 ГБ bypass в месяц\n"
                "📱 Безлимит устройств\n"
                "⚡ YouTube / TikTok / AI работают\n\n"
                "Воспользуйтесь предложением — оно ждёт вас в главном меню!",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
        except Exception as e:
            logger.debug("engagement lapsed_paid_trial_reset: user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# 6. Device reset counter — every 3rd reset suggest higher tier
# ---------------------------------------------------------------------------

async def check_device_reset_upsell(bot: Bot, user_id: int) -> None:  # noqa: N802
    """Call this after each device reset. If 3rd+ reset (with cooldown),
    suggest a higher tier."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT device_reset_count, last_device_reset_at, subscription_tier
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
        if not row:
            return

        count = (row["device_reset_count"] or 0)
        last_reset = row.get("last_device_reset_at")
        current_tier = row.get("subscription_tier") or "lite"

        # Cooldown: only count if >1 hour since last reset
        if last_reset and (datetime.utcnow() - last_reset) < timedelta(hours=1):
            # Just update timestamp, don't increment
            await conn.execute(
                "UPDATE users SET last_device_reset_at = NOW() WHERE user_id = $1",
                user_id,
            )
            return

        new_count = count + 1
        await conn.execute(
            "UPDATE users SET device_reset_count = $1, last_device_reset_at = NOW() WHERE user_id = $2",
            new_count, user_id,
        )

    # Every 3rd reset
    if new_count % 3 != 0:
        return

    # On Free — suggest Plus
    if current_tier not in ALL_PAID_TIER_IDS:
        plus_info = TIERS["plus"]
        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text="💎 Перейти на Plus",
                callback_data="open_tiers",
            ))
            await bot.send_message(
                user_id,
                "💡 <b>Часто сбрасываете устройства?</b>\n\n"
                "На тарифе <b>Plus</b> — <b>безлимит устройств</b>, "
                "ничего не нужно отключать!\n\n"
                f"• {plus_info['bypass_gb']} ГБ bypass/мес\n"
                "• YouTube / TikTok / AI работают\n"
                "• Безлимит устройств",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
        except Exception as e:
            logger.debug("engagement device_reset upsell: user=%s err=%s", user_id, e)
