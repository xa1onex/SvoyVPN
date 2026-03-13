"""
Модуль для обработки платежей (Telegram Stars и YooKassa)
С исправлениями: проверка дубликатов, идемпотентность, транзакции
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Bot
from aiogram.types import Message

from .database import get_connection
from .subscriptions import set_new_subscription, extend_subscription, create_or_activate_keys_for_all_servers
from .config import AppConfig

logger = logging.getLogger(__name__)


async def process_telegram_stars_payment(
    message: Message,
    bot: Bot,
    plan_id: str,
    plan_data: dict,
    method_data: dict,
    is_new_subscription: bool,
    config: AppConfig
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
                    (user_id, amount, currency, plan_id, plan_type, status, telegram_payment_charge_id, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ''', user_id, total_amount, currency, plan_id, 'subscription', 'completed', charge_id, provider_charge_id)
        
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
        
        await message.answer(receipt, parse_mode='HTML')
        
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
    
    logger.info(f"Processing webhook payment: id={payment_id}, method={method_id}, user={user_id}, plan={plan_id}")
    
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
                        SET status = 'completed', amount = $1
                        WHERE yookassa_payment_id = $2
                    ''', amount_cents, payment_id)
                else:
                    await conn.execute('''
                        INSERT INTO payments 
                        (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ''', user_id, amount_cents, "RUB", plan_id, "subscription", "completed", payment_id)
            
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
                    text = (
                        f"✅ <b>Оплата через {method_data['title']} успешно получена!</b>\n\n"
                        f"План: <i>{plan_data['title']}</i>\n"
                        f"Подписка активна до: <b>{end_str}</b>\n\n"
                        "Нажмите <b>🔗 Получить VPN</b> в главном меню, чтобы получить ссылку подписки."
                    )
                    await bot.send_message(user_id, text, parse_mode="HTML")
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
