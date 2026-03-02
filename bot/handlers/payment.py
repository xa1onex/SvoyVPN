"""
Обработчики платежей (Telegram Stars и YooKassa)
"""
import logging
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..payments import process_telegram_stars_payment
from ..plans import get_subscription_plans, get_renewal_plans, PAYMENT_METHODS
from ..config import AppConfig

logger = logging.getLogger(__name__)


async def setup_payment_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики платежей"""
    
    @dp.pre_checkout_query()
    async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        """Обработка pre-checkout запроса"""
        await pre_checkout_query.answer(ok=True)
    
    @dp.message(F.successful_payment)
    async def handle_successful_payment(message: Message):
        """Обработка успешного платежа через Telegram Stars"""
        try:
            payload = message.successful_payment.invoice_payload
            logger.info(f"Processing successful payment with payload: {payload}")
            
            plan_id = None
            method_id = None
            
            if payload.startswith("stars_"):
                method_id = "stars"
                parts = payload.split("_")
                # stars_{user_id}_{tariff_id}_{timestamp}
                # tariff_id can have underscores (e.g. 1_month)
                plan_id = "_".join(parts[2:-1])
            elif payload.startswith("yoo_"):
                method_id = "yookassa"
                parts = payload.split("_")
                # yoo_{user_id}_{tariff_id}_{timestamp}
                plan_id = "_".join(parts[2:-1])
            elif "|" in payload:
                parts = payload.split("|")
                plan_id = parts[0]
                method_id = parts[1]
            else:
                # Fallback: keep existing logic if it might be just plan_id
                plan_id = payload
                method_id = "stars" if message.successful_payment.currency == "XTR" else "yookassa"
            
            # Получаем планы
            subscription_plans = await get_subscription_plans()
            renewal_plans = await get_renewal_plans()
            
            # Определяем тип подписки
            if plan_id in subscription_plans:
                plan_data = subscription_plans[plan_id]
                is_new_subscription = True
            elif plan_id in renewal_plans:
                plan_data = renewal_plans[plan_id]
                is_new_subscription = False
            else:
                raise ValueError(f"Неизвестный план: {plan_id}")
            
            # Валидация метода оплаты
            if method_id not in PAYMENT_METHODS:
                raise ValueError(f"Неизвестный метод оплаты: {method_id}")
            
            method_data = PAYMENT_METHODS[method_id]
            
            # Обрабатываем платеж
            await process_telegram_stars_payment(
                message=message,
                bot=bot,
                plan_id=plan_id,
                plan_data=plan_data,
                method_data=method_data,
                is_new_subscription=is_new_subscription,
                config=config
            )
            
        except Exception as e:
            logger.error(f"Ошибка обработки платежа: {e}", exc_info=True)
            await message.answer(
                "❌ Произошла ошибка при обработке платежа. "
                "Пожалуйста, обратитесь в поддержку."
            )
