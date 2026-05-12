"""
Минимальная обработка tier-платежей через вебхуки (YooKassa / CryptoPay).
Совместима с main, где нет полной системы тарифов — просто активирует подписку.
"""
import logging
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import AppConfig
from .database import get_connection
from .subscriptions import create_or_activate_keys_for_all_servers, extend_subscription, set_new_subscription
from .traffic import apply_subscription_anchor_on_payment

logger = logging.getLogger(__name__)

TIER_PLAN_DURATIONS = {
    "lite_1m": 1, "lite_3m": 3, "lite_12m": 12,
    "standard_1m": 1, "standard_3m": 3, "standard_12m": 12,
    "pro_1m": 1, "pro_3m": 3, "pro_12m": 12,
}

TIER_NAMES = {
    "lite": "Lite", "standard": "Standard", "pro": "Pro",
}


def _plan_tier(plan_id: str) -> str:
    return plan_id.rsplit("_", 1)[0] if "_" in plan_id else plan_id


def _payment_amount_cents(payment_obj: dict) -> int:
    amt = payment_obj.get("amount")
    if isinstance(amt, dict):
        try:
            return int(round(float(amt.get("value", 0)) * 100))
        except (TypeError, ValueError):
            pass
    return 0


async def process_tier_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
) -> bool:
    """Process webhook (YooKassa/CryptoPay) payment for tier subscription."""
    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan_id")
    payment_source = metadata.get("payment_source", "bot")

    if user_id is None or plan_id is None:
        logger.warning("tier webhook %s missing metadata", payment_id)
        return False
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    duration = TIER_PLAN_DURATIONS.get(plan_id)
    if duration is None:
        logger.error("tier webhook: unknown plan_id %s", plan_id)
        return False

    tier = _plan_tier(plan_id)
    tier_name = TIER_NAMES.get(tier, tier.capitalize())
    amount_cents = _payment_amount_cents(payment_obj)

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1",
            payment_id,
        )
        if existing and existing["status"] == "completed":
            logger.warning("tier payment %s already processed", payment_id)
            return False

        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE user_id = $1", user_id
        )
        if not user_exists:
            logger.error("tier webhook: user %s not found", user_id)
            return False

        row = await conn.fetchrow(
            "SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1",
            user_id,
        )
        is_active = (
            row
            and row["pay_subscribed"]
            and row["subscription_end"]
            and row["subscription_end"].date() >= datetime.now().date()
        ) if row else False

        async with conn.transaction():
            if is_active:
                await extend_subscription(user_id, duration, conn)
            else:
                await set_new_subscription(user_id, duration, conn)

            await apply_subscription_anchor_on_payment(conn, user_id)

            if existing:
                await conn.execute(
                    "UPDATE payments SET status = 'completed', amount = $1, payment_source = $2 WHERE yookassa_payment_id = $3",
                    amount_cents, payment_source, payment_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    user_id, amount_cents, "RUB", plan_id, "tier",
                    "completed", payment_id, payment_source,
                )

    await create_or_activate_keys_for_all_servers(user_id)

    if bot:
        try:
            sub_end = None
            async with get_connection() as conn2:
                r = await conn2.fetchrow(
                    "SELECT subscription_end FROM users WHERE user_id = $1", user_id
                )
                if r:
                    sub_end = r["subscription_end"]
            end_str = sub_end.strftime("%d.%m.%Y") if sub_end else "—"
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
            await bot.send_message(
                user_id,
                f"✅ <b>Тариф {tier_name} активирован!</b>\n\n"
                f"📅 Подписка до: <b>{end_str}</b>\n"
                f"Срок: {duration} мес.",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
        except Exception as e:
            logger.error("tier webhook notify: %s", e)

    for admin_id in config.bot.admin_ids:
        if admin_id != user_id and bot:
            try:
                await bot.send_message(
                    admin_id,
                    f"💎 <b>Покупка тарифа (вебхук)</b>\n"
                    f"User: <code>{user_id}</code>\n"
                    f"План: {plan_id} ({tier_name})\n"
                    f"Сумма: {amount_cents / 100:.2f}₽",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    logger.info("tier webhook %s completed for user %s, plan %s", payment_id, user_id, plan_id)
    return True


async def process_tier_upgrade_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
) -> bool:
    """Process webhook payment for tier upgrade (minimal: just record payment)."""
    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan_id")

    if user_id is None or plan_id is None:
        return False
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    tier = _plan_tier(plan_id)
    tier_name = TIER_NAMES.get(tier, tier.capitalize())
    amount_cents = _payment_amount_cents(payment_obj)

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1", payment_id
        )
        if existing and existing["status"] == "completed":
            return False

        async with conn.transaction():
            if existing:
                await conn.execute(
                    "UPDATE payments SET status = 'completed' WHERE yookassa_payment_id = $1",
                    payment_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    user_id, amount_cents, "RUB", plan_id, "tier_upgrade",
                    "completed", payment_id, "bot",
                )

    if bot:
        try:
            await bot.send_message(
                user_id,
                f"✅ <b>Апгрейд до {tier_name} оплачен!</b>\n\n"
                f"Изменения вступят в силу после обновления системы.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True


async def process_bypass_pack_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
) -> bool:
    """Process webhook payment for bypass GB pack (minimal)."""
    user_id = metadata.get("user_id")
    pack_id = metadata.get("pack_id")

    if user_id is None or pack_id is None:
        return False
    try:
        user_id = int(user_id)
        pack_id = int(str(pack_id).strip())
    except (TypeError, ValueError):
        return False

    amount_cents = _payment_amount_cents(payment_obj)

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1", payment_id
        )
        if existing and existing["status"] == "completed":
            return False

        async with conn.transaction():
            if existing:
                await conn.execute(
                    "UPDATE payments SET status = 'completed' WHERE yookassa_payment_id = $1",
                    payment_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    user_id, amount_cents, "RUB", f"bypass_pack:{pack_id}",
                    "bypass_pack", "completed", payment_id, "bot",
                )

    if bot:
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Bypass-пакет оплачен!</b>\nДополнительные ГБ добавлены.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True
