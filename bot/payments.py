"""
Модуль для обработки платежей (Telegram Stars и YooKassa)
С исправлениями: проверка дубликатов, идемпотентность, транзакции
"""
import base64
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import esim_service
from .config import AppConfig
from .database import get_connection
from .subscriptions import create_or_activate_keys_for_all_servers, extend_subscription, set_new_subscription

logger = logging.getLogger(__name__)


def _payment_amount_rub_cents(payment_obj: dict) -> int:
    amt = payment_obj.get("amount")
    if isinstance(amt, dict):
        v = amt.get("value")
    else:
        v = amt
    if v is not None:
        try:
            return int(round(float(v) * 100))
        except (TypeError, ValueError):
            pass
    for key in ("paid_amount", "paid_fiat_amount"):
        x = payment_obj.get(key)
        if x is not None:
            try:
                return int(round(float(x) * 100))
            except (TypeError, ValueError):
                pass
    return 0


async def _finalize_esim_delivery(
    conn,
    *,
    user_id: int,
    location_code: str,
    package_code: str,
    price_kopecks: int,
    payment_ref: str,
    payment_source: str,
    amount_cents: int,
    currency: str,
    telegram_charge_id: Optional[str],
    provider_charge_id: Optional[str],
    method_label: str,
    bot: Optional[Bot],
    config: AppConfig,
) -> dict[str, Any]:
    """Создаёт заказ eSIM, пишет payments + esim_orders, шлёт пользователю QR/код."""
    mode = esim_service._cfg_mode()
    tx_id = f"svoy_{user_id}_{uuid.uuid4().hex[:16]}"

    delivery: dict[str, Any]
    provider_raw: Optional[dict] = None
    batch_no: Optional[str] = None
    ok = True
    err_msg = ""

    if mode == "live":
        ok_live, err_msg, extra = await esim_service.fulfill_order_live(tx_id, package_code)
        provider_raw = extra.get("providerResponse") if isinstance(extra, dict) else None
        batch_no = (extra or {}).get("batchOrderNo") if isinstance(extra, dict) else None
        if ok_live:
            delivery = {k: v for k, v in extra.items() if k != "providerResponse"}
        else:
            ok = False
            delivery = {"error": err_msg or "esim_failed", "details": extra}
    else:
        delivery = esim_service.test_fake_delivery(package_code)

    plan_key = f"esim:{location_code}:{package_code}"
    yk_slot = provider_charge_id or payment_ref
    if telegram_charge_id and not yk_slot:
        yk_slot = telegram_charge_id

    async with conn.transaction():
        if telegram_charge_id:
            await conn.execute(
                """
                INSERT INTO payments
                (user_id, amount, currency, plan_id, plan_type, status,
                 telegram_payment_charge_id, yookassa_payment_id, payment_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                user_id,
                amount_cents,
                currency,
                plan_key,
                "esim",
                "completed",
                telegram_charge_id,
                yk_slot,
                payment_source,
            )
        else:
            await conn.execute(
                """
                INSERT INTO payments
                (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id, payment_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                user_id,
                amount_cents,
                currency,
                plan_key,
                "esim",
                "completed",
                payment_ref,
                payment_source,
            )

        await conn.execute(
            """
            INSERT INTO esim_orders
            (user_id, transaction_id, package_code, location_code, price_kopecks,
             mode, batch_order_no, status, delivery_json, provider_raw)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            user_id,
            tx_id,
            package_code,
            location_code,
            price_kopecks,
            mode,
            batch_no,
            "completed" if ok else "failed",
            delivery,
            provider_raw,
        )

    if bot:
        try:
            if ok and delivery.get("activationCode"):
                cap = (
                    f"✅ <b>eSIM готов</b>\n\n"
                    f"Код активации:\n<code>{delivery.get('activationCode', '')}</code>\n\n"
                    f"SMDP+:\n<code>{delivery.get('smdpAddress', '')}</code>"
                )
                b64 = delivery.get("qrImagePngBase64")
                if b64:
                    raw = base64.b64decode(b64)
                    await bot.send_photo(
                        user_id,
                        BufferedInputFile(raw, filename="esim.png"),
                        caption=cap,
                        parse_mode="HTML",
                    )
                else:
                    await bot.send_message(user_id, cap, parse_mode="HTML")
            else:
                await bot.send_message(
                    user_id,
                    "✅ Оплата получена. Оформление eSIM временно не удалось — напишите в поддержку, пришлите скрин оплаты.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("eSIM notify user %s: %s", user_id, e, exc_info=True)

    return delivery


async def process_esim_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
    payment_methods: dict,
) -> bool:
    """ЮKassa / Crypto Pay: оплата eSIM (metadata.product_type=esim)."""
    user_id = metadata.get("user_id")
    loc = metadata.get("location_code") or metadata.get("esim_location")
    pkg = metadata.get("package_code") or metadata.get("esim_package")
    method_id = metadata.get("method_id", "yookassa")
    payment_source = metadata.get("payment_source", "miniapp")

    logger.info(
        "Processing eSIM webhook payment: id=%s user=%s loc=%s pkg=%s",
        payment_id,
        user_id,
        loc,
        pkg,
    )

    if user_id is None or not loc or not pkg:
        logger.warning("eSIM webhook %s missing metadata", payment_id)
        return False
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    if user_id <= 0:
        return False

    method_data = payment_methods.get(method_id, {"title": "Онлайн-оплата", "currency": "RUB"})
    amount_cents = _payment_amount_rub_cents(payment_obj)
    if amount_cents <= 0:
        amount_cents = 1

    pkgs = await esim_service.public_packages(str(loc).upper())
    package_row = next((p for p in pkgs if p.get("packageCode") == pkg), None)
    if not package_row:
        logger.error("eSIM webhook: unknown package %s / %s", loc, pkg)
        return False
    price_k = int(
        package_row.get("salePriceKopecks") or esim_service.package_sale_price_kopecks(package_row)
    )

    async with get_connection() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM payments WHERE yookassa_payment_id = $1",
            payment_id,
        )
        if existing and existing["status"] == "completed":
            logger.warning("eSIM payment %s already done", payment_id)
            return False
        user_exists = await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1", user_id)
        if not user_exists:
            logger.error("eSIM webhook: user %s missing", user_id)
            return False

        await _finalize_esim_delivery(
            conn,
            user_id=user_id,
            location_code=str(loc).upper(),
            package_code=str(pkg),
            price_kopecks=price_k,
            payment_ref=payment_id,
            payment_source=payment_source,
            amount_cents=amount_cents,
            currency="RUB",
            telegram_charge_id=None,
            provider_charge_id=None,
            method_label=method_data.get("title", method_id),
            bot=bot,
            config=config,
        )

    logger.info("eSIM webhook payment %s completed for user %s", payment_id, user_id)
    return True


async def process_esim_telegram_invoice_payment(
    message: Message,
    bot: Bot,
    config: AppConfig,
    method_id: str,
    source: str,
    location_code: str,
    package_code: str,
) -> bool:
    """Успешная оплата инвойса Telegram (Stars / нативная ЮKassa) для eSIM."""
    user_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    provider_charge_id = message.successful_payment.provider_payment_charge_id
    currency = message.successful_payment.currency
    total_amount = message.successful_payment.total_amount

    pkgs = await esim_service.public_packages(location_code.upper())
    package_row = next((p for p in pkgs if p.get("packageCode") == package_code), None)
    if not package_row:
        logger.error("eSIM telegram: bad package %s %s", location_code, package_code)
        await message.answer("❌ Тариф не найден. Обратитесь в поддержку.")
        return False

    price_k = int(
        package_row.get("salePriceKopecks") or esim_service.package_sale_price_kopecks(package_row)
    )
    if currency == "XTR":
        expected = max(1, (price_k + 99) // 100)
        if int(total_amount) != int(expected):
            logger.error(
                "eSIM stars amount mismatch: got %s expected %s", total_amount, expected
            )
            await message.answer("❌ Сумма платежа не совпадает с тарифом.")
            return False
        amount_cents = int(total_amount)
    else:
        expected_k = price_k
        if int(total_amount) != int(expected_k):
            logger.error(
                "eSIM rub invoice mismatch: got %s expected %s", total_amount, expected_k
            )
            await message.answer("❌ Сумма платежа не совпадает с тарифом.")
            return False
        amount_cents = int(total_amount)

    async with get_connection() as conn:
        if provider_charge_id:
            existing_payment = await conn.fetchrow(
                "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1 OR yookassa_payment_id = $2",
                charge_id,
                provider_charge_id,
            )
        else:
            existing_payment = await conn.fetchrow(
                "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1",
                charge_id,
            )
        if existing_payment and existing_payment["status"] == "completed":
            await message.answer("✅ Этот платёж уже был обработан.")
            return False

        await _finalize_esim_delivery(
            conn,
            user_id=user_id,
            location_code=location_code.upper(),
            package_code=package_code,
            price_kopecks=price_k,
            payment_ref=charge_id,
            payment_source=source,
            amount_cents=amount_cents,
            currency=currency,
            telegram_charge_id=charge_id,
            provider_charge_id=provider_charge_id,
            method_label=method_id,
            bot=bot,
            config=config,
        )

    await message.answer("✅ Оплата принята! eSIM отправлен вам в отдельном сообщении.")
    return True


async def process_telegram_stars_payment(
    message: Message,
    bot: Bot,
    plan_id: str,
    plan_data: dict,
    method_data: dict,
    is_new_subscription: bool,
    config: AppConfig,
    source: str = 'bot'
) -> bool:
    """
    Обрабатывает платеж через Telegram Stars
    
    Args:
        message: Сообщение с successful_payment
        bot: Экземпляр бота
        plan_id: ID плана подписки
        plan_data: Данные плана
        method_data: Данные метода оплаты
        is_new_subscription: True если новая подписка, False если продление
        config: Конфигурация приложения
        
    Returns:
        True если платеж успешно обработан, False если уже был обработан
    """
    user_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    provider_charge_id = message.successful_payment.provider_payment_charge_id
    currency = message.successful_payment.currency
    total_amount = message.successful_payment.total_amount # В минимальных единицах (копейки/звезды)
    
    async with get_connection() as conn:
        # ✅ ПРОВЕРКА НА ДУБЛИКАТЫ ПЕРЕД ОБРАБОТКОЙ
        # Проверяем по обоим ID для надежности
        if provider_charge_id:
            existing_payment = await conn.fetchrow(
                "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1 OR yookassa_payment_id = $2",
                charge_id, provider_charge_id
            )
        else:
            existing_payment = await conn.fetchrow(
                "SELECT id, status FROM payments WHERE telegram_payment_charge_id = $1",
                charge_id
            )
        
        if existing_payment and existing_payment['status'] == 'completed':
            logger.warning(f"Payment {charge_id}/{provider_charge_id} already processed, skipping")
            await message.answer(
                "✅ Этот платеж уже был обработан ранее."
            )
            return False
        
        # ✅ ИСПОЛЬЗУЕМ ТРАНЗАКЦИЮ
        async with conn.transaction():
            duration_months = plan_data['duration']
            
            # Обновляем подписку
            if is_new_subscription:
                await set_new_subscription(user_id, duration_months, conn)
            else:
                await extend_subscription(user_id, duration_months, conn)
            
            # Получаем обновленную дату окончания
            subscription_end_row = await conn.fetchrow(
                "SELECT subscription_end FROM users WHERE user_id = $1",
                user_id
            )
            subscription_end = subscription_end_row['subscription_end'] if subscription_end_row else None
            
            # Сохраняем платеж
            if existing_payment:
                await conn.execute('''
                    UPDATE payments 
                    SET status = 'completed', amount = $1, currency = $2, plan_id = $3, yookassa_payment_id = $4
                    WHERE telegram_payment_charge_id = $5 OR (yookassa_payment_id = $4 AND yookassa_payment_id IS NOT NULL)
                ''', total_amount, currency, plan_id, provider_charge_id, charge_id)
            else:
                await conn.execute('''
                    INSERT INTO payments 
                    (user_id, amount, currency, plan_id, plan_type, status, telegram_payment_charge_id, yookassa_payment_id, payment_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ''', user_id, total_amount, currency, plan_id, 'subscription', 'completed', charge_id, provider_charge_id, source)
        
        # Создаём/активируем ключи (после транзакции)
        if is_new_subscription:
            await create_or_activate_keys_for_all_servers(user_id)
        else:
            # Для продления - синхронизируем ключи
            from .subscriptions import sync_user_keys
            await sync_user_keys(user_id)
        
        # Форматируем дату
        if subscription_end:
            try:
                if isinstance(subscription_end, str):
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = subscription_end
                end_date_str = end_date.strftime("%d.%m.%Y")
            except:
                end_date_str = str(subscription_end)
        else:
            end_date_str = "неизвестно"
        
        # Форматируем цену
        if currency == 'XTR':
            formatted_price = f"{total_amount} Stars (≈ {total_amount * 0.01:.2f}₽)"
        else:
            formatted_price = f"{total_amount // 100}₽"
        
        # Отправляем квитанцию пользователю
        receipt = (
            f"💳 <b>VPN подписка</b> успешно активирована!\n\n"
            f"<b>Чек на оплату</b>\n"
            f"Дата активации: <i>{datetime.now().strftime('%d.%m.%Y')}</i>\n"
            f"Дата окончания: <i>{end_date_str}</i>\n"
            f"Способ оплаты: <i>{method_data['title']}</i>\n"
            f"Сумма оплаты: <i>{formatted_price}</i>\n\n"
            f"<b>Детали подписки</b>:\n"
            f"• План: <i>{plan_data['title']}</i>\n"
            f"• Трафик: <i>{plan_data.get('traffic_gb', 'Безлимитный')} ГБ</i>\n"
            f"• Срок: <i>{duration_months} месяцев</i>\n\n"
            f"✅ Теперь вы можете получить VPN ссылку через кнопку <b>🔗 Получить VPN</b>!\n\n"
            f"ID транзакции: <code>{charge_id}</code>"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
        
        await message.answer(receipt, parse_mode='HTML', reply_markup=builder.as_markup())
        
        # Уведомление админам
        username = message.from_user.username or "нет"
        first_name = message.from_user.first_name or "Пользователь"
        
        for admin_id in config.bot.admin_ids:
            if admin_id != user_id:
                try:
                    await bot.send_message(
                        admin_id,
                        f"💳 <b>Покупка подписки</b>\n\n"
                        f"Пользователь: {first_name} (@{username})\n"
                        f"ID: <code>{user_id}</code>\n"
                        f"План: {plan_data['title']}\n"
                        f"Способ оплаты: {method_data['title']}\n"
                        f"Сумма: {formatted_price}\n"
                        f"Срок: {duration_months} месяцев\n"
                        f"Активирована до: {end_date_str}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to send admin notification to {admin_id}: {e}")
        
        return True


async def process_webhook_payment(
    payment_id: str,
    payment_obj: dict,
    metadata: dict,
    bot: Optional[Bot],
    config: AppConfig,
    subscription_plans: dict,
    renewal_plans: dict,
    payment_methods: dict
) -> bool:
    """
    Обрабатывает платеж через вебхуки (YooKassa, Crypto Pay)
    
    Args:
        payment_id: ID платежа (в YooKassa или Crypto Pay)
        payment_obj: Объект платежа
        metadata: Метаданные платежа
        bot: Экземпляр бота (опционально)
        config: Конфигурация
        subscription_plans: Словарь планов подписки
        renewal_plans: Словарь планов продления
        payment_methods: Словарь методов оплаты
        
    Returns:
        True если платеж успешно обработан, False если уже был обработан
    """
    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan_id")
    method_id = metadata.get("method_id", "yookassa")
    payment_source = metadata.get("payment_source", "bot")
    
    logger.info(f"Processing webhook payment: id={payment_id}, method={method_id}, user={user_id}, plan={plan_id}, source={payment_source}")

    if str(metadata.get("product_type") or "").lower() == "esim":
        return await process_esim_webhook_payment(
            payment_id=payment_id,
            payment_obj=payment_obj,
            metadata=metadata,
            bot=bot,
            config=config,
            payment_methods=payment_methods,
        )

    if user_id is None or plan_id is None:
        logger.warning(f"Webhook payment {payment_id} missing required metadata")
        return False
    
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        logger.error(f"Invalid user_id in webhook metadata: {user_id}")
        return False
    
    if user_id <= 0:
        logger.error(f"Invalid user_id value: {user_id}")
        return False
    
    try:
        # Определяем тип подписки
        if plan_id in subscription_plans:
            plan_data = subscription_plans[plan_id]
            is_new_subscription = True
        elif plan_id in renewal_plans:
            plan_data = renewal_plans[plan_id]
            is_new_subscription = False
        else:
            logger.error(f"Unknown plan_id in webhook payment: {plan_id}")
            return False
        
        method_data = payment_methods.get(method_id, {
            "title": "Онлайн-оплата",
            "currency": "RUB",
        })
        
        duration_months = plan_data["duration"]
        
        # Получаем сумму из объекта платежа
        amount_val = payment_obj.get("amount")
        amount_value = None
        if isinstance(amount_val, dict):
            amount_value = amount_val.get("value")
        elif isinstance(amount_val, (str, int, float)):
            amount_value = amount_val
            
        try:
            amount_rub = float(amount_value) if amount_value is not None else plan_data.get("price_rub", 0) / 100.0
        except (TypeError, ValueError):
            amount_rub = plan_data.get("price_rub", 0) / 100.0
        amount_cents = int(round(amount_rub * 100))
        
        async with get_connection() as conn:
            # ✅ ПРОВЕРКА НА ДУБЛИКАТЫ ПЕРЕД ОБРАБОТКОЙ
            existing = await conn.fetchrow(
                "SELECT id, status FROM payments WHERE yookassa_payment_id = $1",
                payment_id,
            )
            
            if existing and existing['status'] == 'completed':
                logger.warning(f"Webhook payment {payment_id} already processed, skipping")
                return False
            
            # Проверяем пользователя
            user_exists = await conn.fetchval(
                "SELECT user_id FROM users WHERE user_id = $1",
                user_id
            )
            if not user_exists:
                logger.error(f"User {user_id} not found for webhook payment {payment_id}")
                return False
            
            # ✅ ИСПОЛЬЗУЕМ ТРАНЗАКЦИЮ
            async with conn.transaction():
                # Обновляем подписку
                if is_new_subscription:
                    await set_new_subscription(user_id, duration_months, conn)
                else:
                    await extend_subscription(user_id, duration_months, conn)
                
                # Получаем обновлённую дату окончания
                sub_row = await conn.fetchrow(
                    "SELECT subscription_end FROM users WHERE user_id = $1",
                    user_id,
                )
                subscription_end = sub_row["subscription_end"] if sub_row else None
                
                # Обновляем или создаём запись в payments
                if existing:
                    await conn.execute('''
                        UPDATE payments 
                        SET status = 'completed', amount = $1, payment_source = $2
                        WHERE yookassa_payment_id = $3
                    ''', amount_cents, payment_source, payment_id)
                else:
                    await conn.execute('''
                        INSERT INTO payments 
                        (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id, payment_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ''', user_id, amount_cents, "RUB", plan_id, "subscription", "completed", payment_id, payment_source)
            
            # Создаём/активируем ключи (после транзакции)
            if is_new_subscription:
                await create_or_activate_keys_for_all_servers(user_id)
            else:
                from .subscriptions import sync_user_keys
                await sync_user_keys(user_id)
            
            # Форматируем дату
            if subscription_end:
                try:
                    if isinstance(subscription_end, str):
                        if " " in subscription_end:
                            d = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            d = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        d = subscription_end
                    end_str = d.strftime("%d.%m.%Y")
                except:
                    end_str = str(subscription_end)
            else:
                end_str = "неизвестно"
            
            # Уведомление пользователю
            if bot:
                try:
                    formatted_price = f"{amount_rub:.2f} ₽"
                    text = (
                        f"✅ <b>Оплата успешно получена!</b>\n\n"
                        f"💳 <b>Детали платежа:</b>\n"
                        f"• Способ: <i>{method_data['title']}</i>\n"
                        f"• Сумма: <i>{formatted_price}</i>\n"
                        f"• ID транзакции: <code>{payment_id}</code>\n\n"
                        f"💎 <b>Подписка:</b>\n"
                        f"• План: <i>{plan_data['title']}</i>\n"
                        f"• Активна до: <b>{end_str}</b>\n\n"
                        f"Нажмите кнопку ниже, чтобы получить настройки VPN."
                    )
                    
                    builder = InlineKeyboardBuilder()
                    builder.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
                    
                    await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=builder.as_markup())
                except Exception as e:
                    logger.error(f"Error sending webhook confirmation to user {user_id}: {e}")
            
            # Уведомление админам
            if bot:
                try:
                    user_info = await bot.get_chat(user_id)
                    username = user_info.username if hasattr(user_info, 'username') else "нет"
                    first_name = user_info.first_name if hasattr(user_info, 'first_name') else "Пользователь"
                except:
                    username = "нет"
                    first_name = "Пользователь"
                
                formatted_price = f"{amount_rub:.2f} ₽"
                
                for admin_id in config.bot.admin_ids:
                    if admin_id != user_id:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"💳 <b>Покупка подписки ({method_data['title']})</b>\n\n"
                                f"Пользователь: {first_name} (@{username})\n"
                                f"ID: <code>{user_id}</code>\n"
                                f"План: {plan_data['title']}\n"
                                f"Способ оплаты: {method_data['title']}\n"
                                f"Сумма: {formatted_price}\n"
                                f"Срок: {duration_months} месяцев\n"
                                f"Активирована до: {end_str}",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Failed to send admin notification to {admin_id}: {e}")
            
            logger.info(f"Successfully processed webhook payment {payment_id} for user {user_id}")
            return True
        
    except Exception as e:
        logger.error(f"Error processing webhook payment {payment_id}: {e}", exc_info=True)
        return False
