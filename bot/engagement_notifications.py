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
from .custom_emojis import E, e, lbl, btn, emoji_button, raw

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


async def run_referral_trial_backfill(bot: Bot) -> None:
    """
    Разовая рассылка: реферал/UTM пользователи, кому доступен Plus за 1₽,
    но ещё не получали это предложение.
    """
    from .trial_usage import get_trial_days, referral_trial_offer_text, user_show_referral_trial_offer

    async with get_connection() as conn:
        trial_days = await get_trial_days(conn)
        if trial_days <= 0:
            return
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.blacklisted = FALSE
              AND (
                  u.invited_by IS NOT NULL
                  OR NULLIF(TRIM(u.utm_source), '') IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM utm_visits v WHERE v.user_id = u.user_id
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id
                    AND n.notification_type = 'referral_trial_1rub_offer'
              )
            LIMIT 300
            """
        )

    sent = 0
    for row in rows:
        user_id = row["user_id"]
        try:
            async with get_connection() as conn:
                if not await user_show_referral_trial_offer(conn, user_id):
                    continue
                await conn.execute(
                    """
                    INSERT INTO user_notifications (user_id, notification_type)
                    VALUES ($1, $2)
                    """,
                    user_id,
                    "referral_trial_1rub_offer",
                )
            b = InlineKeyboardBuilder()
            b.row(btn("Plus за 1₽ — попробовать", "gift",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                referral_trial_offer_text(trial_days),
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            sent += 1
        except Exception as e:
            logger.debug("referral trial backfill: user=%s err=%s", user_id, e)
    if sent:
        logger.info("referral trial backfill: sent=%s", sent)


# ---------------------------------------------------------------------------
# 1. Registered but idle — send gift + trial button
# ---------------------------------------------------------------------------

async def _notify_idle_new_users(bot: Bot) -> None:
    """Free-пользователи 1–7 дней после регистрации с рефералом/UTM и доступным триалом 1₽."""
    async with get_connection() as conn:
        from .trial_usage import user_show_referral_trial_offer

        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.registration_date < NOW() - INTERVAL '1 day'
              AND u.registration_date > NOW() - INTERVAL '7 days'
              AND u.blacklisted = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = 'idle_new_gift'
              )
            LIMIT 80
            """
        )

    for row in rows:
        user_id = row["user_id"]
        async with get_connection() as conn:
            if not await user_show_referral_trial_offer(conn, user_id):
                continue
        try:
            b = InlineKeyboardBuilder()
            b.row(btn("Plus за 1₽ — попробовать", "gift",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                f"{E.gift} <b>У нас для тебя подарок!</b>\n\n"
                "Мы подготовили VPN, который работает быстро и стабильно — "
                "YouTube, TikTok, ChatGPT без блокировок.\n\n"
                f"{E.bypass} 50 ГБ bypass в месяц\n"
                f"{E.devices} Безлимит устройств\n"
                f"{E.activate} Подключение за 30 сек\n\n"
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
            b.row(btn("Plus за {discount_price_rub:.0f}₽/мес (-30%)", "hot",
                callback_data="promo_plus_30",
            ))
            await bot.send_message(
                user_id,
                f"{E.wave} <b>Привет!</b>\n\n"
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
            b.row(btn("Plus за {offer_price:.0f}₽/мес", "plus",
                callback_data="tier_select:plus:plus_1m",
            ))
            await bot.send_message(
                user_id,
                f"{E.wave} <b>Мы по тебе скучаем!</b>\n\n"
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
    Plus закончился 30+ дней назад (триал, покупка, UTM, реферал — без разницы),
    карта не привязана — снова предлагаем Plus за 1₽ для привязки карты.
    """
    from .trial_usage import can_retry_trial_after_lapse, sync_trial_used_flag, user_show_referral_trial_offer

    async with get_connection() as conn:
        candidates = await conn.fetch(
            """
            SELECT user_id FROM users
            WHERE blacklisted = FALSE
              AND yookassa_recurring_payment_method_id IS NULL
              AND (
                  last_plus_ended_at IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM payments p
                      WHERE p.user_id = users.user_id
                        AND p.status = 'completed'
                        AND p.currency = 'RUB'
                  )
              )
            LIMIT 200
            """
        )

    for row in candidates:
        user_id = row["user_id"]
        try:
            async with get_connection() as conn:
                if not await can_retry_trial_after_lapse(conn, user_id):
                    continue
                if not await user_show_referral_trial_offer(conn, user_id):
                    continue
                already = await conn.fetchval(
                    """
                    SELECT 1 FROM user_notifications
                    WHERE user_id = $1
                      AND notification_type = 'lapsed_paid_trial_reset'
                      AND created_at > NOW() - INTERVAL '30 days'
                    """,
                    user_id,
                )
                if already:
                    continue
                await sync_trial_used_flag(conn, user_id)
                await conn.execute(
                    "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
                    user_id,
                    "lapsed_paid_trial_reset",
                )

            b = InlineKeyboardBuilder()
            b.row(btn("Plus за 1₽ — попробовать снова", "gift",
                callback_data="activate_trial",
            ))
            await bot.send_message(
                user_id,
                f"{E.wave} <b>Давно не виделись!</b>\n\n"
                "Прошёл месяц с момента окончания вашей подписки.\n\n"
                "Специально для вас мы возвращаем <b>предложение Plus за 1₽</b> — "
                "попробуйте снова:\n\n"
                f"{E.bypass} 50 ГБ bypass в месяц\n"
                f"{E.devices} Безлимит устройств\n"
                f"{E.activate} YouTube / TikTok / AI работают\n\n"
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
            b.row(btn("Перейти на Plus", "plus",
                callback_data="open_tiers",
            ))
            await bot.send_message(
                user_id,
                f"{E.bulb} <b>Часто сбрасываете устройства?</b>\n\n"
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
