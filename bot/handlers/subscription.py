"""
Обработчики подписки и получения VPN ссылки
"""
import logging
from datetime import datetime
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from ..database import get_connection, ensure_subscription_token
from ..subscriptions import get_subscription_status, get_user_subscription_url
from ..plans import get_subscription_plans, get_renewal_plans, format_price_rub, format_price_stars, format_price_both, PAYMENT_METHODS
from ..config import AppConfig
from ..yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


async def should_show_discount(days_remaining: int) -> bool:
    """Проверяет, должна ли показываться скидка
    
    Скидка показывается когда:
    - Режим 2 (enable_for_all = True): скидка показывается ВСЕМ пользователям
    - Режим 1 (enable_for_all = False): скидка показывается только если days_remaining <= days_threshold
    """
    # Защита от отрицательных значений
    if days_remaining < 0:
        days_remaining = 0
    
    async with get_connection() as conn:
        discount_settings = await conn.fetchrow('SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1')
        if not discount_settings:
            # По умолчанию скидка показывается за 3 дня
            result = days_remaining <= 3
            logger.info(f"should_show_discount (no settings): days_remaining={days_remaining}, default_threshold=3, result={result}")
            return result
        
        days_threshold = discount_settings['days_threshold']
        enable_for_all = discount_settings['enable_for_all']
        
        # Логируем настройки для отладки
        logger.info(f"should_show_discount settings: days_remaining={days_remaining}, days_threshold={days_threshold}, enable_for_all={enable_for_all}")
        
        # Режим 2: скидка для всех
        if enable_for_all:
            # Если days_threshold = 0, скидка все равно показывается (глобальная скидка)
            logger.info(f"should_show_discount: enable_for_all=True, showing discount for all")
            return True
        
        # Режим 1: скидка только по условию дней
        # Если days_threshold = 0, None или отрицательный, скидка отключена
        if not days_threshold or days_threshold <= 0:
            logger.info(f"should_show_discount: discount disabled (days_threshold={days_threshold})")
            return False
        
        # Защита от слишком больших значений (если больше 365 дней - это явно ошибка)
        if days_threshold > 365:
            logger.warning(f"should_show_discount: days_threshold слишком большой ({days_threshold}), отключаем скидку")
            return False
        
        # Показываем скидку если осталось дней <= порога
        result = days_remaining <= days_threshold
        logger.info(f"should_show_discount result: days_remaining={days_remaining} <= days_threshold={days_threshold} -> {result}")
        return result


async def setup_subscription_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики подписки"""
    
    @dp.callback_query(F.data == "get_vpn_link")
    async def handle_get_vpn_link(callback: CallbackQuery):
        """Отправляет пользователю ссылку подписки"""
        user_id = callback.from_user.id
        link = await get_user_subscription_url(user_id, config)
        await callback.message.answer(
            "🔗 <b>Получить VPN</b>\n\n"
            "Добавьте эту ссылку в приложение как <b>подписку</b>:\n"
            f"<code>{link}</code>\n\n"
            "📱 <b>Как использовать:</b>\n"
            "1. Скопируйте ссылку выше\n"
            "2. Откройте приложение (v2rayNG, v2rayN, sing-box и т.п.)\n"
            "3. Добавьте ссылку как <b>подписку</b>\n"
            "4. Обновите/синхронизируйте подписку в приложении",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await callback.answer()
    
    @dp.callback_query(F.data == "open_premium")
    async def handle_open_premium(callback: CallbackQuery, state: FSMContext):
        """Обработчик кнопки Premium"""
        user_id = callback.from_user.id
        info = await get_subscription_info(user_id)
        text, builder = await build_subscription_message(info, state, config)
        
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
    
    @dp.message(Command("prem"))
    async def handle_prem_command(message: Message, state: FSMContext):
        """Обработчик команды /prem"""
        user_id = message.from_user.id
        info = await get_subscription_info(user_id)
        text, builder = await build_subscription_message(info, state, config)
        
        await message.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    
    # Подключаем обработчики планов
    await setup_subscription_plan_handlers(dp, bot, config)


async def get_subscription_info(user_id: int) -> dict:
    """Получает информацию о подписке пользователя"""
    async with get_connection() as conn:
        user_data = await conn.fetchrow('''
            SELECT subscription_end, pay_subscribed, subscription_token
            FROM users WHERE user_id = $1
        ''', user_id)
        
        if not user_data:
            return {
                'is_active': False,
                'days_remaining': 0,
                'end_date_str': None,
                'user_id': user_id
            }
        
        subscription_end = user_data['subscription_end']
        pay_subscribed = user_data['pay_subscribed']
        is_active = False
        days_remaining = 0
        end_date_str = None
        
        if pay_subscribed and subscription_end:
            try:
                if isinstance(subscription_end, str):
                    if ' ' in subscription_end:
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                
                if end_date_only >= today:
                    is_active = True
                    days_remaining = (end_date_only - today).days
                    end_date_str = end_date.strftime("%d.%m.%Y")
            except Exception as e:
                logger.error(f"Error parsing subscription date: {e}")
        
        return {
            'is_active': is_active,
            'days_remaining': days_remaining,
            'end_date_str': end_date_str,
            'user_id': user_id
        }


async def build_subscription_message(info: dict, state: FSMContext, config=None) -> tuple[str, InlineKeyboardBuilder]:
    """Строит сообщение и клавиатуру для подписки"""
    builder = InlineKeyboardBuilder()
    is_active = info['is_active']
    days_remaining = info['days_remaining']
    end_date_str = info['end_date_str']
    user_id = info['user_id']
    
    subscription_url = await get_user_subscription_url(user_id, config)
    
    if is_active:
        if days_remaining < 1:
            days_display = "СЕГОДНЯ"
        else:
            days_display = f"{days_remaining} {'день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней'}"
        
        text = (
            "✅ Ваш <b>VPN</b> <b>активен</b>!\n\n"
            f"📅 Дата окончания: <i>{end_date_str}</i>\n"
            f"⏰ Осталось: <i>{days_display}</i>\n\n"
            "🔗 <b>Ваша ссылка VPN (подписка):</b>\n"
            f"<code>{subscription_url}</code>\n\n"
            "📱 <b>Как использовать:</b>\n"
            "1. Скопируйте ссылку выше\n"
            "2. Откройте приложение (v2rayNG, v2rayN, sing-box и т.п.)\n"
            "3. Добавьте ссылку как <b>подписку</b>\n"
            "4. Обновите/синхронизируйте подписку в приложении\n\n"
        )
        
        text += (
            "<b>Детали VPN</b>:\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость\n\n"
        )
        
        # Всегда показываем возможность продления, если подписка активна
        renewal_plans = await get_renewal_plans()
        subscription_plans = await get_subscription_plans()
        
        # Проверяем, должна ли показываться скидка
        show_discount = await should_show_discount(days_remaining)
        
        if show_discount:
            # Если скидка активна - показываем текст про скидку
            text += "🎁 <b>Специальное предложение!</b>\n\n"
            text += "🔥 Успей продлить <b>VPN</b> по специальной цене:\n\n"
            
            # Показываем цены продления с зачеркнутыми обычными ценами
            for plan_id in renewal_plans:
                renew_plan = renewal_plans[plan_id]
                # Находим обычную цену (убираем _renew из plan_id)
                base_plan_id = plan_id.replace('_renew', '')
                base_plan = subscription_plans.get(base_plan_id, {})
                old_price = format_price_rub(base_plan.get('price_rub', 0))
                new_price = format_price_rub(renew_plan['price_rub'])
                
                text += f"{renew_plan['title'].replace(' 🔥', '')} <s>{old_price}</s> - {new_price}\n"
            text += "\n"
        else:
            # Если скидки нет - просто показываем обычный текст
            text += "💡 Вы можете продлить подписку в любое время:\n\n"
        
        # Всегда показываем кнопки продления
        # Но если скидка не активна, убираем "🔥" из названий и показываем обычные цены
        for plan_id, plan_data in renewal_plans.items():
            if show_discount:
                # Если скидка активна - показываем скидочные цены с "🔥"
                button_title = plan_data['title']
                button_price_rub = plan_data['price_rub']
                button_price_stars = plan_data['price_stars']
            else:
                # Если скидка не активна - убираем "🔥" и показываем обычные цены
                # Находим обычный план (убираем _renew из plan_id)
                base_plan_id = plan_id.replace('_renew', '')
                base_plan = subscription_plans.get(base_plan_id, plan_data)
                button_title = plan_data['title'].replace(' 🔥', '')
                button_price_rub = base_plan.get('price_rub', plan_data['price_rub'])
                button_price_stars = base_plan.get('price_stars', plan_data['price_stars'])
            
            builder.button(
                text=f"{button_title} - {format_price_both(button_price_rub, button_price_stars)}",
                callback_data=f"plan:{plan_id}"
            )
        builder.adjust(1)
        
        # Кнопка "Назад" всегда
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    else:
        text = (
            "❌ Ваш <b>VPN</b> <b>неактивен</b>\n\n"
            "💳 Для активации VPN необходимо купить подписку.\n\n"
            "📋 <b>Доступные планы:</b>\n"
        )
        
        subscription_plans = await get_subscription_plans()
        for plan_id, plan_data in list(subscription_plans.items())[:4]:  # Показываем первые 4
            price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
            text += f"• {plan_data['title']} - {price_text}\n"
        
        builder.row(InlineKeyboardButton(text="💳 Купить подписку", callback_data="show_subscription_plans"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    
    return text, builder


async def setup_subscription_plan_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики для показа планов подписки"""
    
    @dp.callback_query(F.data == "show_subscription_plans")
    async def handle_show_subscription_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы подписки для новых пользователей"""
        user_id = callback.from_user.id
        subscription_plans = await get_subscription_plans()
        
        text = "💳 <b>Выберите план подписки:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in subscription_plans.items():
            price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
            text += f"• <b>{plan_data['title']}</b> - {price_text}\n"
            text += f"  Срок: {plan_data['duration']} месяцев\n"
            text += f"  Трафик: {plan_data.get('traffic_gb', 'Безлимитный')} ГБ\n\n"
        
        # Кнопки для оплаты (Stars и YooKassa)
        for plan_id, plan_data in list(subscription_plans.items())[:2]:  # Показываем первые 2 плана
            builder.row(
                InlineKeyboardButton(
                    text=f"⭐ {plan_data['title']} ({format_price_stars(plan_data['price_stars'])})",
                    callback_data=f"buy_subscription:{plan_id}:stars"
                )
            )
            if config.yookassa.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💳 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"buy_subscription:{plan_id}:yookassa"
                    )
                )
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data == "show_renewal_plans")
    async def handle_show_renewal_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы продления"""
        user_id = callback.from_user.id
        renewal_plans = await get_renewal_plans()
        
        text = "💳 <b>Продлить подписку:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in renewal_plans.items():
            price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
            text += f"• <b>{plan_data['title']}</b> - {price_text}\n"
        
        # Кнопки для оплаты
        for plan_id, plan_data in list(renewal_plans.items())[:2]:
            builder.row(
                InlineKeyboardButton(
                    text=f"⭐ {plan_data['title']} ({format_price_stars(plan_data['price_stars'])})",
                    callback_data=f"buy_renewal:{plan_id}:stars"
                )
            )
            if config.yookassa.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💳 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"buy_renewal:{plan_id}:yookassa"
                    )
                )
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data.startswith("plan:"))
    async def handle_select_plan(callback: CallbackQuery, state: FSMContext):
        """Обработчик выбора плана (показывает методы оплаты)"""
        plan_id = callback.data.split(":")[1]
        user_id = callback.from_user.id
        
        # Получаем планы
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        ALL_PLANS = {**subscription_plans, **renewal_plans}
        
        if plan_id not in ALL_PLANS:
            await callback.answer("❌ Неверный план", show_alert=True)
            return
        
        is_renewal = plan_id in renewal_plans or '_renew' in plan_id
        plan_data = renewal_plans.get(plan_id) if is_renewal else subscription_plans.get(plan_id)
        
        if not plan_data:
            await callback.answer("❌ План не найден", show_alert=True)
            return
        
        # Проверяем, есть ли у пользователя активная подписка
        async with get_connection() as conn:
            active_sub = await conn.fetchrow('''
                SELECT subscription_end 
                FROM users 
                WHERE user_id = $1 
                    AND pay_subscribed = TRUE 
                    AND subscription_end >= CURRENT_DATE
            ''', user_id)
        
        # Если пользователь пытается купить новую подписку, но у него уже есть активная
        if not is_renewal and active_sub:
            await callback.answer("❌ У вас уже есть активная подписка! Используйте продление.", show_alert=True)
            return
        
        # Проверяем наличие активной подписки для продления
        if is_renewal:
            if not active_sub:
                await callback.answer("❌ У вас нет активной подписки для продления!", show_alert=True)
                return
        
        # Показываем методы оплаты
        text = f"💳 <b>{plan_data['title']}</b>\n\n"
        text += f"Срок: {plan_data['duration']} месяцев\n"
        text += f"Трафик: {plan_data.get('traffic_gb', 'Безлимитный')} ГБ\n\n"
        text += "Выберите способ оплаты:"
        
        builder = InlineKeyboardBuilder()
        
        # Кнопка для оплаты Stars
        builder.row(
            InlineKeyboardButton(
                text=f"⭐ Telegram Stars ({format_price_stars(plan_data['price_stars'])})",
                callback_data=f"{'buy_renewal' if is_renewal else 'buy_subscription'}:{plan_id}:stars"
            )
        )
        
        # Кнопка для оплаты YooKassa (если включена)
        if config.yookassa.enabled:
            builder.row(
                InlineKeyboardButton(
                    text=f"💳 Банковская карта ({format_price_rub(plan_data['price_rub'])})",
                    callback_data=f"{'buy_renewal' if is_renewal else 'buy_subscription'}:{plan_id}:yookassa"
                )
            )
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data.startswith("buy_subscription:"))
    async def handle_buy_subscription(callback: CallbackQuery):
        """Обработка покупки подписки"""
        await process_payment(callback, is_renewal=False)
    
    @dp.callback_query(F.data.startswith("buy_renewal:"))
    async def handle_buy_renewal(callback: CallbackQuery):
        """Обработка продления подписки"""
        await process_payment(callback, is_renewal=True)
    
    async def process_payment(callback: CallbackQuery, is_renewal: bool):
        """Обработка платежа (общая функция для покупки и продления)"""
        # Парсим callback_data: buy_subscription:plan_id:method_id или buy_renewal:plan_id:method_id
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Неверный формат данных", show_alert=True)
            return
        
        plan_id = parts[1]
        method_id = parts[2]
        
        # Получаем планы
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        
        # Определяем план
        if is_renewal:
            plan_data = renewal_plans.get(plan_id)
        else:
            plan_data = subscription_plans.get(plan_id)
        
        if not plan_data:
            await callback.answer("❌ План не найден", show_alert=True)
            return
        
        # Валидация метода оплаты
        if method_id not in PAYMENT_METHODS:
            await callback.answer("❌ Неизвестный метод оплаты", show_alert=True)
            return
        
        method_data = PAYMENT_METHODS[method_id]
        
        # Определяем цену
        currency_type = 'stars' if method_data['currency'] == 'XTR' else 'rub'
        price_key = f"price_{currency_type}"
        price = plan_data.get(price_key)
        
        if price is None:
            await callback.answer(f"❌ Цена не найдена для плана. Ключ: {price_key}", show_alert=True)
            logger.error(f"Price key '{price_key}' not found in plan_data for plan {plan_id}")
            return
        
        # Преобразуем цену в int
        try:
            price = int(float(price))
        except (ValueError, TypeError) as e:
            await callback.answer("❌ Ошибка: неверный формат цены", show_alert=True)
            logger.error(f"Invalid price format for plan {plan_id}: {price}, error: {e}")
            return
        
        if price <= 0:
            await callback.answer("❌ Ошибка: цена должна быть больше нуля", show_alert=True)
            logger.error(f"Invalid price value for plan {plan_id}: {price}")
            return
        
        # Обработка разных методов оплаты
        if method_id == "yookassa":
            # Оплата через ЮKassa
            if not config.yookassa.enabled:
                await callback.answer("❌ ЮKassa не настроена", show_alert=True)
                return
            
            # Проверяем минимальную сумму для ЮKassa (минимум 1 рубль = 100 копеек)
            if price < 100:
                await callback.answer("❌ Минимальная сумма оплаты - 1 рубль", show_alert=True)
                return
            
            try:
                # Создаем YooKassa клиент
                yookassa_client = YooKassaClient(config.yookassa)
                
                # Получаем username бота для return_url
                bot_info = await bot.get_me()
                bot_username = bot_info.username
                
                # Конвертируем цену из копеек в рубли
                amount_rub = price / 100.0
                
                # Создаем платеж через ЮKassa
                payment_data = yookassa_client.create_payment(
                    amount=amount_rub,
                    description=f"VPN подписка - {plan_data['title']}",
                    return_url=f"https://t.me/{bot_username}?start=payment_success",
                    metadata={
                        "user_id": callback.from_user.id,
                        "plan_id": plan_id,
                        "method_id": method_id
                    }
                )
                
                payment_id = payment_data["id"]
                confirmation_url = payment_data["confirmation_url"]
                
                # Сохраняем платеж в БД со статусом pending
                async with get_connection() as conn:
                    await conn.execute('''
                        INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ''',
                        callback.from_user.id,
                        price,
                        "RUB",
                        plan_id,
                        "subscription",
                        "pending",
                        payment_id
                    )
                
                # Отправляем пользователю ссылку на оплату
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="💳 Перейти к оплате", url=confirmation_url))
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
                
                await callback.message.edit_text(
                    f"💳 <b>Оплата через ЮKassa</b>\n\n"
                    f"План: <i>{plan_data['title']}</i>\n"
                    f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                    f"Нажмите кнопку ниже, чтобы перейти к оплате.\n"
                    f"После успешной оплаты подписка будет активирована автоматически.\n\n"
                    f"⏰ <i>Платеж не должен задерживаться больше 1 часа.</i>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
                
                await callback.answer()
                
            except Exception as e:
                logger.error(f"Error creating YooKassa payment: {e}", exc_info=True)
                await callback.answer("❌ Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
                return
        
        else:
            # Оплата через Telegram (Stars)
            # Проверяем минимальную сумму
            if price < 1:
                await callback.answer("❌ Ошибка: сумма слишком мала", show_alert=True)
                return
            
            # Создаем payload
            payload = f"{plan_id}|{method_id}"
            
            # Отправляем инвойс
            try:
                await bot.send_invoice(
                    chat_id=callback.message.chat.id,
                    title=f"VPN подписка - {plan_data['title']}",
                    description=f"Нажимая кнопку «Заплатить» Вы соглашаетесь с правилами VPN бота (/help)",
                    provider_token=method_data.get('provider_token', ''),
                    currency=method_data['currency'],
                    prices=[LabeledPrice(label="VPN подписка", amount=price)],
                    payload=payload,
                    start_parameter='subscription'
                )
                await callback.answer()
            except Exception as e:
                logger.error(f"Error sending invoice: {e}", exc_info=True)
                await callback.answer("❌ Ошибка при создании инвойса. Попробуйте позже.", show_alert=True)
    
    @dp.callback_query(F.data == "go_back")
    async def handle_go_back_subscription(callback: CallbackQuery, state: FSMContext):
        """Возврат в меню подписки"""
        user_id = callback.from_user.id
        info = await get_subscription_info(user_id)
        text, builder = await build_subscription_message(info, state, config)
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest as e:
            # Игнорируем ошибку, если сообщение не изменилось
            if "message is not modified" in str(e):
                logger.debug(f"Message not modified for user {user_id}, ignoring")
            else:
                raise
        await callback.answer()
