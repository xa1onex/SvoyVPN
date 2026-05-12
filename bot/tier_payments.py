"""
Обработка платежей для новой системы тарифов (Lite/Standard/Pro).
Покупка тарифа, апгрейд, докупка bypass ГБ.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import AppConfig
from .database import get_connection
from .plans import (
    TIERS,
    get_tier_bypass_gb,
    get_tier_max_devices,
    get_tier_plans,
)
from .subscriptions import (
    create_or_activate_keys_for_all_servers,
    extend_subscription,
    set_new_subscription,
    sync_user_keys,
)
from .traffic import apply_subscription_anchor_on_payment, ensure_bypass_period

logger = logging.getLogger(__name__)


def _yookassa_saved_payment_method_id(payment_obj: dict) -> Optional[str]:
    """
    Извлекает payment_method.id только если saved == true.
    https://yookassa.ru/developers/payment-acceptance/scenario-extensions/recurring-payments/save-payment-method/save-during-payment#save-mandatory
    """
    pm = payment_obj.get("payment_method")
    if isinstance(pm, dict) and pm.get("saved") is True:
        return pm.get("id")
    return None


async def activate_tier_subscription(
    conn,
    user_id: int,
    plan_id: str,
    plan_data: dict,
    price_paid: int,
) -> None:
    """
    Activate or extend subscription with the new tier system.
    Sets tier, bypass limit, device limit, duration info.
    """
    tier = plan_data["tier"]
    duration = plan_data["duration"]
    bypass_gb = plan_data["bypass_gb"]
    max_devices = plan_data["max_devices"]

    row = await conn.fetchrow(
        "SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1",
        user_id,
    )
    is_active = (
        row
        and row["pay_subscribed"]
        and row["subscription_end"]
        and row["subscription_end"].date() >= datetime.now().date()
    )

    if is_active:
        await extend_subscription(user_id, duration, conn)
    else:
        await set_new_subscription(user_id, duration, conn)

    await conn.execute(
        """
        UPDATE users SET
            subscription_tier = $1,
            bypass_traffic_limit_gb = $2,
            device_limit = $3,
            tier_duration_months = $4,
            tier_price_paid = $5,
            tier_purchased_at = NOW(),
            bypass_traffic_used_bytes = CASE
                WHEN bypass_period_start IS NULL THEN 0
                ELSE bypass_traffic_used_bytes
            END
        WHERE user_id = $6
        """,
        tier,
        bypass_gb,
        max_devices,
        duration,
        price_paid,
        user_id,
    )
    await apply_subscription_anchor_on_payment(conn, user_id)
    await ensure_bypass_period(conn, user_id)


async def apply_tier_upgrade(
    conn,
    user_id: int,
    plan_id: str,
    plan_data: dict,
    price_paid: int,
) -> None:
    """
    Apply tier upgrade: change tier, update bypass limit immediately.
    Subscription end date stays the same.
    """
    tier = plan_data["tier"]
    bypass_gb = plan_data["bypass_gb"]
    max_devices = plan_data["max_devices"]

    old_row = await conn.fetchrow(
        "SELECT tier_price_paid FROM users WHERE user_id = $1", user_id
    )
    old_paid = int(old_row["tier_price_paid"] or 0) if old_row else 0
    new_total_paid = old_paid + price_paid

    await conn.execute(
        """
        UPDATE users SET
            subscription_tier = $1,
            bypass_traffic_limit_gb = $2,
            device_limit = $3,
            tier_price_paid = $4
        WHERE user_id = $5
        """,
        tier,
        bypass_gb,
        max_devices,
        new_total_paid,
        user_id,
    )


async def apply_bypass_pack(conn, user_id: int, gb_amount: int) -> None:
    """Add bypass bonus GB to user's current period."""
    await ensure_bypass_period(conn, user_id)
    await conn.execute(
        """
        UPDATE users
        SET bypass_bonus_gb = COALESCE(bypass_bonus_gb, 0) + $1
        WHERE user_id = $2
        """,
        gb_amount,
        user_id,
    )


async def process_tier_stars_payment(
    message: Message,
    bot: Bot,
    plan_id: str,
    config: AppConfig,
    source: str = "bot",
) -> bool:
    """Process Stars payment for a new tier subscription."""
    user_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    provider_charge_id = message.successful_payment.provider_payment_charge_id
    currency = message.successful_payment.currency
    total_amount = message.successful_payment.total_amount

    plans = await get_tier_plans()
    if plan_id not in plans:
        logger.error("tier payment: unknown plan %s", plan_id)
        await message.answer("❌ Тариф не найден.")
        return False

    plan_data = plans[plan_id]
    tier_info = TIERS.get(plan_data["tier"], {})

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1",
            charge_id,
        )
        if existing and existing["status"] == "completed":
            await message.answer("✅ Этот платёж уже обработан.")
            return False

        async with conn.transaction():
            await activate_tier_subscription(
                conn, user_id, plan_id, plan_data, plan_data["price_rub"]
            )

            if existing:
                await conn.execute(
                    """
                    UPDATE payments SET status = 'completed', amount = $1, currency = $2
                    WHERE telegram_payment_charge_id = $3
                    """,
                    total_amount, currency, charge_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status,
                     telegram_payment_charge_id, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    user_id, total_amount, currency, plan_id, "tier",
                    "completed", charge_id, provider_charge_id, source,
                )

        await create_or_activate_keys_for_all_servers(user_id)

    sub_end = None
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT subscription_end FROM users WHERE user_id = $1", user_id
        )
        if row:
            sub_end = row["subscription_end"]

    end_str = sub_end.strftime("%d.%m.%Y") if sub_end else "—"
    receipt = (
        f"✅ <b>Тариф {tier_info.get('name', '')} активирован!</b>\n\n"
        f"📅 Подписка до: <b>{end_str}</b>\n"
        f"🔓 Bypass: <b>{plan_data['bypass_gb']} ГБ/мес</b>\n"
        f"📱 Устройств: до {plan_data['max_devices']}\n"
        f"🌐 Обычный VPN: безлимит\n\n"
        f"Оплачено: {total_amount} Stars\n"
        f"ID: <code>{charge_id}</code>"
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
    await message.answer(receipt, parse_mode="HTML", reply_markup=b.as_markup())

    for admin_id in config.bot.admin_ids:
        if admin_id != user_id:
            try:
                await bot.send_message(
                    admin_id,
                    f"💎 <b>Покупка тарифа</b>\n"
                    f"User: {user_id}\n"
                    f"Тариф: {plan_data['title']}\n"
                    f"Сумма: {total_amount} Stars",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    return True


async def process_tier_upgrade_stars_payment(
    message: Message,
    bot: Bot,
    plan_id: str,
    config: AppConfig,
    source: str = "bot",
) -> bool:
    """Process Stars payment for a tier upgrade."""
    user_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    provider_charge_id = message.successful_payment.provider_payment_charge_id
    currency = message.successful_payment.currency
    total_amount = message.successful_payment.total_amount

    plans = await get_tier_plans()
    if plan_id not in plans:
        await message.answer("❌ Тариф не найден.")
        return False

    plan_data = plans[plan_id]
    tier_info = TIERS.get(plan_data["tier"], {})

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1",
            charge_id,
        )
        if existing and existing["status"] == "completed":
            await message.answer("✅ Этот платёж уже обработан.")
            return False

        async with conn.transaction():
            price_rub_equiv = total_amount * 100
            await apply_tier_upgrade(
                conn, user_id, plan_id, plan_data, price_rub_equiv
            )

            if existing:
                await conn.execute(
                    "UPDATE payments SET status = 'completed' WHERE telegram_payment_charge_id = $1",
                    charge_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status,
                     telegram_payment_charge_id, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    user_id, total_amount, currency, plan_id, "tier_upgrade",
                    "completed", charge_id, provider_charge_id, source,
                )

    await message.answer(
        f"✅ <b>Тариф повышен до {tier_info.get('name', '')}!</b>\n\n"
        f"🔓 Bypass: <b>{plan_data['bypass_gb']} ГБ/мес</b>\n"
        f"📱 Устройств: до {plan_data['max_devices']}\n"
        f"Лимит обновлён сразу.",
        parse_mode="HTML",
    )
    return True


async def process_bypass_pack_stars_payment(
    message: Message,
    bot: Bot,
    pack_id: int,
    config: AppConfig,
    source: str = "bot",
) -> bool:
    """Process Stars payment for bypass GB pack."""
    user_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    provider_charge_id = message.successful_payment.provider_payment_charge_id
    currency = message.successful_payment.currency
    total_amount = message.successful_payment.total_amount

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1",
            charge_id,
        )
        if existing and existing["status"] == "completed":
            await message.answer("✅ Этот платёж уже обработан.")
            return False

        pack = await conn.fetchrow(
            "SELECT id, title, gb_amount FROM bypass_pack_products WHERE id = $1 AND is_active = TRUE",
            pack_id,
        )
        if not pack:
            await message.answer("❌ Пакет недоступен.")
            return False

        async with conn.transaction():
            await apply_bypass_pack(conn, user_id, int(pack["gb_amount"]))

            if existing:
                await conn.execute(
                    "UPDATE payments SET status = 'completed' WHERE telegram_payment_charge_id = $1",
                    charge_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, amount, currency, plan_id, plan_type, status,
                     telegram_payment_charge_id, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    user_id, total_amount, currency, f"bypass_pack:{pack_id}",
                    "bypass_pack", "completed", charge_id, provider_charge_id, source,
                )

    await message.answer(
        f"✅ <b>+{pack['gb_amount']} ГБ bypass</b> добавлено!\n\n"
        f"Пакет: {pack['title']}\n"
        f"Оплачено: {total_amount} Stars",
        parse_mode="HTML",
    )
    return True


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
        return False
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    plans = await get_tier_plans()
    if plan_id not in plans:
        logger.error("tier webhook: unknown plan %s", plan_id)
        return False

    plan_data = plans[plan_id]
    tier_info = TIERS.get(plan_data["tier"], {})

    amount_cents = 0
    amt = payment_obj.get("amount")
    if isinstance(amt, dict):
        try:
            amount_cents = int(round(float(amt.get("value", 0)) * 100))
        except (TypeError, ValueError):
            pass
    if not amount_cents:
        amount_cents = plan_data["price_rub"]

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1",
            payment_id,
        )
        if existing and existing["status"] == "completed":
            return False

        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE user_id = $1", user_id
        )
        if not user_exists:
            return False

        async with conn.transaction():
            await activate_tier_subscription(
                conn, user_id, plan_id, plan_data, amount_cents
            )
            pm_id = _yookassa_saved_payment_method_id(payment_obj)
            if pm_id:
                await conn.execute(
                    """
                    UPDATE users SET yookassa_recurring_payment_method_id = $1
                    WHERE user_id = $2
                    """,
                    pm_id,
                    user_id,
                )
                logger.info(
                    "Saved payment_method_id=%s for user=%s tier=%s",
                    pm_id, user_id, plan_data.get("tier"),
                )
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
            async with get_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT subscription_end FROM users WHERE user_id = $1", user_id
                )
                if row:
                    sub_end = row["subscription_end"]
            end_str = sub_end.strftime("%d.%m.%Y") if sub_end else "—"
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
            await bot.send_message(
                user_id,
                f"✅ <b>Тариф {tier_info.get('name', '')} активирован!</b>\n\n"
                f"📅 Подписка до: <b>{end_str}</b>\n"
                f"🔓 Bypass: <b>{plan_data['bypass_gb']} ГБ/мес</b>\n"
                f"📱 Устройств: до {plan_data['max_devices']}",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
        except Exception as e:
            logger.error("tier webhook notify: %s", e)

    return True


async def process_tier_upgrade_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
) -> bool:
    """Process webhook payment for tier upgrade."""
    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan_id")

    if user_id is None or plan_id is None:
        return False
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    plans = await get_tier_plans()
    if plan_id not in plans:
        return False

    plan_data = plans[plan_id]
    tier_info = TIERS.get(plan_data["tier"], {})

    amount_cents = 0
    amt = payment_obj.get("amount")
    if isinstance(amt, dict):
        try:
            amount_cents = int(round(float(amt.get("value", 0)) * 100))
        except (TypeError, ValueError):
            pass

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1", payment_id
        )
        if existing and existing["status"] == "completed":
            return False

        async with conn.transaction():
            await apply_tier_upgrade(conn, user_id, plan_id, plan_data, amount_cents)
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
                f"✅ <b>Тариф повышен до {tier_info.get('name', '')}!</b>\n\n"
                f"🔓 Bypass: {plan_data['bypass_gb']} ГБ/мес\n"
                f"📱 Устройств: до {plan_data['max_devices']}",
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
    """Process webhook payment for bypass GB pack."""
    user_id = metadata.get("user_id")
    pack_id = metadata.get("pack_id")

    if user_id is None or pack_id is None:
        return False
    try:
        user_id = int(user_id)
        pack_id = int(str(pack_id).strip())
    except (TypeError, ValueError):
        return False

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1", payment_id
        )
        if existing and existing["status"] == "completed":
            return False

        pack = await conn.fetchrow(
            "SELECT id, title, gb_amount FROM bypass_pack_products WHERE id = $1 AND is_active = TRUE",
            pack_id,
        )
        if not pack:
            return False

        amount_cents = 0
        amt = payment_obj.get("amount")
        if isinstance(amt, dict):
            try:
                amount_cents = int(round(float(amt.get("value", 0)) * 100))
            except (TypeError, ValueError):
                pass

        async with conn.transaction():
            await apply_bypass_pack(conn, user_id, int(pack["gb_amount"]))
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
                f"✅ <b>+{pack['gb_amount']} ГБ bypass</b> добавлено!\n\nПакет: {pack['title']}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True
