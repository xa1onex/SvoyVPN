"""Персональные скидки из бота техподдержки (callback personal_promo:ID)."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import AppConfig
from ..database import get_connection
from ..plans import TIERS, format_price_rub, get_tier_plans
from ..yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


async def setup_personal_promo_handlers(dp, bot: Bot, config: AppConfig) -> None:
    @dp.callback_query(F.data.startswith("personal_promo:"))
    async def handle_personal_promo(callback: CallbackQuery):
        user_id = callback.from_user.id
        try:
            offer_id = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("❌ Неверный оффер", show_alert=True)
            return

        async with get_connection() as conn:
            offer = await conn.fetchrow(
                "SELECT * FROM support_personal_promo_offers WHERE id=$1", offer_id
            )

        if not offer:
            await callback.answer("❌ Предложение не найдено", show_alert=True)
            return
        if int(offer["user_id"]) != user_id:
            await callback.answer("❌ Это предложение не для вас", show_alert=True)
            return
        if offer["status"] != "pending":
            await callback.answer("❌ Предложение уже использовано или отменено", show_alert=True)
            return
        if offer["expires_at"] and offer["expires_at"] < datetime.now():
            await callback.answer("❌ Срок предложения истёк", show_alert=True)
            return

        plan_id = offer["plan_id"]
        price = int(offer["price_rub"])
        plans = await get_tier_plans()
        if plan_id not in plans:
            await callback.answer("❌ План недоступен", show_alert=True)
            return

        plan = plans[plan_id]
        tier_id = plan["tier"]
        t = TIERS.get(tier_id, {})

        if not config.yookassa.enabled:
            await callback.answer("❌ Оплата временно недоступна", show_alert=True)
            return

        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            payment_data = yk.create_payment(
                amount=price / 100.0,
                description=f"VPN {plan['title']} (персональная скидка {offer['discount_percent']}%)",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": plan_id,
                    "method_id": "yookassa",
                    "product_type": "tier",
                    "personal_promo_id": str(offer_id),
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id,
                    price,
                    "RUB",
                    plan_id,
                    "tier",
                    "pending",
                    payment_data["id"],
                )

            warn = ""
            if offer["has_recurring_at_create"]:
                warn = (
                    "\n\n<i>Автопродление по карте списывает полную цену в дату продления. "
                    "Отключите автопродление в разделе подписки, если нужна только эта оплата.</i>"
                )

            b = InlineKeyboardBuilder()
            b.row(
                InlineKeyboardButton(
                    text="💳 Перейти к оплате",
                    url=payment_data["confirmation_url"],
                )
            )
            b.row(InlineKeyboardButton(text="◀️ Тарифы", callback_data="open_tiers"))

            await callback.message.edit_text(
                f"🔥 <b>{t.get('name', tier_id)}</b> · скидка <b>{offer['discount_percent']}%</b>\n\n"
                f"К оплате: <b>{format_price_rub(price)}</b> "
                f"<s>{format_price_rub(offer['base_price_rub'])}</s>{warn}",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
        except Exception as e:
            logger.error("personal_promo %s: %s", offer_id, e, exc_info=True)
            await callback.answer("❌ Ошибка создания платежа", show_alert=True)
