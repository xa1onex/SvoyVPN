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
from .plans import TIERS, TIER_PLANS_BASE, get_tier_plans, TIER_ORDER

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
        await _notify_referral_no_invites(bot, config)
    except Exception as e:
        logger.error("engagement: referral_no_invites error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# 1. Registered but idle — send gift + trial button
# ---------------------------------------------------------------------------

async def _notify_idle_new_users(bot: Bot) -> None:
    """Users who registered 1+ day ago, never subscribed, never used trial,
    and haven't been notified yet."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.trial_used = FALSE
              AND u.pay_subscribed = FALSE
              AND u.created_at < NOW() - INTERVAL '1 day'
              AND u.created_at > NOW() - INTERVAL '7 days'
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
                text="🎁 Standard за 1₽ — попробовать",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                "🎁 <b>У нас для тебя подарок!</b>\n\n"
                "Мы подготовили VPN, который работает быстро и стабильно — "
                "безлимитный доступ ко всем сайтам и сервисам.\n\n"
                "🔓 100 ГБ bypass (обход блокировок)\n"
                "📱 До 5 устройств\n"
                "⚡ Высокая скорость\n\n"
                "Попробуй за <b>1₽</b> — никаких обязательств!",
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
    lite_plan = plans.get("lite_1m")
    if not lite_plan:
        return
    full_price = lite_plan["price_rub"]
    discount_price = int(full_price * 0.7)
    discount_price_rub = discount_price / 100.0

    for row in rows:
        user_id = row["user_id"]
        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text=f"🔥 Lite за {discount_price_rub:.0f}₽/мес (-30%)",
                callback_data="promo_lite_30",
            ))
            await bot.send_message(
                user_id,
                "👋 <b>Привет!</b>\n\n"
                "Мы видим, что пробный период закончился. "
                "Наш VPN — это стабильный и быстрый доступ ко всему интернету "
                "без ограничений.\n\n"
                f"Специально для тебя — <b>скидка 30%</b> на тариф Lite:\n"
                f"<s>{full_price / 100:.0f}₽</s> → <b>{discount_price_rub:.0f}₽/мес</b>\n\n"
                "• 30 ГБ bypass\n"
                "• Безлимит обычного VPN\n"
                "• До 3 устройств",
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
            SELECT u.user_id, u.subscription_tier, u.device_limit,
                   u.bypass_traffic_limit_gb,
                   COALESCE(u.bypass_traffic_used_bytes, 0) as used_bytes
            FROM users u
            WHERE u.subscription_tier IN ('standard', 'pro')
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

    for row in rows:
        user_id = row["user_id"]
        old_tier = row["subscription_tier"]
        used_gb = row["used_bytes"] / (1024 ** 3) if row["used_bytes"] else 0
        devices = row.get("device_limit") or 3

        # Find best matching tier based on actual usage
        if used_gb <= 30 and devices <= 3:
            offer_plan_id = "lite_1m"
        elif used_gb <= 100 and devices <= 5:
            offer_plan_id = "standard_1m"
        else:
            offer_plan_id = "pro_1m"

        offer_plan = plans.get(offer_plan_id)
        if not offer_plan:
            continue

        offer_price = offer_plan["price_rub"] / 100.0
        offer_name = offer_plan["title"].split("·")[0].strip()

        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text=f"💎 {offer_name} за {offer_price:.0f}₽/мес",
                callback_data=f"tier_select:{offer_plan['tier']}",
            ))
            await bot.send_message(
                user_id,
                f"👋 <b>Мы по тебе скучаем!</b>\n\n"
                f"Проанализировали твоё использование:\n"
                f"• Устройств: ~{devices}\n"
                f"• Bypass: ~{used_gb:.0f} ГБ/мес\n\n"
                f"Подобрали индивидуальный тариф — <b>{offer_name}</b> "
                f"за <b>{offer_price:.0f}₽/мес</b>. "
                f"Ровно столько, сколько тебе нужно!",
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
# 4. Clicked referral/gift button, 1 hour later nobody registered
# ---------------------------------------------------------------------------

async def _notify_referral_no_invites(bot: Bot, config: AppConfig) -> None:
    """Users who opened referral screen 1+ hour ago but got no new referrals."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_tier, u.pay_subscribed
            FROM users u
            JOIN user_notifications n ON n.user_id = u.user_id
            WHERE n.notification_type = 'referral_opened'
              AND n.created_at < NOW() - INTERVAL '1 hour'
              AND n.created_at > NOW() - INTERVAL '2 hours'
              AND NOT EXISTS (
                  SELECT 1 FROM users inv
                  WHERE inv.invited_by = u.user_id
                    AND inv.created_at > n.created_at
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n2
                  WHERE n2.user_id = u.user_id AND n2.notification_type = 'referral_no_invite_offer'
              )
            LIMIT 50
            """
        )

    if not rows:
        return

    plans = await get_tier_plans()

    for row in rows:
        user_id = row["user_id"]
        tier = row.get("subscription_tier") or "lite"
        plan_id = f"{tier}_1m" if tier in TIER_ORDER else "lite_1m"
        plan = plans.get(plan_id)
        if not plan:
            continue

        discount_price = int(plan["price_rub"] * 0.9) / 100.0
        full_price = plan["price_rub"] / 100.0

        try:
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(
                text=f"🔥 {plan['title'].split('·')[0].strip()} за {discount_price:.0f}₽",
                callback_data=f"promo_referral_10:{tier}",
            ))
            b.row(InlineKeyboardButton(
                text="🎁 Попробовать ещё раз",
                callback_data="open_invite",
            ))
            await bot.send_message(
                user_id,
                "😔 Похоже, пока никто не перешёл по вашей ссылке.\n\n"
                f"Не беда! Вот <b>скидка 10%</b> лично для вас:\n"
                f"<s>{full_price:.0f}₽</s> → <b>{discount_price:.0f}₽</b>\n\n"
                "А ещё за каждого приглашённого друга — скидка на следующее списание!",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id, "referral_no_invite_offer",
                )
        except Exception as e:
            logger.debug("engagement referral_no_invites: user=%s err=%s", user_id, e)


# ---------------------------------------------------------------------------
# 5. Device reset counter — every 3rd reset suggest higher tier
# ---------------------------------------------------------------------------

async def check_device_reset_upsell(bot: Bot, user_id: int) -> None:
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

    # Find next tier up
    if current_tier not in TIER_ORDER:
        return
    idx = TIER_ORDER.index(current_tier)
    if idx >= len(TIER_ORDER) - 1:
        return  # Already on Pro

    next_tier = TIER_ORDER[idx + 1]
    next_info = TIERS[next_tier]

    try:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(
            text=f"⬆️ Перейти на {next_info['name']}",
            callback_data=f"tier_upgrade:{next_tier}",
        ))
        await bot.send_message(
            user_id,
            f"💡 <b>Часто сбрасываете устройства?</b>\n\n"
            f"На тарифе <b>{next_info['name']}</b> доступно до "
            f"<b>{next_info['max_devices']} устройств</b> — "
            f"не придётся ничего отключать!\n\n"
            f"• {next_info['bypass_gb']} ГБ bypass\n"
            f"• До {next_info['max_devices']} устройств",
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )
    except Exception as e:
        logger.debug("engagement device_reset upsell: user=%s err=%s", user_id, e)
