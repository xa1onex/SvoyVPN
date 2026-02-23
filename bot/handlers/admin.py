"""
Админ-панель - все обработчики для администраторов
"""
import logging
from datetime import datetime
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..database import get_connection
from ..config import AppConfig
from ..plans import SUBSCRIPTION_PLANS_BASE, RENEWAL_PLANS_BASE, format_price_rub, format_price_stars

logger = logging.getLogger(__name__)


class AdminStates(StatesGroup):
    BROADCAST_MESSAGE = State()
    BROADCAST_MEDIA = State()
    BROADCAST_BUTTONS = State()
    SETTING_PRICE = State()
    BALANCE_USER_SELECT = State()
    BALANCE_AMOUNT = State()
    ADD_ADMIN = State()
    REMOVE_ADMIN = State()
    ADD_MANAGER = State()
    REMOVE_MANAGER = State()
    REFERRAL_INVITER_DAYS = State()
    REFERRAL_INVITED_DAYS = State()
    DISCOUNT_DAYS_THRESHOLD = State()
    TRIAL_DAYS = State()
    SERVER_NAME = State()
    SERVER_IP = State()
    SERVER_PORT = State()
    SERVER_PROTOCOL = State()
    SERVER_USERNAME = State()
    SERVER_PASSWORD = State()
    SERVER_INBOUND_ID = State()
    SERVER_EDIT = State()
    DEVICE_APP_NAME = State()
    DEVICE_APP_URL = State()
    DEVICE_APP_ORDER = State()
    DEVICE_INSTRUCTION_PHOTO_MULTIPLE = State()
    MANAGER_SUPPORT_LINK = State()


class AdminEditStates(StatesGroup):
    EDIT_ANNOUNCEMENT = State()


def is_admin(user_id: int, config: AppConfig) -> bool:
    """Проверка, является ли пользователь админом"""
    return user_id in config.bot.admin_ids


async def notify_admins(message_text: str, bot: Bot, config: AppConfig):
    """Отправить уведомление всем админам о действиях пользователей"""
    try:
        for admin_id in config.bot.admin_ids:
            try:
                await bot.send_message(admin_id, message_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send admin notification to {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Error in notify_admins: {e}")


async def safe_callback_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасный ответ на callback query с обработкой устаревших запросов"""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception as e:
        # Игнорируем ошибки устаревших callback queries
        error_msg = str(e).lower()
        if any(phrase in error_msg for phrase in [
            "query is too old",
            "query id is invalid",
            "response timeout expired"
        ]):
            logger.debug(f"Ignoring expired callback query: {e}")
        else:
            logger.warning(f"Error answering callback: {e}")


def get_admin_panel_keyboard():
    """Получить клавиатуру админ панели"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="💰 Управление ценами", callback_data="admin_prices"))
    builder.row(InlineKeyboardButton(text="🎁 Управление скидками", callback_data="admin_discounts"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="✏️ Редактировать объявление", callback_data="edit_announcement"))
    builder.row(InlineKeyboardButton(text="🎁 Управление реферальной системой", callback_data="admin_referral"))
    builder.row(InlineKeyboardButton(text="🆓 Управление пробным периодом", callback_data="admin_trial"))
    builder.row(InlineKeyboardButton(text="🖥️ Управление серверами", callback_data="admin_servers"))
    builder.row(InlineKeyboardButton(text="💳 Управление балансом", callback_data="admin_balance"))
    builder.row(InlineKeyboardButton(text="👥 Управление админами", callback_data="admin_manage_admins"))
    builder.row(InlineKeyboardButton(text="🛟 Управление менеджерами", callback_data="admin_manage_managers"))
    builder.row(InlineKeyboardButton(text="📱 Управление приложениями", callback_data="admin_device_apps"))
    builder.row(InlineKeyboardButton(text="⏰ Ручная отправка напоминаний", callback_data="admin_manual_reminder"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_to_main"))
    return builder.as_markup()


async def setup_admin_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает все админ-обработчики"""
    
    @dp.message(Command("admin"))
    async def handle_admin_panel(message: Message):
        """Главная админ панель"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ У вас нет доступа к админ панели.")
            return
        
        await message.answer(
            "🔐 <b>Админ панель</b>\n\n"
            "Выберите действие:",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="HTML"
        )
    
    @dp.callback_query(F.data == "admin_panel")
    async def handle_admin_panel_callback(callback: CallbackQuery):
        """Callback для админ панели"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "🔐 <b>Админ панель</b>\n\n"
            "Выберите действие:",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_back")
    async def handle_admin_back(callback: CallbackQuery):
        """Вернуться в админ панель"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "🔐 <b>Админ панель</b>\n\n"
            "Выберите действие:",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_back_to_main")
    async def handle_admin_back_to_main(callback: CallbackQuery):
        """Вернуться из админ панели в главное меню"""
        from .start import get_main_text, get_main_keyboard
        
        user_id = callback.from_user.id
        first_name = callback.from_user.first_name or "Пользователь"
        
        # Обновляем активность
        async with get_connection() as conn:
            await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)
        
        from ..subscriptions import get_subscription_status
        subscription_status = await get_subscription_status(user_id)
        
        await callback.message.edit_text(
            await get_main_text(first_name, subscription_status, user_id),
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(user_id, config)
        )
        await safe_callback_answer(callback)
    
    # Редактирование объявления
    @dp.callback_query(F.data == "edit_announcement")
    async def start_edit_announcement(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            "✏️ Введите новый текст объявления.\n\n<code>Он будет показан в главном меню всем пользователям.</code>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminEditStates.EDIT_ANNOUNCEMENT)
        await safe_callback_answer(callback)
    
    @dp.message(AdminEditStates.EDIT_ANNOUNCEMENT)
    async def save_announcement_text(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа", parse_mode="HTML")
            await state.clear()
            return
        
        new_ann = message.text[:2048] if message.text else ''
        if not new_ann.strip():
            await message.answer("Сообщение не может быть пустым. Попробуйте снова (или отмените командой /start)")
            return
        
        async with get_connection() as conn:
            await conn.execute('''
                UPDATE announcements 
                SET text = $1, updated_at = CURRENT_TIMESTAMP 
                WHERE id = (SELECT id FROM announcements ORDER BY id DESC LIMIT 1)
            ''', new_ann)
        
        await message.answer("✅ Объявление обновлено! Теперь оно показывается всем пользователям.", parse_mode="HTML")
        await state.clear()
    
    # Статистика
    @dp.callback_query(F.data == "admin_stats")
    async def handle_admin_stats(callback: CallbackQuery):
        """Подробная статистика"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            # Общая статистика пользователей
            total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
            active_subscriptions = await conn.fetchval('SELECT COUNT(*) FROM users WHERE pay_subscribed = TRUE AND subscription_end >= CURRENT_DATE')
            new_today = await conn.fetchval('SELECT COUNT(*) FROM users WHERE DATE(registration_date) = CURRENT_DATE')
            new_week = await conn.fetchval('SELECT COUNT(*) FROM users WHERE registration_date >= CURRENT_DATE - INTERVAL \'7 days\'')
            
            # Статистика по платежам
            total_revenue_rub = await conn.fetchval('SELECT COALESCE(SUM(amount), 0) FROM payments WHERE currency = \'RUB\' AND status = \'completed\'')
            total_revenue_stars = await conn.fetchval('SELECT COALESCE(SUM(amount), 0) FROM payments WHERE currency = \'XTR\' AND status = \'completed\'')
            payments_today = await conn.fetchval('SELECT COUNT(*) FROM payments WHERE DATE(timestamp) = CURRENT_DATE AND status = \'completed\'')
            revenue_today_rub = await conn.fetchval('SELECT COALESCE(SUM(amount), 0) FROM payments WHERE currency = \'RUB\' AND DATE(timestamp) = CURRENT_DATE AND status = \'completed\'')
            
            # Доход за 30 дней
            revenue_30d_rub = await conn.fetchval('''
                SELECT COALESCE(SUM(amount), 0) 
                FROM payments 
                WHERE currency = 'RUB' 
                    AND status = 'completed' 
                    AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
            ''')
            paying_users_30d = await conn.fetchval('''
                SELECT COUNT(DISTINCT user_id) 
                FROM payments 
                WHERE status = 'completed'
                    AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
            ''')
            active_users_30d = await conn.fetchval(
                'SELECT COUNT(DISTINCT user_id) FROM users WHERE last_activity >= CURRENT_DATE - INTERVAL \'30 days\''
            )
            
            arpu_30d = 0.0
            arppu_30d = 0.0
            if active_users_30d and active_users_30d > 0:
                arpu_30d = (revenue_30d_rub or 0) / 100.0 / active_users_30d
            if paying_users_30d and paying_users_30d > 0:
                arppu_30d = (revenue_30d_rub or 0) / 100.0 / paying_users_30d
            
            # Статистика по ключам
            total_keys = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys')
            active_keys = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE is_active = TRUE')
            
            # Статистика по рефералам
            total_referrals = await conn.fetchval('SELECT COALESCE(SUM(referral_count), 0) FROM users')
            
            # Статистика активности
            active_7days = await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM users WHERE last_activity >= CURRENT_DATE - INTERVAL \'7 days\'')
            inactive_30days = await conn.fetchval('SELECT COUNT(*) FROM users WHERE last_activity < CURRENT_DATE - INTERVAL \'30 days\' OR last_activity IS NULL')
            
            # Отток и продления
            churn_30d = await conn.fetchval('''
                SELECT COUNT(*) 
                FROM users
                WHERE subscription_end BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE - INTERVAL '1 day'
                  AND (pay_subscribed = FALSE OR subscription_end < CURRENT_DATE)
            ''')
            expiring_7d = await conn.fetchval('''
                SELECT COUNT(*) 
                FROM users
                WHERE pay_subscribed = TRUE
                  AND subscription_end BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
            ''')
            
            # Статистика по серверам
            total_servers = await conn.fetchval('SELECT COUNT(*) FROM servers')
            active_servers = await conn.fetchval('SELECT COUNT(*) FROM servers WHERE is_active = TRUE')
            
            # Платежеспособные пользователи
            paying_users_count = await conn.fetchval('''
                SELECT COUNT(DISTINCT user_id) FROM payments 
                WHERE status = 'completed'
            ''')
            
            # Пробный период
            trial_activated = await conn.fetchval('SELECT COUNT(*) FROM users WHERE trial_used = TRUE')
            trial_converted = await conn.fetchval('''
                SELECT COUNT(DISTINCT p.user_id)
                FROM payments p
                JOIN users u ON u.user_id = p.user_id
                WHERE p.status = 'completed'
                  AND u.trial_used = TRUE
            ''')
            trial_conversion_rate = 0.0
            if trial_activated and trial_activated > 0:
                trial_conversion_rate = (trial_converted or 0) / trial_activated * 100.0
        
        stats_text = (
            "📊 <b>Подробная статистика</b>\n\n"
            "👥 <b>Пользователи:</b>\n"
            f"• Всего пользователей: <i>{total_users}</i>\n"
            f"• Активных подписок: <i>{active_subscriptions}</i>\n"
            f"• Платежеспособных (платили): <i>{paying_users_count}</i>\n"
            f"• Новых сегодня: <i>{new_today}</i>\n"
            f"• Новых за неделю: <i>{new_week}</i>\n\n"
            "📈 <b>Активность пользователей:</b>\n"
            f"• Активных за 7 дней: <i>{active_7days}</i>\n"
            f"• Активных за 30 дней: <i>{active_users_30d}</i>\n"
            f"• Неактивных 30+ дней: <i>{inactive_30days}</i>\n\n"
            "💰 <b>Финансы:</b>\n"
            f"• Доход (RUB): <i>{total_revenue_rub / 100 if total_revenue_rub else 0:.2f}₽</i>\n"
            f"• Доход (Stars): <i>{total_revenue_stars}⭐</i>\n"
            f"• Платежей сегодня: <i>{payments_today}</i>\n"
            f"• Доход сегодня: <i>{revenue_today_rub / 100 if revenue_today_rub else 0:.2f}₽</i>\n"
            f"• Доход за 30 дней (RUB): <i>{revenue_30d_rub / 100 if revenue_30d_rub else 0:.2f}₽</i>\n"
            f"• ARPU 30д: <i>{arpu_30d:.2f}₽</i>\n"
            f"• ARPPU 30д: <i>{arppu_30d:.2f}₽</i>\n\n"
            "📉 <b>Отток и продления:</b>\n"
            f"• Подписок истекло за 30д (churn): <i>{churn_30d}</i>\n"
            f"• Подписок истекает в ближайшие 7д: <i>{expiring_7d}</i>\n\n"
            "🧪 <b>Пробный период:</b>\n"
            f"• Активировали триал: <i>{trial_activated}</i>\n"
            f"• Сделали платеж после триала: <i>{trial_converted}</i>\n"
            f"• Конверсия триала в оплату: <i>{trial_conversion_rate:.1f}%</i>\n\n"
            "🔑 <b>VPN Ключи:</b>\n"
            f"• Всего ключей: <i>{total_keys}</i>\n"
            f"• Активных ключей: <i>{active_keys}</i>\n\n"
            "🎁 <b>Рефералы:</b>\n"
            f"• Всего рефералов: <i>{total_referrals}</i>\n\n"
            "🖥️ <b>Серверы:</b>\n"
            f"• Всего серверов: <i>{total_servers}</i>\n"
            f"• Активных серверов: <i>{active_servers}</i>\n"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(stats_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    # Управление ценами
    @dp.callback_query(F.data == "admin_prices")
    async def handle_admin_prices(callback: CallbackQuery):
        """Управление ценами"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        # Получаем текущие цены из БД
        async with get_connection() as conn:
            price_settings = await conn.fetch('SELECT plan_id, price_rub, price_stars FROM price_settings')
            prices_dict = {row['plan_id']: row for row in price_settings}
        
        # Объединяем все планы
        all_plans = {**SUBSCRIPTION_PLANS_BASE, **RENEWAL_PLANS_BASE}
        
        text = "💰 <b>Управление ценами</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in all_plans.items():
            current_price_rub = prices_dict.get(plan_id, {}).get('price_rub', plan_data['price_rub'])
            current_price_stars = prices_dict.get(plan_id, {}).get('price_stars', plan_data['price_stars'])
            text += f"• {plan_data['title']}: {format_price_rub(current_price_rub)} | {format_price_stars(current_price_stars)}\n"
            builder.row(InlineKeyboardButton(
                text=f"✏️ {plan_data['title']}",
                callback_data=f"admin_edit_price:{plan_id}"
            ))
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_edit_price:"))
    async def handle_edit_price(callback: CallbackQuery, state: FSMContext):
        """Редактирование цены"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        plan_id = callback.data.split(":")[1]
        all_plans = {**SUBSCRIPTION_PLANS_BASE, **RENEWAL_PLANS_BASE}
        plan_data = all_plans.get(plan_id)
        
        if not plan_data:
            await safe_callback_answer(callback, "❌ План не найден", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"💰 <b>Редактирование цены</b>\n\n"
            f"План: {plan_data['title']}\n\n"
            f"Введите новую цену в формате:\n"
            f"<code>RUB СУММА_IN_КОПЕЙКАХ</code> или <code>STARS КОЛИЧЕСТВО</code>\n\n"
            f"Примеры:\n"
            f"<code>RUB 19900</code> - 199₽\n"
            f"<code>STARS 199</code> - 199⭐",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.SETTING_PRICE)
        await state.update_data(plan_id=plan_id)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.SETTING_PRICE)
    async def process_price_setting(message: Message, state: FSMContext):
        """Обработка установки цены"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        data = await state.get_data()
        plan_id = data.get('plan_id')
        
        parts = message.text.strip().upper().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: RUB 19900 или STARS 199")
            return
        
        currency_type = parts[0]
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("❌ Сумма должна быть числом")
            return
        
        async with get_connection() as conn:
            if currency_type == "RUB":
                # Получаем текущую цену в stars
                price_row = await conn.fetchrow('SELECT price_stars FROM price_settings WHERE plan_id = $1', plan_id)
                price_stars = price_row['price_stars'] if price_row else RENEWAL_PLANS_BASE.get(plan_id, SUBSCRIPTION_PLANS_BASE.get(plan_id, {}))['price_stars']
                price_stars = int(price_stars) if price_stars is not None else int(RENEWAL_PLANS_BASE.get(plan_id, SUBSCRIPTION_PLANS_BASE.get(plan_id, {}))['price_stars'])
                
                await conn.execute('''
                    INSERT INTO price_settings (plan_id, price_rub, price_stars, updated_at)
                    VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                    ON CONFLICT (plan_id) DO UPDATE
                    SET price_rub = $2, updated_at = CURRENT_TIMESTAMP
                ''', plan_id, amount, price_stars)
            elif currency_type == "STARS":
                # Получаем текущую цену в rub
                price_row = await conn.fetchrow('SELECT price_rub FROM price_settings WHERE plan_id = $1', plan_id)
                price_rub = price_row['price_rub'] if price_row else RENEWAL_PLANS_BASE.get(plan_id, SUBSCRIPTION_PLANS_BASE.get(plan_id, {}))['price_rub']
                price_rub = int(price_rub) if price_rub is not None else int(RENEWAL_PLANS_BASE.get(plan_id, SUBSCRIPTION_PLANS_BASE.get(plan_id, {}))['price_rub'])
                
                await conn.execute('''
                    INSERT INTO price_settings (plan_id, price_rub, price_stars, updated_at)
                    VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                    ON CONFLICT (plan_id) DO UPDATE
                    SET price_stars = $3, updated_at = CURRENT_TIMESTAMP
                ''', plan_id, price_rub, amount)
            else:
                await message.answer("❌ Неверный тип валюты. Используйте RUB или STARS")
                return
        
        # Получаем данные плана для сообщения
        all_plans = {**SUBSCRIPTION_PLANS_BASE, **RENEWAL_PLANS_BASE}
        plan_data = all_plans.get(plan_id)
        plan_title = plan_data['title'] if plan_data else plan_id
        
        # Получаем обновленную цену для отображения
        async with get_connection() as conn:
            price_row = await conn.fetchrow('SELECT price_rub, price_stars FROM price_settings WHERE plan_id = $1', plan_id)
            if price_row:
                price_rub = price_row['price_rub']
                price_stars = price_row['price_stars']
            else:
                price_rub = plan_data['price_rub'] if plan_data else 0
                price_stars = plan_data['price_stars'] if plan_data else 0
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к ценам", callback_data="admin_prices"))
        
        await message.answer(
            f"✅ Цена обновлена!\n\n"
            f"План: <b>{plan_title}</b>\n"
            f"Новая цена: {format_price_rub(price_rub)} | {format_price_stars(price_stars)}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    # Управление скидками
    @dp.callback_query(F.data == "admin_discounts")
    async def handle_admin_discounts(callback: CallbackQuery):
        """Управление скидками"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            settings = await conn.fetchrow('SELECT days_threshold, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1')
            if not settings:
                days_threshold = 3
                enable_for_all = False
            else:
                days_threshold = settings['days_threshold']
                enable_for_all = settings['enable_for_all']
        
        status_text = "✅ Включена" if enable_for_all else "❌ Выключена"
        threshold_text = f"{days_threshold} {'день' if days_threshold == 1 else 'дня' if days_threshold < 5 else 'дней'}" if days_threshold > 0 else "Выключена"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Дни до окончания для скидки", callback_data="admin_discount_threshold"))
        builder.row(InlineKeyboardButton(
            text=f"{'🔴 Выключить' if enable_for_all else '🟢 Включить'} скидку для всех",
            callback_data=f"admin_discount_toggle_all"
        ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            f"🎁 <b>Управление скидками</b>\n\n"
            f"<b>Режим 1:</b> Скидка за N дней до окончания\n"
            f"Текущее значение: <b>{threshold_text}</b>\n"
            f"(0 = скидка отключена)\n\n"
            f"<b>Режим 2:</b> Скидка для всех пользователей\n"
            f"Статус: <b>{status_text}</b>\n\n"
            f"Выберите действие:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_discount_threshold")
    async def handle_discount_threshold(callback: CallbackQuery, state: FSMContext):
        """Настройка дней до окончания для скидки"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_discounts"))
        
        await callback.message.edit_text(
            "✏️ <b>Дни до окончания для скидки</b>\n\n"
            "Введите количество дней до окончания подписки, когда будет показываться скидка:\n\n"
            "• <code>0</code> - скидка отключена\n"
            "• <code>3</code> - скидка показывается за 3 дня до окончания (по умолчанию)\n"
            "• <code>7</code> - скидка показывается за 7 дней до окончания\n\n"
            "И так далее...",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.DISCOUNT_DAYS_THRESHOLD)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.DISCOUNT_DAYS_THRESHOLD)
    async def process_discount_threshold(message: Message, state: FSMContext):
        """Обработка дней для скидки"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        try:
            days = int(message.text.strip())
            if days < 0:
                await message.answer("❌ Количество дней не может быть отрицательным")
                return
        except ValueError:
            await message.answer("❌ Введите число")
            return
        
        async with get_connection() as conn:
            existing = await conn.fetchrow('SELECT id FROM discount_settings ORDER BY id DESC LIMIT 1')
            if not existing:
                await conn.execute('''
                    INSERT INTO discount_settings (days_threshold, enable_for_all, updated_at)
                    VALUES ($1, FALSE, CURRENT_TIMESTAMP)
                ''', days)
            else:
                await conn.execute('''
                    UPDATE discount_settings
                    SET days_threshold = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM discount_settings ORDER BY id DESC LIMIT 1)
                ''', days)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к скидкам", callback_data="admin_discounts"))
        
        threshold_text = f"{days} {'день' if days == 1 else 'дня' if days < 5 else 'дней'}" if days > 0 else "Выключена"
        await message.answer(
            f"✅ Количество дней для скидки установлено: <b>{threshold_text}</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    @dp.callback_query(F.data == "admin_discount_toggle_all")
    async def handle_discount_toggle_all(callback: CallbackQuery):
        """Переключение режима скидки для всех"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            existing = await conn.fetchrow('SELECT id, enable_for_all FROM discount_settings ORDER BY id DESC LIMIT 1')
            if not existing:
                await conn.execute('''
                    INSERT INTO discount_settings (days_threshold, enable_for_all, updated_at)
                    VALUES (3, TRUE, CURRENT_TIMESTAMP)
                ''')
                new_value = True
            else:
                new_value = not existing['enable_for_all']
                await conn.execute('''
                    UPDATE discount_settings
                    SET enable_for_all = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM discount_settings ORDER BY id DESC LIMIT 1)
                ''', new_value)
        
        status_text = "✅ Включена" if new_value else "❌ Выключена"
        await safe_callback_answer(callback, f"Скидка для всех: {status_text}")
        # Обновляем интерфейс
        await handle_admin_discounts(callback)
    
    # TODO: Добавить остальные обработчики:
    # - admin_broadcast (рассылка)
    # - admin_referral (реферальная система)
    # - admin_trial (пробный период)
    # - admin_servers (серверы)
    # - admin_balance (баланс)
    # - admin_manage_admins (админы)
    # - admin_manage_managers (менеджеры)
    # - admin_device_apps (приложения)
    # - admin_manual_reminder (напоминания)
