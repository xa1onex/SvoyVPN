"""
Обработчики платежей (Telegram Stars и YooKassa)
"""
import logging
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..esim_invoice_payload import parse_stars_or_yoo_esim_payload
from ..payments import (
    process_esim_telegram_invoice_payment,
    process_telegram_stars_payment,
    process_telegram_gb_pack_payment,
)
from ..tier_payments import (
    process_tier_stars_payment,
    process_bypass_pack_stars_payment,
)
from ..plans import get_subscription_plans, get_renewal_plans, PAYMENT_METHODS
from ..config import AppConfig
from ..custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)


async def setup_payment_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики платежей"""
    
    @dp.pre_checkout_query()
    async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        """Обработка pre-checkout запроса"""
        payload = pre_checkout_query.invoice_payload or ""
        from ..plans import is_active_tier_plan, is_legacy_subscription_plan

        if payload.startswith("tier|") or payload.startswith("tier_upgrade|"):
            await pre_checkout_query.answer(
                ok=False,
                error_message="Оплата подписки Stars недоступна. Используйте карту.",
            )
            return

        if payload.startswith("stars_"):
            pl = payload.replace("_miniapp", "")
            parts = pl.split("_")
            if len(parts) >= 3:
                plan_part = "_".join(parts[2:-1] if parts[-1].isdigit() else parts[2:])
                if is_active_tier_plan(plan_part) or is_legacy_subscription_plan(plan_part):
                    await pre_checkout_query.answer(
                        ok=False,
                        error_message="Оплата подписки Stars недоступна. Используйте карту.",
                    )
                    return

        await pre_checkout_query.answer(ok=True)
    
    @dp.message(F.successful_payment)
    async def handle_successful_payment(message: Message):
        """Обработка успешного платежа через Telegram Stars"""
        try:
            payload = message.successful_payment.invoice_payload
            logger.info(f"Processing successful payment with payload: {payload}")

            esim_parsed = parse_stars_or_yoo_esim_payload(payload or "")
            if esim_parsed:
                kind, uid_enc, loc, pkg = esim_parsed
                if int(uid_enc) != int(message.from_user.id):
                    raise ValueError("user mismatch eSIM invoice")
                method_id = "stars" if kind == "stars" else "yookassa"
                source = "miniapp" if (payload or "").endswith("_miniapp") else "bot"
                await process_esim_telegram_invoice_payment(
                    message=message,
                    bot=bot,
                    config=config,
                    method_id=method_id,
                    source=source,
                    location_code=loc,
                    package_code=pkg,
                )
                return

            raw_pl = payload or ""

            # --- New tier system payloads ---
            if raw_pl.startswith("tier|"):
                parts = raw_pl.split("|")
                plan_id = parts[1] if len(parts) > 1 else ""
                await process_tier_stars_payment(message, bot, plan_id, config, source="bot")
                return

            if raw_pl.startswith("tier_upgrade|"):
                # Upgrade flow removed (single Plus tier); treat as regular tier purchase
                parts = raw_pl.split("|")
                plan_id = parts[1] if len(parts) > 1 else ""
                await process_tier_stars_payment(message, bot, plan_id, config, source="bot")
                return

            if raw_pl.startswith("bypass_pack|"):
                parts = raw_pl.split("|")
                try:
                    pack_id = int(parts[2]) if len(parts) > 2 else 0
                except (ValueError, IndexError):
                    raise ValueError("bad bypass_pack payload")
                await process_bypass_pack_stars_payment(message, bot, pack_id, config, source="bot")
                return

            # --- Legacy payloads ---
            if raw_pl.startswith("stars_gbpack_") or raw_pl.startswith("yoo_gbpack_"):
                is_mini = raw_pl.endswith("_miniapp")
                src = "miniapp" if is_mini else "bot"
                pl = raw_pl[:-8] if is_mini else raw_pl
                parts = pl.split("_")
                if len(parts) < 5 or parts[1] != "gbpack":
                    raise ValueError("bad gbpack payload")
                uid_enc = int(parts[2])
                pack_id_enc = int(parts[3])
                if uid_enc != int(message.from_user.id):
                    raise ValueError("user mismatch gb pack invoice")
                pref = parts[0]
                mid = "stars" if pref == "stars" else "yookassa"
                await process_telegram_gb_pack_payment(
                    message,
                    bot,
                    pack_id=pack_id_enc,
                    config=config,
                    source=src,
                    method_id=mid,
                )
                return

            source = 'bot'
            if raw_pl.startswith("stars_"):
                method_id = "stars"
                pl = raw_pl
                if pl.endswith("_miniapp"):
                    source = 'miniapp'
                    pl = pl[:-8]
                parts = pl.split("_")
                if len(parts) < 4:
                    raise ValueError("bad stars payload")
                if int(parts[1]) != int(message.from_user.id):
                    raise ValueError("user mismatch subscription invoice")
                if len(parts) >= 5 and parts[-2].isdigit() and int(parts[-2]) < 1_000_000_000:
                    plan_id = "_".join(parts[2:-2])
                else:
                    plan_id = "_".join(parts[2:-1])
            elif raw_pl.startswith("yoo_"):
                method_id = "yookassa"
                pl = raw_pl
                if pl.endswith("_miniapp"):
                    source = 'miniapp'
                    pl = pl[:-8]
                parts = pl.split("_")
                if len(parts) < 4:
                    raise ValueError("bad yoo payload")
                if int(parts[1]) != int(message.from_user.id):
                    raise ValueError("user mismatch subscription invoice")
                if len(parts) >= 5 and parts[-2].isdigit() and int(parts[-2]) < 1_000_000_000:
                    plan_id = "_".join(parts[2:-2])
                else:
                    plan_id = "_".join(parts[2:-1])
            elif "|" in raw_pl:
                parts = raw_pl.split("|")
                plan_id = parts[0]
                method_id = parts[1]
                if len(parts) > 3 and parts[3] == "miniapp":
                    source = 'miniapp'
            else:
                plan_id = raw_pl
                method_id = "stars" if message.successful_payment.currency == "XTR" else "yookassa"
            
            # Получаем планы
            from ..plans import is_active_tier_plan, is_legacy_subscription_plan

            if is_legacy_subscription_plan(plan_id):
                raise ValueError(f"Legacy plan rejected: {plan_id}")

            if is_active_tier_plan(plan_id):
                await process_tier_stars_payment(message, bot, plan_id, config, source=source)
                return

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
                config=config,
                source=source,
            )
            
        except Exception as e:
            logger.error(f"Ошибка обработки платежа: {e}", exc_info=True)
            await message.answer(
                f"{E.error} Произошла ошибка при обработке платежа. "
                "Пожалуйста, обратитесь в поддержку."
            )
