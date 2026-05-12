"""
Админ-панель - все обработчики для администраторов
"""
import html as html_std
import logging
import asyncio
import pytz
from datetime import datetime, timedelta
from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..database import get_connection, get_support_link, set_announcement_text, get_device_instruction_photos, get_device_instruction_photos_list, add_device_instruction_photo, delete_device_instruction_photo
from ..config import AppConfig
from ..plans import SUBSCRIPTION_PLANS_BASE, RENEWAL_PLANS_BASE, format_price_rub, format_price_stars, format_price_both, get_renewal_plans
from ..subscriptions import create_or_activate_keys_for_all_servers, create_keys_for_specific_server, update_vless_links_for_server
from ..xui_client import XUIClient
from ..webhook_server import WebhookServer

logger = logging.getLogger(__name__)

DEVICE_TYPES = {
    "iphone": "📱 iPhone",
    "android": "🤖 Android",
    "windows": "🪟 Windows",
    "macbook": "💻 MacBook",
    "linux": "🐧 Linux"
}


async def _admin_traffic_panel_builder() -> tuple[str, InlineKeyboardBuilder]:
    """Текст и клавиатура экрана «Трафик и пакеты ГБ» (список пакетов из БД)."""
    async with get_connection() as conn:
        ts = await conn.fetchrow(
            """
            SELECT t.default_monthly_gb, t.panel_sync_min_seconds, t.tg_relay_server_id,
                   s.name AS tg_relay_server_name
            FROM traffic_settings t
            LEFT JOIN servers s ON s.id = t.tg_relay_server_id
            ORDER BY t.id DESC
            LIMIT 1
            """
        )
        default_gb = int(ts["default_monthly_gb"] or 50) if ts else 50
        sync_sec = int(ts["panel_sync_min_seconds"] or 240) if ts else 240
        tg_sid = int(ts["tg_relay_server_id"]) if ts and ts.get("tg_relay_server_id") is not None else None
        tg_name = (ts.get("tg_relay_server_name") or "") if ts else ""
        packs = await conn.fetch(
            """
            SELECT id, title, gb_amount, price_rub, price_stars, is_active, display_order
            FROM gb_pack_products
            ORDER BY gb_amount ASC, display_order ASC, id ASC
            """
        )
    if tg_sid:
        tg_line = (
            f"• Сервер «ТГ безлимит» (при лимите — одна ссылка <b>‼️ ТГ БЕЗЛИМИТ ‼️</b> в конце подписки): "
            f"<b>#{tg_sid}</b> {html_std.escape(str(tg_name))}\n"
        )
    else:
        tg_line = (
            "• Сервер «ТГ безлимит»: <i>не выбран</i> — при лимите только информационные строки подписки\n"
        )
    lines = [
        "📶 <b>Трафик и пакеты ГБ</b>\n",
        f"• Лимит по умолчанию: <b>{default_gb} ГБ</b> / месяц (день сброса = день первой оплаты)\n",
        f"• Мин. интервал синхронизации с панелями: <b>{sync_sec}</b> сек\n",
        tg_line,
        "\n<b>Пакеты доп. ГБ</b> (хранятся в таблице <code>gb_pack_products</code>):",
    ]
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Лимит по умолчанию (ГБ)", callback_data="admin_traffic_edit:default_gb")
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Интервал синхронизации (сек)", callback_data="admin_traffic_edit:sync_sec")
    )
    builder.row(
        InlineKeyboardButton(text="📡 Сервер «ТГ безлимит»", callback_data="admin_traffic_tg_relay_pick")
    )
    builder.row(InlineKeyboardButton(text="➕ Новый пакет ГБ", callback_data="admin_gb_pack_add"))
    if packs:
        for p in packs:
            st = "✅" if p["is_active"] else "⏸"
            lines.append(
                f"\n{st} <b>#{p['id']}</b> {html_std.escape(str(p['title']))} — +{p['gb_amount']} ГБ, "
                f"{p['price_rub'] // 100}₽ / {p['price_stars']}⭐"
            )
            builder.row(
                InlineKeyboardButton(
                    text=f"✏️ #{p['id']}",
                    callback_data=f"admin_gb_pack_manage:{p['id']}",
                ),
                InlineKeyboardButton(
                    text=f"{st} вкл",
                    callback_data=f"admin_gb_pack_toggle:{p['id']}",
                ),
            )
    else:
        lines.append("\n<i>Пакетов пока нет — добавь через «Новый пакет ГБ».</i>")
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
    return "\n".join(lines), builder


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
    TRAFFIC_SETTING_VALUE = State()
    TRAFFIC_TG_RELAY_SERVER_ID = State()
    GB_PACK_TITLE = State()
    GB_PACK_GB = State()
    GB_PACK_PRICE_RUB = State()
    GB_PACK_PRICE_STARS = State()
    GB_PACK_EDIT_FIELD = State()
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
    UTM_TAG = State()
    UTM_DESCRIPTION = State()
    UTM_BONUS_DAYS = State()
    USER_INFO_ID = State()


class AdminEditStates(StatesGroup):
    EDIT_ANNOUNCEMENT = State()


class AdminManualReminderStates(StatesGroup):
    CHOOSING_TIME_BEFORE = State()


class AddServerSteps(StatesGroup):
    WAITING_NAME = State()
    WAITING_PANEL_URL = State()
    WAITING_USERNAME = State()
    WAITING_PASSWORD = State()
    WAITING_INBOUND_ID = State()
    WAITING_ORDER = State()
    WAITING_SYSTEM = State()
    CONFIRMING = State()


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
    builder.row(InlineKeyboardButton(text="🔔 Логи активности", callback_data="admin_realtime_logs"))
    builder.row(InlineKeyboardButton(text="💰 Управление ценами", callback_data="admin_prices"))
    builder.row(InlineKeyboardButton(text="🚀 Тарифы (Lite/Standard/Pro)", callback_data="admin_tier_prices"))
    builder.row(InlineKeyboardButton(text="📶 Трафик и пакеты ГБ", callback_data="admin_traffic"))
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
    builder.row(InlineKeyboardButton(text="📈 UTM метки", callback_data="admin_utm"))
    builder.row(InlineKeyboardButton(text="👤 Инфо о пользователе", callback_data="admin_user_info"))
    builder.row(InlineKeyboardButton(text="⏰ Ручная отправка напоминаний", callback_data="admin_manual_reminder"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_to_main"))
    return builder.as_markup()



async def get_broadcast_constructor_menu(state_data: dict):
    """Получить меню конструктора рассылки"""
    text = state_data.get('broadcast_text', '')
    media_type = state_data.get('media_type')
    buttons = state_data.get('broadcast_buttons', [])
    filter_type = state_data.get('broadcast_filter', 'all')
    test_mode = state_data.get('broadcast_test', False)
    
    status_text = "📢 <b>Конструктор рассылки</b>\n\n"
    status_text += "<b>Текущий состав:</b>\n"
    
    if text:
        status_text += f"✅ Текст: {text[:50]}...\n"
    else:
        status_text += "❌ Текст не добавлен\n"
    
    if media_type:
        media_name = "🖼️ Фото" if media_type == "photo" else "🎥 Видео" if media_type == "video" else "📄 Документ" if media_type == "document" else "🎬 GIF"
        status_text += f"✅ Медиа: {media_name}\n"
    else:
        status_text += "❌ Медиа не добавлено\n"
    
    if buttons:
        status_text += f"✅ Кнопки: {len(buttons)} шт.\n"
    else:
        status_text += "❌ Кнопки не добавлены\n"
    
    filter_names = {
        'all': 'Все пользователи',
        'active': 'С активной подпиской',
        'inactive': 'Без подписки',
        'active_7d': 'Активные за 7 дней',
        'active_30d': 'Активные за 30 дней',
        'with_referrals': 'С рефералами',
        'trial_used': 'Использовали пробный период',
        'trial_not_used': 'Не использовали пробный период'
    }
    filter_name = filter_names.get(filter_type, 'Не выбрано')
    status_text += f"✅ Фильтр: {filter_name}\n"
    
    if test_mode:
        status_text += "🧪 <b>ТЕСТОВЫЙ РЕЖИМ</b> (только админ)\n"
    
    status_text += "\n<b>Выберите действие:</b>"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить текст" if not text else "✏️ Изменить текст", callback_data="broadcast_add_text"))
    builder.row(InlineKeyboardButton(text="🖼️ Добавить медиа" if not media_type else "🖼️ Изменить медиа", callback_data="broadcast_add_media"))
    builder.row(InlineKeyboardButton(text="🔘 Добавить кнопки" if not buttons else f"🔘 Кнопки ({len(buttons)})", callback_data="broadcast_add_buttons"))
    builder.row(InlineKeyboardButton(text="👥 Выбрать получателей", callback_data="broadcast_choose_filter"))
    builder.row(InlineKeyboardButton(text="🧪 Тест (только админ)" if not test_mode else "✅ Тест включен", callback_data="broadcast_toggle_test"))
    
    if text or media_type:
        builder.row(InlineKeyboardButton(text="✅ Отправить рассылку", callback_data="broadcast_confirm"))
    
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"))
    
    return status_text, builder.as_markup()


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
                
            # Статистика использования подписок (из новых логов)
            # DAU (24h), WAU (7d), MAU (30d) по реальным запросам
            sub_dau_24h = await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL \'24 hours\'')
            sub_wau_7d = await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL \'7 days\'')
            sub_mau_30d = await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL \'30 days\'')
            sub_requests_today = await conn.fetchval('SELECT COUNT(*) FROM subscription_usage_logs WHERE DATE(timestamp) = CURRENT_DATE')
            
            # Топ платформ за 7 дней
            top_platforms_rows = await conn.fetch('''
                SELECT user_agent, COUNT(*) as count 
                FROM subscription_usage_logs 
                WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
                GROUP BY user_agent 
                ORDER BY count DESC 
                LIMIT 5
            ''')
            
            platforms_text = ""
            for row in top_platforms_rows:
                ua = (row['user_agent'] or "Unknown").split('/')[0].split(' ')[0][:15]
                platforms_text += f"  • {ua}: <i>{row['count']} запр.</i>\n"
            if not platforms_text: platforms_text = "  • Данных пока нет\n"

            # Статистика продаж по источникам
            sales_source = await conn.fetch('''
                SELECT payment_source, COUNT(*) as count, SUM(amount) as total
                FROM payments 
                WHERE status = 'completed'
                GROUP BY payment_source
            ''')
            sales_stats = {"bot": {"count": 0, "total": 0}, "miniapp": {"count": 0, "total": 0}}
            for row in sales_source:
                src = row['payment_source'] or 'bot'
                if src in sales_stats:
                    sales_stats[src] = {"count": row['count'], "total": row['total']}
        
        stats_text = (
            "📊 <b>Подробная статистика</b>\n\n"
            "👥 <b>Пользователи:</b>\n"
            f"• Всего пользователей: <i>{total_users}</i>\n"
            f"• Активных подписок: <i>{active_subscriptions}</i>\n"
            f"• Платежеспособных (платили): <i>{paying_users_count}</i>\n"
            f"• Новых сегодня: <i>{new_today}</i>\n"
            f"• Новых за неделю: <i>{new_week}</i>\n\n"

            "📈 <b>Активность VPN (по логам):</b>\n"
            f"  • DAU (24ч): <b>{sub_dau_24h or 0}</b> чел. 🏆\n"
            f"  • WAU (7дн): <b>{sub_wau_7d or 0}</b> чел.\n"
            f"  • MAU (30дн): <b>{sub_mau_30d or 0}</b> чел.\n"
            f"  • Запросов сегодня: <b>{sub_requests_today or 0}</b>\n\n"
            
            "📱 <b>Топ клиентов (7дн):</b>\n"
            f"{platforms_text}\n"
            
            "🤖 <b>Активность в боте:</b>\n"
            f"• Активных за 7 дней: <i>{active_7days}</i>\n"
            f"• Активных за 30 дней: <i>{active_users_30d}</i>\n"
            f"• Неактивных 30+ дней: <i>{inactive_30days}</i>\n\n"

            "💰 <b>Финансы:</b>\n"
            f"• Доход (RUB): <i>{total_revenue_rub / 100 if total_revenue_rub else 0:.2f}₽</i>\n"
            f"• Доход (Stars): <i>{total_revenue_stars}⭐</i>\n"
            f"• Платежи через Бота: <b>{sales_stats['bot']['count']}</b> (<i>{sales_stats['bot']['total']/100:.0f}₽</i>)\n"
            f"• Платежи через Mini-App: <b>{sales_stats['miniapp']['count']}</b> (<i>{sales_stats['miniapp']['total']/100:.0f}₽</i>)\n"
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
            f"• Активных серверов: <i>{active_servers}</i>\n\n"
            
            f"🕒 {datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(stats_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    # Логи активности в реальном времени
    @dp.callback_query(F.data == "admin_realtime_logs")
    async def handle_admin_realtime_logs(callback: CallbackQuery):
        """Просмотр последних действий пользователей (по логам подписки)"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            logs = await conn.fetch('''
                SELECT l.user_id, l.type, l.detail, l.timestamp, u.username, u.first_name 
                FROM (
                    SELECT user_id, 'vpn' as type, user_agent as detail, timestamp FROM subscription_usage_logs
                    UNION ALL
                    SELECT user_id, 'miniapp' as type, action as detail, timestamp FROM miniapp_usage_logs
                ) l
                LEFT JOIN users u ON l.user_id = u.user_id
                ORDER BY l.timestamp DESC 
                LIMIT 20
            ''')
        
        if not logs:
            text = "🔔 <b>Логи активности</b>\n\nЛоги пока пусты. Дождитесь подключений пользователей."
        else:
            text = "🔔 <b>Последние действия (реальное время):</b>\n\n"
            for log in logs:
                user_id = log['user_id']
                name = log['first_name'] or log['username'] or f"ID:{user_id}"
                ltype = log['type']
                detail = log['detail'] or "Unknown"
                
                # Иконка и текст в зависимости от типа
                if ltype == 'vpn':
                    ua = detail.split('/')[0].split(' ')[0][:12]
                    action_text = f"🔌 Подключился через: <code>{ua}</code>"
                else:
                    action_text = f"📱 Мини-апп: <code>{detail.capitalize()}</code>"
                
                # Форматирование времени (МСК)
                ts = log['timestamp']
                if ts.tzinfo is None:
                    ts = pytz.utc.localize(ts).astimezone(pytz.timezone('Europe/Moscow'))
                else:
                    ts = ts.astimezone(pytz.timezone('Europe/Moscow'))
                
                time_str = ts.strftime('%H:%M:%S')
                
                text += f"🕒 <code>{time_str}</code> | <b>{name}</b>\n"
                text += f"└ {action_text}\n\n"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_realtime_logs"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                raise
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
        """Обработка установки цены (legacy plans, tier plans, bypass packs)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return

        data = await state.get_data()
        tier_plan_id = data.get('tier_plan_id')
        bypass_pack_id = data.get('bypass_pack_id')
        plan_id = data.get('plan_id')

        text_input = message.text.strip().upper()

        # --- Handle bypass pack edit ---
        if bypass_pack_id:
            if text_input == "TOGGLE":
                async with get_connection() as conn:
                    await conn.execute(
                        "UPDATE bypass_pack_products SET is_active = NOT is_active WHERE id = $1",
                        bypass_pack_id,
                    )
                await message.answer("✅ Статус пакета изменён.")
                await state.clear()
                return
            parts = text_input.split()
            if len(parts) != 2:
                await message.answer("❌ Формат: RUB 5900 или STARS 59 или TOGGLE")
                return
            currency_type, amt_str = parts
            try:
                amount = int(amt_str)
            except ValueError:
                await message.answer("❌ Сумма должна быть числом")
                return
            async with get_connection() as conn:
                if currency_type == "RUB":
                    await conn.execute(
                        "UPDATE bypass_pack_products SET price_rub = $1, updated_at = NOW() WHERE id = $2",
                        amount, bypass_pack_id,
                    )
                elif currency_type == "STARS":
                    await conn.execute(
                        "UPDATE bypass_pack_products SET price_stars = $1, updated_at = NOW() WHERE id = $2",
                        amount, bypass_pack_id,
                    )
                else:
                    await message.answer("❌ Используйте RUB, STARS или TOGGLE")
                    return
            await message.answer("✅ Цена bypass пакета обновлена!")
            await state.clear()
            return

        # --- Handle tier plan edit ---
        if tier_plan_id:
            from ..plans import TIER_PLANS_BASE
            parts = text_input.split()
            if len(parts) != 2:
                await message.answer("❌ Формат: RUB 9900 или STARS 99")
                return
            currency_type, amt_str = parts
            try:
                amount = int(amt_str)
            except ValueError:
                await message.answer("❌ Сумма должна быть числом")
                return
            base = TIER_PLANS_BASE.get(tier_plan_id, {})
            tier = base.get("tier", "")
            duration = base.get("duration", 1)
            async with get_connection() as conn:
                if currency_type == "RUB":
                    existing = await conn.fetchrow(
                        "SELECT price_stars FROM tier_price_settings WHERE tier = $1 AND duration_months = $2",
                        tier, duration,
                    )
                    stars = existing["price_stars"] if existing else base.get("price_stars", 0)
                    await conn.execute(
                        """
                        INSERT INTO tier_price_settings (tier, duration_months, price_rub, price_stars, updated_at)
                        VALUES ($1, $2, $3, $4, NOW())
                        ON CONFLICT (tier, duration_months) DO UPDATE SET price_rub = $3, updated_at = NOW()
                        """,
                        tier, duration, amount, stars,
                    )
                elif currency_type == "STARS":
                    existing = await conn.fetchrow(
                        "SELECT price_rub FROM tier_price_settings WHERE tier = $1 AND duration_months = $2",
                        tier, duration,
                    )
                    rub = existing["price_rub"] if existing else base.get("price_rub", 0)
                    await conn.execute(
                        """
                        INSERT INTO tier_price_settings (tier, duration_months, price_rub, price_stars, updated_at)
                        VALUES ($1, $2, $3, $4, NOW())
                        ON CONFLICT (tier, duration_months) DO UPDATE SET price_stars = $4, updated_at = NOW()
                        """,
                        tier, duration, rub, amount,
                    )
                else:
                    await message.answer("❌ Используйте RUB или STARS")
                    return
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="◀️ К тарифам", callback_data="admin_tier_prices"))
            await message.answer(
                f"✅ Цена тарифа обновлена: {base.get('title', tier_plan_id)}",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            await state.clear()
            return

        # --- Handle legacy plan edit ---
        parts = text_input.split()
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
        
        all_plans = {**SUBSCRIPTION_PLANS_BASE, **RENEWAL_PLANS_BASE}
        plan_data = all_plans.get(plan_id)
        plan_title = plan_data['title'] if plan_data else plan_id
        
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

    # ------------------------------------------------------------------
    # Tier price management (Lite/Standard/Pro)
    # ------------------------------------------------------------------
    @dp.callback_query(F.data == "admin_tier_prices")
    async def handle_admin_tier_prices(callback: CallbackQuery):
        """Управление ценами тарифов Lite/Standard/Pro"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        from ..plans import TIER_PLANS_BASE, TIERS, get_tier_plans, get_bypass_packs

        plans = await get_tier_plans()
        text = "🚀 <b>Тарифы: Lite / Standard / Pro</b>\n\n"

        builder = InlineKeyboardBuilder()
        for tier_id in ["lite", "standard", "pro"]:
            t = TIERS[tier_id]
            text += f"<b>{t['name']}</b> ({t['bypass_gb']} ГБ bypass, до {t['max_devices']} устр.)\n"
            for plan_id, plan_data in plans.items():
                if plan_data.get("tier") == tier_id:
                    text += f"  • {plan_data['title']}: {format_price_rub(plan_data['price_rub'])} | {format_price_stars(plan_data['price_stars'])}\n"
            text += "\n"

        text += "<b>Bypass пакеты (докупка):</b>\n"
        packs = await get_bypass_packs()
        for p in packs:
            text += f"  • +{p['gb_amount']} ГБ: {format_price_rub(p['price_rub'])} | {format_price_stars(p['price_stars'])}\n"
        text += "\n💡 Докупка должна быть менее выгодной, чем апгрейд.\n"

        for plan_id in plans:
            builder.row(InlineKeyboardButton(
                text=f"✏️ {plans[plan_id]['title']}",
                callback_data=f"admin_tier_edit:{plan_id}",
            ))

        builder.row(InlineKeyboardButton(text="📶 Bypass пакеты", callback_data="admin_bypass_packs"))
        builder.row(InlineKeyboardButton(text="🏷️ Пометить bypass-сервер", callback_data="admin_mark_bypass"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_tier_edit:"))
    async def handle_admin_tier_edit(callback: CallbackQuery, state: FSMContext):
        """Редактирование цены тарифа"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        plan_id = callback.data.split(":")[1]
        from ..plans import get_tier_plans
        plans = await get_tier_plans()
        plan_data = plans.get(plan_id)
        if not plan_data:
            await safe_callback_answer(callback, "❌ Не найден", show_alert=True)
            return

        await callback.message.edit_text(
            f"✏️ <b>Редактирование: {plan_data['title']}</b>\n\n"
            f"Текущая цена: {format_price_rub(plan_data['price_rub'])} | {format_price_stars(plan_data['price_stars'])}\n\n"
            f"Введите новую цену:\n"
            f"<code>RUB КОПЕЙКИ</code> или <code>STARS КОЛИЧЕСТВО</code>\n\n"
            f"Пример: <code>RUB 9900</code> = 99₽",
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.SETTING_PRICE)
        await state.update_data(tier_plan_id=plan_id)
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "admin_bypass_packs")
    async def handle_admin_bypass_packs(callback: CallbackQuery):
        """Управление bypass пакетами"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        async with get_connection() as conn:
            packs = await conn.fetch(
                "SELECT id, title, gb_amount, price_rub, price_stars, is_active FROM bypass_pack_products ORDER BY gb_amount"
            )

        text = "📶 <b>Bypass пакеты (докупка ГБ)</b>\n\n"
        builder = InlineKeyboardBuilder()
        for p in packs:
            status = "✅" if p["is_active"] else "❌"
            text += f"{status} +{p['gb_amount']} ГБ — {p['title']} — {format_price_rub(p['price_rub'])}\n"
            builder.row(InlineKeyboardButton(
                text=f"✏️ +{p['gb_amount']} ГБ ({format_price_rub(p['price_rub'])})",
                callback_data=f"admin_bp_edit:{p['id']}",
            ))

        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_tier_prices"))
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_bp_edit:"))
    async def handle_admin_bp_edit(callback: CallbackQuery, state: FSMContext):
        """Редактирование bypass пакета"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        pack_id = int(callback.data.split(":")[1])
        async with get_connection() as conn:
            p = await conn.fetchrow("SELECT * FROM bypass_pack_products WHERE id = $1", pack_id)
        if not p:
            await safe_callback_answer(callback, "❌ Не найден", show_alert=True)
            return

        await callback.message.edit_text(
            f"✏️ <b>Bypass пакет: +{p['gb_amount']} ГБ</b>\n\n"
            f"Текущая цена: {format_price_rub(p['price_rub'])} | {format_price_stars(p['price_stars'])}\n"
            f"Активен: {'да' if p['is_active'] else 'нет'}\n\n"
            f"Введите новую цену:\n"
            f"<code>RUB КОПЕЙКИ</code> или <code>STARS КОЛ-ВО</code>\n"
            f"Или <code>TOGGLE</code> для вкл/выкл",
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.SETTING_PRICE)
        await state.update_data(bypass_pack_id=pack_id)
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "admin_mark_bypass")
    async def handle_admin_mark_bypass(callback: CallbackQuery):
        """Пометить сервер как bypass"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        async with get_connection() as conn:
            servers = await conn.fetch(
                "SELECT id, name, is_bypass, is_active FROM servers ORDER BY display_order, id"
            )

        text = "🏷️ <b>Bypass серверы</b>\n\nОтметьте серверы, которые используются для обхода блокировок:\n\n"
        builder = InlineKeyboardBuilder()
        for s in servers:
            mark = "🔓" if s["is_bypass"] else "🌐"
            active = "✅" if s["is_active"] else "⏸"
            builder.row(InlineKeyboardButton(
                text=f"{mark} {active} {s['name']}",
                callback_data=f"admin_toggle_bypass:{s['id']}",
            ))

        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_tier_prices"))
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_toggle_bypass:"))
    async def handle_admin_toggle_bypass(callback: CallbackQuery):
        """Toggle bypass flag on server"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        server_id = int(callback.data.split(":")[1])
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE servers SET is_bypass = NOT is_bypass WHERE id = $1",
                server_id,
            )
            s = await conn.fetchrow("SELECT name, is_bypass FROM servers WHERE id = $1", server_id)

        status = "bypass 🔓" if s["is_bypass"] else "обычный 🌐"
        await safe_callback_answer(callback, f"{s['name']} → {status}", show_alert=True)
        # Refresh the list
        await handle_admin_mark_bypass(callback)

    @dp.callback_query(F.data == "admin_traffic")
    async def handle_admin_traffic(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.clear()
        text, builder = await _admin_traffic_panel_builder()
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_traffic_edit:"))
    async def handle_admin_traffic_edit(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        mode = callback.data.split(":")[1]
        prompts = {
            "default_gb": "Введите месячный лимит по умолчанию в ГБ (например 50):",
            "sync_sec": "Введите минимальный интервал опроса панелей в секундах (например 240):",
        }
        await state.set_state(AdminStates.TRAFFIC_SETTING_VALUE)
        await state.update_data(traffic_setting_mode=mode)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_traffic"))
        await callback.message.edit_text(
            f"📶 <b>Настройка трафика</b>\n\n{prompts.get(mode, 'Введите значение:')}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "admin_traffic_tg_relay_pick")
    async def handle_admin_traffic_tg_relay_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.clear()
        async with get_connection() as conn:
            servers = await conn.fetch(
                """
                SELECT id, name FROM servers
                WHERE is_active = TRUE
                ORDER BY display_order ASC NULLS LAST, id ASC
                """
            )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="⌨️ Указать по ID (любой сервер в БД)",
                callback_data="admin_traffic_tg_relay_by_id",
            )
        )
        for s in servers:
            nm = (s["name"] or "")[:40]
            builder.row(
                InlineKeyboardButton(
                    text=f"#{s['id']} {nm}",
                    callback_data=f"admin_traffic_tg_relay_set:{s['id']}",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text="🚫 Без сервера (только плейсхолдеры)",
                callback_data="admin_traffic_tg_relay_clear",
            )
        )
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_traffic"))
        await callback.message.edit_text(
            "📡 <b>Сервер «ТГ безлимит»</b>\n\n"
            "При превышении месячного лимита в подписке пользователь увидит "
            "<b>одну</b> рабочую ссылку на выбранный узел (в клиенте имя <b>‼️ ТГ БЕЗЛИМИТ ‼️</b>, в конце списка). "
            "Ограничение «только Telegram» задаётся в <b>Xray</b> на inbound этого сервера.\n\n"
            "<b>Обычным пользователям</b> узел не показывай: в карточке сервера — "
            "<b>«Скрыть из подписки Happ»</b>. Сервер для реле можно держать на паузе — "
            "для id, выбранного здесь, ключи при паузе <b>не снимаются</b> автоматически.\n\n"
            "Выберите активный сервер из списка или укажите <b>ID вручную</b>.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "admin_traffic_tg_relay_by_id")
    async def handle_admin_traffic_tg_relay_by_id(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.set_state(AdminStates.TRAFFIC_TG_RELAY_SERVER_ID)
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="◀️ К списку серверов", callback_data="admin_traffic_tg_relay_pick"))
        b.row(InlineKeyboardButton(text="◀️ К трафику", callback_data="admin_traffic"))
        await callback.message.edit_text(
            "⌨️ <b>ID сервера для «ТГ безлимит»</b>\n\n"
            "Отправьте одним сообщением <b>числовой id</b> записи в таблице серверов "
            "(тот же номер, что в админке «Серверы» — колонка id).\n\n"
            "Так можно привязать узел, которого <b>нет</b> в списке выше (например, выключен для продажи, "
            "но оставлен для реле).",
            reply_markup=b.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.message(AdminStates.TRAFFIC_TG_RELAY_SERVER_ID)
    async def process_admin_traffic_tg_relay_server_id(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        raw = (message.text or "").strip()
        try:
            sid = int(raw)
        except ValueError:
            await message.answer("❌ Нужно целое число — id сервера (например <code>12</code>).", parse_mode="HTML")
            return
        if sid < 1:
            await message.answer("❌ ID должен быть положительным.")
            return
        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, is_active FROM servers WHERE id = $1",
                sid,
            )
            if not row:
                await message.answer(f"❌ Сервера с id=<b>{sid}</b> в базе нет.", parse_mode="HTML")
                return
            await conn.execute(
                """
                UPDATE traffic_settings
                SET tg_relay_server_id = $1, updated_at = CURRENT_TIMESTAMP
                WHERE id = (SELECT id FROM traffic_settings ORDER BY id DESC LIMIT 1)
                """,
                sid,
            )
        await state.clear()
        nm = html_std.escape(str(row["name"] or ""))
        st = "активен" if row["is_active"] else "⚠️ выключен в продаже"
        text, builder = await _admin_traffic_panel_builder()
        tip = (
            "\n\nВ карточке сервера нажми <b>«Скрыть из подписки Happ»</b>, если узел не должен быть в общем списке."
            if row["is_active"]
            else "\n\nСервер на паузе — для реле это ок: ключи для этого id не снимаются. При желании включи обратно и скрой из подписки."
        )
        await message.answer(
            f"✅ «ТГ безлимит» → сервер <b>#{sid}</b> {nm} ({st}).{tip}",
            parse_mode="HTML",
        )
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

    @dp.callback_query(F.data.startswith("admin_traffic_tg_relay_set:"))
    async def handle_admin_traffic_tg_relay_set(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.clear()
        try:
            sid = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "❌ Неверный id", show_alert=True)
            return
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE traffic_settings
                SET tg_relay_server_id = $1, updated_at = CURRENT_TIMESTAMP
                WHERE id = (SELECT id FROM traffic_settings ORDER BY id DESC LIMIT 1)
                """,
                sid,
            )
        text, builder = await _admin_traffic_panel_builder()
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback, "✅ Сохранено")

    @dp.callback_query(F.data == "admin_traffic_tg_relay_clear")
    async def handle_admin_traffic_tg_relay_clear(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.clear()
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE traffic_settings
                SET tg_relay_server_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = (SELECT id FROM traffic_settings ORDER BY id DESC LIMIT 1)
                """
            )
        text, builder = await _admin_traffic_panel_builder()
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback, "✅ Снято")

    @dp.message(AdminStates.TRAFFIC_SETTING_VALUE)
    async def process_admin_traffic_setting_value(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        data = await state.get_data()
        mode = data.get("traffic_setting_mode")
        try:
            val = int((message.text or "").strip())
        except ValueError:
            await message.answer("❌ Введите целое число")
            return

        if mode == "default_gb" and (val < 1 or val > 100000):
            await message.answer("❌ Допустимо от 1 до 100000 ГБ")
            return
        if mode == "sync_sec" and (val < 30 or val > 86400):
            await message.answer("❌ Интервал: от 30 до 86400 сек")
            return

        async with get_connection() as conn:
            if mode == "default_gb":
                await conn.execute(
                    """
                    UPDATE traffic_settings SET default_monthly_gb = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM traffic_settings ORDER BY id DESC LIMIT 1)
                    """,
                    val,
                )
            elif mode == "sync_sec":
                await conn.execute(
                    """
                    UPDATE traffic_settings SET panel_sync_min_seconds = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM traffic_settings ORDER BY id DESC LIMIT 1)
                    """,
                    val,
                )

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ К трафику", callback_data="admin_traffic"))
        await message.answer("✅ Сохранено", reply_markup=builder.as_markup())
        await state.clear()

    @dp.callback_query(F.data == "admin_gb_pack_add")
    async def handle_admin_gb_pack_add(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        await state.set_state(AdminStates.GB_PACK_TITLE)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_traffic"))
        await callback.message.edit_text(
            "➕ <b>Новый пакет ГБ</b>\n\nВведите <b>название</b> для отображения в магазине:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.message(AdminStates.GB_PACK_TITLE)
    async def process_gb_pack_title(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await state.clear()
            return
        title = (message.text or "").strip()
        if len(title) < 2:
            await message.answer("❌ Слишком короткое название")
            return
        await state.update_data(gb_pack_title=title)
        await state.set_state(AdminStates.GB_PACK_GB)
        await message.answer("Введите объём пакета в <b>ГБ</b> (целое число):", parse_mode="HTML")

    @dp.message(AdminStates.GB_PACK_GB)
    async def process_gb_pack_gb(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await state.clear()
            return
        try:
            gb = int((message.text or "").strip())
        except ValueError:
            await message.answer("❌ Введите целое число")
            return
        if gb < 1 or gb > 100000:
            await message.answer("❌ От 1 до 100000")
            return
        await state.update_data(gb_pack_gb=gb)
        await state.set_state(AdminStates.GB_PACK_PRICE_RUB)
        await message.answer("Введите цену в <b>копейках</b> (например 9900 = 99₽):", parse_mode="HTML")

    @dp.message(AdminStates.GB_PACK_PRICE_RUB)
    async def process_gb_pack_rub(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await state.clear()
            return
        try:
            rub = int((message.text or "").strip())
        except ValueError:
            await message.answer("❌ Введите целое число")
            return
        if rub < 0:
            await message.answer("❌ Не может быть отрицательной")
            return
        await state.update_data(gb_pack_rub=rub)
        await state.set_state(AdminStates.GB_PACK_PRICE_STARS)
        await message.answer("Введите цену в <b>Telegram Stars</b> (целое число):", parse_mode="HTML")

    @dp.message(AdminStates.GB_PACK_PRICE_STARS)
    async def process_gb_pack_stars(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await state.clear()
            return
        try:
            stars = int((message.text or "").strip())
        except ValueError:
            await message.answer("❌ Введите целое число")
            return
        if stars < 0:
            await message.answer("❌ Не может быть отрицательной")
            return
        data = await state.get_data()
        title = data.get("gb_pack_title")
        gb = data.get("gb_pack_gb")
        rub = data.get("gb_pack_rub")
        async with get_connection() as conn:
            next_ord = await conn.fetchval("SELECT COALESCE(MAX(display_order), 0) + 10 FROM gb_pack_products")
            await conn.execute(
                """
                INSERT INTO gb_pack_products (title, gb_amount, price_rub, price_stars, is_active, display_order, updated_at)
                VALUES ($1, $2, $3, $4, TRUE, $5, CURRENT_TIMESTAMP)
                """,
                title,
                gb,
                rub,
                stars,
                next_ord or 100,
            )
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ К трафику", callback_data="admin_traffic"))
        await message.answer("✅ Пакет добавлен", reply_markup=builder.as_markup())
        await state.clear()

    @dp.callback_query(F.data.startswith("admin_gb_pack_toggle:"))
    async def handle_admin_gb_pack_toggle(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        try:
            pid = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Ошибка id", show_alert=True)
            return
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE gb_pack_products
                SET is_active = NOT COALESCE(is_active, TRUE), updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                pid,
            )
        await state.clear()
        text, builder = await _admin_traffic_panel_builder()
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback, "Статус обновлён")

    @dp.callback_query(F.data.startswith("admin_gb_pack_manage:"))
    async def handle_admin_gb_pack_manage(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        try:
            pid = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Ошибка id", show_alert=True)
            return
        await state.clear()
        async with get_connection() as conn:
            p = await conn.fetchrow(
                """
                SELECT id, title, gb_amount, price_rub, price_stars, is_active
                FROM gb_pack_products WHERE id = $1
                """,
                pid,
            )
        if not p:
            await safe_callback_answer(callback, "Пакет не найден", show_alert=True)
            return
        st = "✅ активен" if p["is_active"] else "⏸ выключен"
        body = (
            f"✏️ <b>Пакет #{p['id']}</b> ({st})\n\n"
            f"• Название: <b>{html_std.escape(str(p['title']))}</b>\n"
            f"• Объём: <b>+{p['gb_amount']} ГБ</b>\n"
            f"• Цена (коп.): <b>{p['price_rub']}</b> ({p['price_rub'] // 100} ₽)\n"
            f"• Stars: <b>{p['price_stars']}</b>\n\n"
            "Выберите, что изменить:"
        )
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="📝 Название", callback_data=f"admin_gb_pack_field:{pid}:title"))
        b.row(InlineKeyboardButton(text="📦 Объём (ГБ)", callback_data=f"admin_gb_pack_field:{pid}:gb"))
        b.row(InlineKeyboardButton(text="₽ Цена (коп.)", callback_data=f"admin_gb_pack_field:{pid}:rub"))
        b.row(InlineKeyboardButton(text="⭐ Stars", callback_data=f"admin_gb_pack_field:{pid}:stars"))
        b.row(InlineKeyboardButton(text="🗑 Удалить пакет", callback_data=f"admin_gb_pack_askdel:{pid}"))
        b.row(InlineKeyboardButton(text="◀️ К списку пакетов", callback_data="admin_traffic"))
        await callback.message.edit_text(body, reply_markup=b.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_gb_pack_field:"))
    async def handle_admin_gb_pack_field(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await safe_callback_answer(callback, "Ошибка данных", show_alert=True)
            return
        try:
            pid = int(parts[1])
        except ValueError:
            await safe_callback_answer(callback, "Ошибка id", show_alert=True)
            return
        field = parts[2]
        prompts = {
            "title": "Введите новое <b>название</b> пакета (от 2 символов):",
            "gb": "Введите новый объём в <b>ГБ</b> (целое число, 1–100000):",
            "rub": "Введите цену в <b>копейках</b> (целое число ≥ 0, например 9900 = 99₽):",
            "stars": "Введите цену в <b>Telegram Stars</b> (целое число ≥ 0):",
        }
        if field not in prompts:
            await safe_callback_answer(callback, "Неизвестное поле", show_alert=True)
            return
        await state.set_state(AdminStates.GB_PACK_EDIT_FIELD)
        await state.update_data(gb_pack_edit_id=pid, gb_pack_edit_field=field)
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data=f"admin_gb_pack_manage:{pid}"))
        await callback.message.edit_text(
            f"📶 <b>Редактирование пакета #{pid}</b>\n\n{prompts[field]}",
            reply_markup=b.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.message(AdminStates.GB_PACK_EDIT_FIELD)
    async def process_gb_pack_edit_field(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id, config):
            await state.clear()
            return
        data = await state.get_data()
        pid = data.get("gb_pack_edit_id")
        field = data.get("gb_pack_edit_field")
        if pid is None or field not in ("title", "gb", "rub", "stars"):
            await message.answer("❌ Сессия устарела. Откройте пакет снова из админки.")
            await state.clear()
            return
        raw = (message.text or "").strip()
        try:
            async with get_connection() as conn:
                if field == "title":
                    if len(raw) < 2:
                        await message.answer("❌ Слишком короткое название")
                        return
                    await conn.execute(
                        """
                        UPDATE gb_pack_products
                        SET title = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2
                        """,
                        raw,
                        int(pid),
                    )
                elif field == "gb":
                    gb = int(raw)
                    if gb < 1 or gb > 100000:
                        await message.answer("❌ От 1 до 100000")
                        return
                    await conn.execute(
                        """
                        UPDATE gb_pack_products
                        SET gb_amount = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2
                        """,
                        gb,
                        int(pid),
                    )
                elif field == "rub":
                    rub = int(raw)
                    if rub < 0:
                        await message.answer("❌ Не может быть отрицательной")
                        return
                    await conn.execute(
                        """
                        UPDATE gb_pack_products
                        SET price_rub = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2
                        """,
                        rub,
                        int(pid),
                    )
                elif field == "stars":
                    stars = int(raw)
                    if stars < 0:
                        await message.answer("❌ Не может быть отрицательной")
                        return
                    await conn.execute(
                        """
                        UPDATE gb_pack_products
                        SET price_stars = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2
                        """,
                        stars,
                        int(pid),
                    )
        except ValueError:
            await message.answer("❌ Введите целое число")
            return
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="◀️ К пакету", callback_data=f"admin_gb_pack_manage:{pid}"))
        kb.row(InlineKeyboardButton(text="📶 К списку трафика", callback_data="admin_traffic"))
        await message.answer("✅ Сохранено", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("admin_gb_pack_askdel:"))
    async def handle_admin_gb_pack_askdel(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        try:
            pid = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Ошибка id", show_alert=True)
            return
        await state.clear()
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_gb_pack_del:{pid}"))
        b.row(InlineKeyboardButton(text="◀️ Нет, назад", callback_data=f"admin_gb_pack_manage:{pid}"))
        await callback.message.edit_text(
            f"🗑 Удалить пакет <b>#{pid}</b> из базы?\nЭто действие необратимо.",
            reply_markup=b.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("admin_gb_pack_del:"))
    async def handle_admin_gb_pack_del(callback: CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        try:
            pid = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Ошибка id", show_alert=True)
            return
        await state.clear()
        async with get_connection() as conn:
            await conn.execute("DELETE FROM gb_pack_products WHERE id = $1", pid)
        text, builder = await _admin_traffic_panel_builder()
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await safe_callback_answer(callback, "Пакет удалён")
    
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
    
    # Функция для отправки напоминаний
    async def send_manual_subscription_reminder(end_day_offset: int, time_before_hours: float):
        """
        Отправляет напоминание о скором окончании подписки вручную
        
        Args:
            end_day_offset: Смещение дня окончания (0 = сегодня, 1 = завтра, 3 = через 3 дня, 5 = через 5 дней)
            time_before_hours: За сколько часов до окончания отправлять (5 дней = 120, 3 дня = 72, 1 день = 24, 3 часа = 3)
        """
        logger.info(f"Starting manual subscription reminders: end_day_offset={end_day_offset}, time_before_hours={time_before_hours}")
        
        try:
            async with get_connection() as conn:
                # Вычисляем целевую дату окончания
                target_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=end_day_offset)
                target_date_only = target_date.date()
                
                # Получаем пользователей, у которых подписка заканчивается в выбранный день
                users_to_remind = await conn.fetch('''
                    SELECT user_id, username, first_name, subscription_end
                    FROM users
                    WHERE pay_subscribed = TRUE
                      AND subscription_end IS NOT NULL
                      AND DATE(subscription_end) = $1
                ''', target_date_only)
                
                if not users_to_remind:
                    return 0, f"Не найдено пользователей с подписками, заканчивающимися {target_date.strftime('%d.%m.%Y')}"
                
                sent_count = 0
                error_count = 0
                
                for user in users_to_remind:
                    user_id = user['user_id']
                    subscription_end = user['subscription_end']
                    try:
                        # Форматируем дату окончания
                        try:
                            if isinstance(subscription_end, str):
                                end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                            else:
                                end_date = subscription_end
                            end_date_str = end_date.strftime("%d.%m.%Y")
                            days_remaining = (end_date - datetime.now()).days
                            if days_remaining < 1:
                                days_display = "СЕГОДНЯ"
                            else:
                                days_display = f"{days_remaining} {('день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней')}"
                        except:
                            end_date_str = str(subscription_end)
                            days_display = "?"
                        
                        # Формируем кнопки для продления подписки
                        from ..plans import get_user_tariffs
                        current_tariffs, _, _ = await get_user_tariffs(user_id)
                        
                        builder = InlineKeyboardBuilder()
                        for plan_id, plan_data in current_tariffs.items():
                            builder.button(
                                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                                callback_data=f"plan:{plan_id}"
                            )
                        builder.adjust(1)
                        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
                        
                        # Формируем текст
                        if days_remaining < 1:
                            days_text = "<b>СЕГОДНЯ</b>"
                        else:
                            days_text = f"<b>через {days_display}</b>"
                        
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                "⏰ <b>Напоминание о подписке</b>\n\n"
                                f"Ваша VPN подписка истекает {days_text} ({end_date_str})\n\n"
                                "🔥 <b>Сейчас действует скидка!</b>\n"
                                "Успей продлить подписку сейчас и получи выгодную цену.\n\n"
                                "Не упусти возможность продолжить пользоваться VPN по специальной цене! 🎁"
                            ),
                            reply_markup=builder.as_markup(),
                            parse_mode="HTML"
                        )
                        
                        sent_count += 1
                        logger.info(f"Sent manual subscription reminder to user {user_id}")
                        
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Failed to send reminder to user {user_id}: {e}")
                
                return sent_count, f"Отправлено уведомлений: {sent_count}, ошибок: {error_count}"
        
        except Exception as e:
            logger.error(f"Error in send_manual_subscription_reminder: {e}", exc_info=True)
            return 0, f"Ошибка: {str(e)}"
    
    # Обработчик рассылки
    @dp.callback_query(F.data == "admin_broadcast")
    async def handle_admin_broadcast(callback: CallbackQuery, state: FSMContext):
        """Начать рассылку - конструктор"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await state.update_data(
            broadcast_text='',
            media_type=None,
            media_file_id=None,
            broadcast_buttons=[],
            broadcast_filter='all',
            broadcast_test=False
        )
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        
        await callback.message.edit_text(
            status_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "broadcast_add_text")
    async def handle_broadcast_add_text(callback: CallbackQuery, state: FSMContext):
        """Добавление текста к рассылке"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "📝 <b>Добавление текста</b>\n\n"
            "Введите текст сообщения:\n\n"
            "💡 <i>Если вы также добавите медиа, этот текст будет использован как подпись (caption)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.BROADCAST_MESSAGE)
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "broadcast_add_media")
    async def handle_broadcast_add_media(callback: CallbackQuery, state: FSMContext):
        """Добавление медиа к рассылке"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "🖼️ <b>Добавление медиа</b>\n\n"
            "Отправьте фото, видео, документ или GIF:\n\n"
            "💡 <i>Можно добавить подпись (caption) к медиа, если текст не был добавлен отдельно</i>",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.BROADCAST_MEDIA)
        await safe_callback_answer(callback)

    @dp.message(AdminStates.BROADCAST_MESSAGE)
    async def process_broadcast_message(message: Message, state: FSMContext):
        """Обработка текста рассылки"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        broadcast_text = message.text or ""
        if not broadcast_text.strip():
            await message.answer("❌ Сообщение не может быть пустым")
            return
        
        await state.update_data(broadcast_text=broadcast_text)
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        
        await message.answer(
            "✅ <b>Текст добавлен!</b>\n\n" + status_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    @dp.message(AdminStates.BROADCAST_MEDIA)
    async def process_broadcast_media(message: Message, state: FSMContext):
        """Обработка медиа для рассылки"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        media_type = None
        file_id = None
        caption = message.caption or ""
        
        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
        elif message.animation:
            media_type = "animation"
            file_id = message.animation.file_id
        else:
            await message.answer("❌ Пожалуйста, отправьте фото, видео, документ или GIF")
            return
        
        data = await state.get_data()
        existing_text = data.get('broadcast_text', '')
        if not existing_text and caption:
            await state.update_data(broadcast_text=caption)
        
        await state.update_data(
            media_type=media_type,
            media_file_id=file_id
        )
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        media_preview = "🖼️ Фото" if media_type == "photo" else "🎥 Видео" if media_type == "video" else "📄 Документ" if media_type == "document" else "🎬 GIF"
        
        await message.answer(
            f"✅ <b>{media_preview} добавлено!</b>\n\n" + status_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "broadcast_add_buttons")
    async def handle_broadcast_add_buttons(callback: CallbackQuery, state: FSMContext):
        """Добавление кнопок к рассылке"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        data = await state.get_data()
        existing_buttons = data.get('broadcast_buttons', [])
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="broadcast_add_menu_button:get_vpn"))
        builder.row(InlineKeyboardButton(text="🎁 Подарок", callback_data="broadcast_add_menu_button:referral"))
        builder.row(InlineKeyboardButton(text="💎 Подписка", callback_data="broadcast_add_menu_button:premium"))
        builder.row(InlineKeyboardButton(text="🆘 Помощь", callback_data="broadcast_add_menu_button:help"))
        builder.row(InlineKeyboardButton(text="🆓 Пробный период", callback_data="broadcast_add_menu_button:trial"))
        builder.row(InlineKeyboardButton(text="➕ Добавить свою кнопку", callback_data="broadcast_add_custom_button"))
        if existing_buttons:
            builder.row(InlineKeyboardButton(text="✅ Готово", callback_data="broadcast_buttons_done"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_broadcast"))
        
        buttons_list = ""
        if existing_buttons:
            buttons_list = "\n\n<b>Текущие кнопки:</b>\n" + "\n".join([f"• {btn['text']}" for btn in existing_buttons])
        
        text = (
            "🔘 <b>Добавление кнопок</b>\n\n"
            "Выберите кнопку из главного меню или добавьте свою:"
            f"{buttons_list}"
        )
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("broadcast_add_menu_button:"))
    async def handle_broadcast_add_menu_button(callback: CallbackQuery, state: FSMContext):
        """Добавление кнопки главного меню в рассылку"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        button_type = callback.data.split(":")[1]
        data = await state.get_data()
        existing_buttons = data.get('broadcast_buttons', [])
        
        menu_buttons_map = {
            "get_vpn": "🔗 Получить VPN",
            "referral": "🎁 Подарок",
            "premium": "💎 Подписка",
            "help": "🆘 Помощь",
            "trial": "🆓 Пробный период"
        }
        
        button_text = menu_buttons_map.get(button_type)
        if not button_text:
            await safe_callback_answer(callback, "❌ Неизвестный тип кнопки", show_alert=True)
            return
        
        for btn in existing_buttons:
            if btn.get('callback_data') == f"menu:{button_type}":
                await safe_callback_answer(callback, "⚠️ Эта кнопка уже добавлена", show_alert=True)
                return
        
        new_button = {
            'text': button_text,
            'callback_data': f"menu:{button_type}"
        }
        existing_buttons.append(new_button)
        await state.update_data(broadcast_buttons=existing_buttons)
        await safe_callback_answer(callback, f"✅ Кнопка '{button_text}' добавлена", show_alert=True)
        await handle_broadcast_add_buttons(callback, state)

    @dp.callback_query(F.data == "broadcast_add_custom_button")
    async def handle_broadcast_add_custom_button(callback: CallbackQuery, state: FSMContext):
        """Добавление своей кнопки в рассылку"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        data = await state.get_data()
        existing_buttons = data.get('broadcast_buttons', [])
        
        if existing_buttons:
            buttons_list = "\n".join([f"• {btn['text']} → {btn.get('url', btn.get('callback_data', ''))}" for btn in existing_buttons])
            text = (
                "🔘 <b>Добавление своей кнопки</b>\n\n"
                f"<b>Текущие кнопки ({len(existing_buttons)}):</b>\n{buttons_list}\n\n"
                "Отправьте кнопку в формате:\n"
                "<code>Текст кнопки | URL</code>\n\n"
                "Пример:\n"
                "<code>Открыть сайт | https://example.com</code>\n"
                "Когда закончите, отправьте /done"
            )
        else:
            text = (
                "🔘 <b>Добавление своей кнопки</b>\n\n"
                "Отправьте кнопку в формате:\n"
                "<code>Текст кнопки | URL</code>\n\n"
                "Пример:\n"
                "<code>Открыть сайт | https://example.com</code>\n"
                "Когда закончите, отправьте /done"
            )
        
        await callback.message.edit_text(text, parse_mode="HTML")
        await state.set_state(AdminStates.BROADCAST_BUTTONS)
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "broadcast_buttons_done")
    async def handle_broadcast_buttons_done(callback: CallbackQuery, state: FSMContext):
        """Завершение добавления кнопок"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        await callback.message.edit_text(
            "✅ <b>Кнопки настроены!</b>\n\n" + status_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)

    @dp.message(AdminStates.BROADCAST_BUTTONS)
    async def process_broadcast_buttons(message: Message, state: FSMContext):
        """Обработка кнопок для рассылки"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        if message.text and message.text.strip().lower() == "/done":
            data = await state.get_data()
            status_text, keyboard = await get_broadcast_constructor_menu(data)
            buttons = data.get('broadcast_buttons', [])
            if buttons:
                await message.answer("✅ <b>Кнопки добавлены!</b>\n\n" + status_text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.answer("⏭️ <b>Кнопки не добавлены</b>\n\n" + status_text, reply_markup=keyboard, parse_mode="HTML")
            return
        
        text = message.text or ""
        buttons = []
        
        for line in text.split('\n'):
            line = line.strip()
            if '|' in line:
                parts = line.split('|', 1)
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                if btn_text and btn_url and (btn_url.startswith('http://') or btn_url.startswith('https://')):
                    buttons.append({'text': btn_text, 'url': btn_url})
        
        if not buttons:
            await message.answer("❌ Неверный формат. Используйте: <code>Текст кнопки | URL</code>", parse_mode="HTML")
            return
        
        data = await state.get_data()
        existing_buttons = data.get('broadcast_buttons', [])
        existing_buttons.extend(buttons)
        await state.update_data(broadcast_buttons=existing_buttons)
        
        buttons_list = "\n".join([f"• {btn['text']} → {btn['url']}" for btn in existing_buttons])
        await message.answer(
            f"✅ <b>Кнопки добавлены ({len(existing_buttons)}):</b>\n\n{buttons_list}\n\n"
            f"Отправьте ещё кнопки или /done для возврата в конструктор",
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "broadcast_choose_filter")
    async def handle_broadcast_choose_filter(callback: CallbackQuery, state: FSMContext):
        """Выбор фильтра для рассылки"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="👥 Все пользователи", callback_data="broadcast_filter:all"))
        builder.row(InlineKeyboardButton(text="✅ С активной подпиской", callback_data="broadcast_filter:active"))
        builder.row(InlineKeyboardButton(text="❌ Без подписки", callback_data="broadcast_filter:inactive"))
        builder.row(InlineKeyboardButton(text="📅 Активные за 7 дней", callback_data="broadcast_filter:active_7d"))
        builder.row(InlineKeyboardButton(text="📅 Активные за 30 дней", callback_data="broadcast_filter:active_30d"))
        builder.row(InlineKeyboardButton(text="🎁 С рефералами", callback_data="broadcast_filter:with_referrals"))
        builder.row(InlineKeyboardButton(text="🆓 Использовали пробный период", callback_data="broadcast_filter:trial_used"))
        builder.row(InlineKeyboardButton(text="🧪 Не использовали пробный период", callback_data="broadcast_filter:trial_not_used"))
        builder.row(InlineKeyboardButton(text="◀️ Назад в конструктор", callback_data="admin_broadcast"))
        
        await callback.message.edit_text("🔍 <b>Выберите фильтр для рассылки:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "broadcast_toggle_test")
    async def handle_broadcast_toggle_test(callback: CallbackQuery, state: FSMContext):
        """Переключение тестового режима"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        data = await state.get_data()
        current_test = data.get('broadcast_test', False)
        await state.update_data(broadcast_test=not current_test)
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        await callback.message.edit_text(status_text, reply_markup=keyboard, parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("broadcast_filter:"))
    async def handle_broadcast_filter(callback: CallbackQuery, state: FSMContext):
        """Обработка выбора фильтра"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        filter_type = callback.data.split(":")[1]
        await state.update_data(broadcast_filter=filter_type)
        
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        await callback.message.edit_text(status_text, reply_markup=keyboard, parse_mode="HTML")
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "broadcast_confirm")
    async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
        """Подтверждение и отправка рассылки"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        data = await state.get_data()
        broadcast_text = data.get('broadcast_text', '')
        media_type = data.get('media_type')
        media_file_id = data.get('media_file_id')
        buttons = data.get('broadcast_buttons', [])
        filter_type = data.get('broadcast_filter', 'all')
        test_mode = data.get('broadcast_test', False)
        
        if not broadcast_text and not media_type:
            await safe_callback_answer(callback, "❌ Добавьте текст или медиа", show_alert=True)
            return
        
        # Формируем клавиатуру с кнопками
        reply_markup = None
        if buttons:
            builder = InlineKeyboardBuilder()
            for btn in buttons:
                if 'url' in btn:
                    builder.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
                elif 'callback_data' in btn:
                    callback_data = btn['callback_data']
                    if callback_data.startswith('menu:'):
                        menu_type = callback_data.split(':')[1]
                        if menu_type == 'get_vpn':
                            builder.row(InlineKeyboardButton(text=btn['text'], callback_data='get_vpn_link'))
                        elif menu_type == 'referral':
                            builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_invite'))
                        elif menu_type == 'premium':
                            builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_premium'))
                        elif menu_type == 'help':
                            builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_help'))
                        elif menu_type == 'trial':
                            builder.row(InlineKeyboardButton(text=btn['text'], callback_data='activate_trial'))
            reply_markup = builder.as_markup()

        if test_mode:
            users = [{'user_id': callback.from_user.id}]
            await callback.message.answer("🧪 <b>ТЕСТОВЫЙ РЕЖИМ:</b> Рассылка будет отправлена только вам", parse_mode="HTML")
        else:
            async with get_connection() as conn:
                if filter_type == "all":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE')
                elif filter_type == "active":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE AND pay_subscribed = TRUE AND subscription_end >= CURRENT_DATE')
                elif filter_type == "inactive":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE AND (pay_subscribed = FALSE OR subscription_end < CURRENT_DATE OR subscription_end IS NULL)')
                elif filter_type == "active_7d":
                    users = await conn.fetch("SELECT user_id FROM users WHERE blacklisted = FALSE AND last_activity >= CURRENT_DATE - INTERVAL '7 days'")
                elif filter_type == "active_30d":
                    users = await conn.fetch("SELECT user_id FROM users WHERE blacklisted = FALSE AND last_activity >= CURRENT_DATE - INTERVAL '30 days'")
                elif filter_type == "with_referrals":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE AND referral_count > 0')
                elif filter_type == "trial_used":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE AND trial_used = TRUE')
                elif filter_type == "trial_not_used":
                    users = await conn.fetch('SELECT user_id FROM users WHERE blacklisted = FALSE AND (trial_used = FALSE OR trial_used IS NULL) AND (pay_subscribed = FALSE OR subscription_end < CURRENT_DATE OR subscription_end IS NULL)')
                else:
                    users = []

        sent = 0
        failed = 0
        failed_user_ids = []
        test_text = " (тестовый режим)" if test_mode else ""
        await callback.message.edit_text(f"📢 Рассылка начата{test_text}... Отправлено: {sent}, Ошибок: {failed}")
        
        has_trial_button = any(btn.get('callback_data') == 'menu:trial' for btn in buttons)

        async def send_with_retry(target_user_id: int, target_markup):
            max_attempts = 4
            base_delay = 0.2
            for attempt in range(1, max_attempts + 1):
                try:
                    if media_type and media_file_id:
                        if media_type == "photo":
                            await bot.send_photo(target_user_id, photo=media_file_id, caption=broadcast_text or None, reply_markup=target_markup, parse_mode="HTML")
                        elif media_type == "video":
                            await bot.send_video(target_user_id, video=media_file_id, caption=broadcast_text or None, reply_markup=target_markup, parse_mode="HTML")
                        elif media_type == "document":
                            await bot.send_document(target_user_id, document=media_file_id, caption=broadcast_text or None, reply_markup=target_markup, parse_mode="HTML")
                        elif media_type == "animation":
                            await bot.send_animation(target_user_id, animation=media_file_id, caption=broadcast_text or None, reply_markup=target_markup, parse_mode="HTML")
                    else:
                        await bot.send_message(target_user_id, broadcast_text, reply_markup=target_markup, parse_mode="HTML")
                    return True
                except TelegramRetryAfter as e:
                    # Telegram flood control: wait required time and retry.
                    retry_after = max(float(getattr(e, "retry_after", 1.0)), 0.5)
                    logger.warning(f"Broadcast flood limit for {target_user_id}, retry in {retry_after:.2f}s (attempt {attempt}/{max_attempts})")
                    await asyncio.sleep(retry_after + 0.1)
                except TelegramBadRequest as e:
                    logger.error(f"Broadcast bad request for {target_user_id}: {e}")
                    return False
                except Exception as e:
                    if attempt >= max_attempts:
                        logger.error(f"Failed to send broadcast to {target_user_id} after {max_attempts} attempts: {e}")
                        return False
                    await asyncio.sleep(base_delay * attempt)
            return False
        
        for user_row in users:
            user_id = user_row['user_id']
            user_reply_markup = reply_markup
            
            if has_trial_button:
                async with get_connection() as conn:
                    user_trial = await conn.fetchrow('SELECT trial_used FROM users WHERE user_id = $1', user_id)
                    trial_used = user_trial.get('trial_used', False) if user_trial else False
                    if trial_used:
                        builder = InlineKeyboardBuilder()
                        for btn in buttons:
                            if btn.get('callback_data') != 'menu:trial':
                                if 'url' in btn:
                                    builder.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
                                elif 'callback_data' in btn:
                                    cd = btn['callback_data']
                                    if cd.startswith('menu:'):
                                        mt = cd.split(':')[1]
                                        if mt == 'get_vpn': builder.row(InlineKeyboardButton(text=btn['text'], callback_data='get_vpn_link'))
                                        elif mt == 'referral': builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_invite'))
                                        elif mt == 'premium': builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_premium'))
                                        elif mt == 'help': builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_help'))
                        user_reply_markup = builder.as_markup() if builder.buttons else None

            try:
                ok = await send_with_retry(user_id, user_reply_markup)
                if not ok:
                    failed += 1
                    failed_user_ids.append(str(user_id))
                    continue
                sent += 1
                if sent % 10 == 0:
                    await callback.message.edit_text(f"📢 Рассылка... Отправлено: {sent}, Ошибок: {failed}")
                await asyncio.sleep(0.12)
            except Exception as e:
                failed += 1
                failed_user_ids.append(str(user_id))
                logger.error(f"Failed to send broadcast to {user_id}: {e}")
        
        if failed_user_ids:
            preview_failed = ", ".join(failed_user_ids[:40])
            suffix = "" if len(failed_user_ids) <= 40 else f" ... (+{len(failed_user_ids) - 40} ещё)"
            await callback.message.edit_text(
                f"✅ <b>Рассылка завершена{test_text}</b>\n\n"
                f"Отправлено: <i>{sent}</i>\n"
                f"Ошибок: <i>{failed}</i>\n\n"
                f"<b>Не доставлено user_id:</b>\n<code>{preview_failed}{suffix}</code>",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"✅ <b>Рассылка завершена{test_text}</b>\n\n"
                f"Отправлено: <i>{sent}</i>\nОшибок: <i>{failed}</i>",
                parse_mode="HTML"
            )
        await state.clear()
        await safe_callback_answer(callback)

    
    # Обработчики ручной отправки напоминаний
    @dp.callback_query(F.data == "admin_manual_reminder")
    async def handle_admin_manual_reminder(callback: CallbackQuery, state: FSMContext):
        """Обработчик кнопки ручной отправки напоминаний"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📅 Сегодня", callback_data="reminder_end_day:0"))
        builder.row(InlineKeyboardButton(text="📅 Завтра (+1 день)", callback_data="reminder_end_day:1"))
        builder.row(InlineKeyboardButton(text="📅 Через 3 дня", callback_data="reminder_end_day:3"))
        builder.row(InlineKeyboardButton(text="📅 Через 5 дней", callback_data="reminder_end_day:5"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            "⏰ <b>Ручная отправка напоминаний о подписке</b>\n\n"
            "Выберите день окончания подписки:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("reminder_end_day:"))
    async def handle_reminder_end_day(callback: CallbackQuery, state: FSMContext):
        """Обработчик выбора дня окончания подписки"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        end_day_offset = int(callback.data.split(":")[1])
        await state.update_data(reminder_end_day=end_day_offset)
        
        target_date = datetime.now() + timedelta(days=end_day_offset)
        date_str = target_date.strftime('%d.%m.%Y')
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="⏰ За 5 дней до окончания", callback_data="reminder_time:120"))
        builder.row(InlineKeyboardButton(text="⏰ За 3 дня до окончания", callback_data="reminder_time:72"))
        builder.row(InlineKeyboardButton(text="⏰ За 1 день до окончания", callback_data="reminder_time:24"))
        builder.row(InlineKeyboardButton(text="⏰ За 3 часа до окончания", callback_data="reminder_time:3"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_manual_reminder"))
        
        day_names = {0: "Сегодня", 1: "Завтра", 3: "Через 3 дня", 5: "Через 5 дней"}
        day_name = day_names.get(end_day_offset, f"Через {end_day_offset} дней")
        
        await callback.message.edit_text(
            f"⏰ <b>Ручная отправка напоминаний</b>\n\n"
            f"📅 День окончания: <b>{day_name}</b> ({date_str})\n\n"
            f"Выберите период до окончания подписки:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("reminder_time:"))
    async def handle_reminder_time(callback: CallbackQuery, state: FSMContext):
        """Обработчик выбора времени до окончания и отправка напоминаний"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        time_before_hours = float(callback.data.split(":")[1])
        data = await state.get_data()
        end_day_offset = data.get('reminder_end_day', 0)
        
        await callback.message.edit_text(
            "⏰ <b>Отправка напоминаний...</b>\n\n"
            "Пожалуйста, подождите...",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
        
        sent_count, result_message = await send_manual_subscription_reminder(end_day_offset, time_before_hours)
        
        target_date = datetime.now() + timedelta(days=end_day_offset)
        day_names = {0: "Сегодня", 1: "Завтра", 3: "Через 3 дня", 5: "Через 5 дней"}
        day_name = day_names.get(end_day_offset, f"Через {end_day_offset} дней")
        
        time_names = {120: "5 дней", 72: "3 дня", 24: "1 день", 3: "3 часа"}
        time_name = time_names.get(int(time_before_hours), f"{time_before_hours} часов")
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад в админ панель", callback_data="admin_back"))
        
        await callback.message.edit_text(
            f"✅ <b>Отправка завершена</b>\n\n"
            f"📅 День окончания: {day_name} ({target_date.strftime('%d.%m.%Y')})\n"
            f"⏰ Период до окончания: {time_name}\n\n"
            f"{result_message}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    # Команды управления серверами
    @dp.message(Command("add_server"))
    async def cmd_add_server(message: Message, state: FSMContext):
        """Команда для добавления нового сервера"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ У вас нет доступа к этой команде.")
            return
        
        await message.answer(
            "🔧 <b>Добавление нового сервера</b>\n\n"
            "Введите название сервера (будет видно пользователям):",
            parse_mode="HTML"
        )
        await state.set_state(AddServerSteps.WAITING_NAME)
    
    @dp.message(AddServerSteps.WAITING_NAME)
    async def process_server_name_cmd(message: Message, state: FSMContext):
        """Обработка названия сервера"""
        await state.update_data(name=message.text)
        await message.answer(
            "🔗 Введите полную ссылку на панель 3x-ui:\n\n"
            "Примеры:\n"
            "• <code>http://79.137.204.85:8080/</code>\n"
            "• <code>https://example.com:54321/</code>\n\n"
            "⚠️ Важно: Укажите полную ссылку, включая протокол (http:// или https://), "
            "адрес, порт и путь (если есть).",
            parse_mode="HTML"
        )
        await state.set_state(AddServerSteps.WAITING_PANEL_URL)
    
    @dp.message(AddServerSteps.WAITING_PANEL_URL)
    async def process_server_panel_url_cmd(message: Message, state: FSMContext):
        """Обработка ссылки на панель"""
        from urllib.parse import urlparse
        
        panel_url = message.text.strip()
        if not panel_url.endswith('/'):
            panel_url += '/'
        
        try:
            parsed = urlparse(panel_url)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("Неверный формат URL")
            
            protocol = parsed.scheme.lower()
            if protocol not in ['http', 'https']:
                await message.answer("❌ Поддерживаются только протоколы HTTP и HTTPS. Попробуйте снова:")
                return
            
            netloc = parsed.netloc
            if ':' in netloc:
                host, port_str = netloc.rsplit(':', 1)
                try:
                    port = int(port_str)
                except ValueError:
                    await message.answer("❌ Неверный формат порта. Попробуйте снова:")
                    return
            else:
                host = netloc
                port = 443 if protocol == 'https' else 80
            
            path = parsed.path
            base_url = f"{protocol}://{host}:{port}{path}".rstrip('/')
            ip_or_domain = host
            
            await state.update_data(
                ip=ip_or_domain,
                port=port,
                protocol=protocol,
                base_url=base_url,
                panel_url=panel_url
            )
            
            await message.answer(
                f"✅ URL успешно распознан!\n\n"
                f"<b>Данные:</b>\n"
                f"Протокол: <i>{protocol.upper()}</i>\n"
                f"Адрес: <i>{ip_or_domain}</i>\n"
                f"Порт: <i>{port}</i>\n"
                f"Base URL: <i>{base_url}</i>\n\n"
                f"Введите username для панели 3x-ui:",
                parse_mode="HTML"
            )
            await state.set_state(AddServerSteps.WAITING_USERNAME)
        except Exception as e:
            await message.answer(
                f"❌ <b>Ошибка парсинга URL:</b>\n<code>{str(e)}</code>\n\n"
                f"Пожалуйста, введите полную ссылку в формате:\n"
                f"<code>http://IP:ПОРТ/ПУТЬ/</code>",
                parse_mode="HTML"
            )
    
    @dp.message(AddServerSteps.WAITING_USERNAME)
    async def process_server_username_cmd(message: Message, state: FSMContext):
        """Обработка username"""
        await state.update_data(username=message.text)
        await message.answer("Введите password для панели 3x-ui:")
        await state.set_state(AddServerSteps.WAITING_PASSWORD)
    
    @dp.message(AddServerSteps.WAITING_PASSWORD)
    async def process_server_password_cmd(message: Message, state: FSMContext):
        """Обработка password"""
        await state.update_data(password=message.text)
        await message.answer("Введите Inbound ID (число):")
        await state.set_state(AddServerSteps.WAITING_INBOUND_ID)
    
    @dp.message(AddServerSteps.WAITING_INBOUND_ID)
    async def process_server_inbound_id_cmd(message: Message, state: FSMContext):
        """Обработка Inbound ID"""
        try:
            inbound_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Inbound ID должен быть числом. Попробуйте снова:")
            return

        await state.update_data(inbound_id=inbound_id)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔝 В начало", callback_data="order:start")
        builder.button(text="🔙 В конец", callback_data="order:end")
        builder.button(text="🔢 По числу (приоритет)", callback_data="order:number")
        builder.adjust(1)
        
        await message.answer(
            "📍 <b>Выберите порядок отображения сервера:</b>\n\n"
            "Это определит, на каком месте будет сервер в списке подписки пользователя.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AddServerSteps.WAITING_ORDER)
    
    @dp.callback_query(AddServerSteps.WAITING_ORDER, F.data.startswith("order:"))
    async def process_server_order_choice(callback: CallbackQuery, state: FSMContext):
        """Выбор типа порядка"""
        choice = callback.data.split(":")[1]
        
        if choice == "number":
            await callback.message.edit_text("Введите число (чем меньше число, тем выше сервер в списке):")
            # Мы остаемся в том же состоянии, но теперь ждем именно число в сообщении
            await callback.answer()
            return
            
        # Для "start" и "end" сразу вычисляем и сохраняем
        display_order = 100
        async with get_connection() as conn:
            if choice == "start":
                min_order = await conn.fetchval("SELECT MIN(display_order) FROM servers")
                display_order = (min_order or 100) - 1
            elif choice == "end":
                max_order = await conn.fetchval("SELECT MAX(display_order) FROM servers")
                display_order = (max_order or 100) + 1
        
        await state.update_data(display_order=display_order)
        await callback.message.edit_text(f"✅ Установлен порядок: {display_order}.")
        await ask_is_system(callback.message, state)
        await callback.answer()

    @dp.message(AddServerSteps.WAITING_ORDER)
    async def process_server_order_number(message: Message, state: FSMContext):
        """Обработка ручного ввода числа порядка"""
        try:
            display_order = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Введите целое число:")
            return
            
        await state.update_data(display_order=display_order)
        await ask_is_system(message, state)

    async def ask_is_system(message: Message, state: FSMContext):
        """Спрашиваем, является ли сервер системным"""
        builder = InlineKeyboardBuilder()
        builder.button(text="🌍 Публичный (Обычный)", callback_data="system:no")
        builder.button(text="⚙️ Системный (Скрытый)", callback_data="system:yes")
        builder.adjust(1)
        
        await message.answer(
            "📍 <b>Тип сервера:</b>\n\n"
            "Системные серверы скрыты в MiniApp, но доступны по подписке.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AddServerSteps.WAITING_SYSTEM)

    @dp.callback_query(AddServerSteps.WAITING_SYSTEM, F.data.startswith("system:"))
    async def process_is_system_choice(callback: CallbackQuery, state: FSMContext):
        """Обработка выбора типа сервера"""
        is_system = (callback.data == "system:yes")
        await state.update_data(is_system=is_system)
        
        await callback.message.edit_text(f"✅ Тип сервера: {'Системный' if is_system else 'Публичный'}. Сохраняю...")
        await save_new_server(callback.message, state)
        await callback.answer()

    async def save_new_server(message: Message, state: FSMContext):
        """Финальное сохранение сервера после всех шагов"""
        data = await state.get_data()
        name = data.get('name')
        ip = data.get('ip')
        port = data.get('port', 54321)
        protocol = data.get('protocol', 'https')
        username = data.get('username')
        password = data.get('password')
        inbound_id = data.get('inbound_id')
        base_url = data.get('base_url')
        display_order = data.get('display_order', 100)
        is_system = data.get('is_system', False)
        
        # Проверяем подключение к серверу перед сохранением если еще не проверяли
        try:
            test_client = XUIClient(
                base_url=base_url,
                username=username,
                password=password,
                inbound_id=inbound_id
            )
            await test_client.login()
            await test_client.close()
        except Exception as e:
            if 'test_client' in locals():
                await test_client.close()
            error_msg = str(e)
            if "SSL" in error_msg or "WRONG_VERSION_NUMBER" in error_msg:
                suggestion = "\n\n💡 <b>Совет:</b> Попробуйте использовать HTTP вместо HTTPS."
            else:
                suggestion = "\n\nПроверьте данные и попробуйте снова."
            await message.answer(
                f"❌ <b>Ошибка подключения к серверу:</b>\n<code>{error_msg}</code>{suggestion}",
                parse_mode="HTML"
            )
            await state.clear()
            return
        
        # Сохраняем сервер в БД
        async with get_connection() as conn:
            await conn.execute('''
                SELECT setval('servers_id_seq', COALESCE((SELECT MAX(id) FROM servers), 0) + 1, false)
            ''')
            server_id = await conn.fetchval('''
                INSERT INTO servers (name, ip, port, protocol, username, password, inbound_id, base_url, is_active, display_order, is_system)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $10)
                RETURNING id
            ''', name, ip, port, protocol, username, password, inbound_id, base_url, display_order, is_system)
            
            
        # Создаём ключи для активных пользователей (теперь через одну задачу, последовательно)
        try:
            from bot.subscriptions import create_keys_for_specific_server
            asyncio.create_task(create_keys_for_specific_server(server_id))
        except Exception as e:
            logger.error(f"Error starting key creation for new server: {e}")

        
        await message.answer(
            f"✅ <b>Сервер успешно добавлен!</b>\n\n"
            f"ID: <i>{server_id}</i>\n"
            f"Название: <i>{name}</i>\n"
            f"IP: <i>{ip}</i>\n\n"
            f"🔑 Ключи для активных пользователей создаются автоматически...",
            parse_mode="HTML"
        )
        await state.clear()
    
    @dp.message(Command("servers"))
    async def cmd_list_servers(message: Message):
        """Список всех серверов"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ У вас нет доступа к этой команде.")
            return
        
        async with get_connection() as conn:
            servers = await conn.fetch('''
                SELECT id, name, ip, is_active, display_order 
                FROM servers 
                ORDER BY display_order, id
            ''')
        
        if not servers:
            await message.answer("📭 Серверы не найдены. Используйте /add_server для добавления.")
            return
        
        text = "🖥️ <b>Список серверов:</b>\n\n"
        for server in servers:
            server_id = server['id']
            name = server['name']
            ip = server['ip']
            is_active = server['is_active']
            display_order = server['display_order']
            is_system = server.get('is_system', False)
            status = "✅ Активен" if is_active else "❌ Неактивен"
            type_label = " (⚙️ СИСТЕМНЫЙ)" if is_system else ""
            text += f"#{server_id} [Порядок: {display_order}] <b>{name}</b> ({ip}){type_label}\n   {status}\n\n"
        
        await message.answer(text, parse_mode="HTML")
    
    @dp.message(Command("toggle_server"))
    async def cmd_toggle_server(message: Message):
        """Переключение статуса сервера"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ У вас нет доступа к этой команде.")
            return
        
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("❌ Использование: /toggle_server <server_id>")
            return
        
        try:
            server_id = int(parts[1])
        except ValueError:
            await message.answer("❌ Server ID должен быть числом")
            return
        
        async with get_connection() as conn:
            server = await conn.fetchrow('SELECT is_active FROM servers WHERE id = $1', server_id)
            if not server:
                await message.answer("❌ Сервер не найден")
                return
            
            new_status = not server['is_active']
            await conn.execute('UPDATE servers SET is_active = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2', new_status, server_id)
            
            # Если сервер переведен на паузу — деактивируем ключи (кроме узла «ТГ безлимит»)
            if not new_status:
                relay_id = await conn.fetchval(
                    """
                    SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1
                    """
                )
                if relay_id is not None and int(relay_id) == int(server_id):
                    logger.info(
                        "Server %s is TG relay — keys stay active in DB while server paused",
                        server_id,
                    )
                else:
                    deactivated_count = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM vpn_keys
                        WHERE server_id = $1 AND is_active = TRUE
                        """,
                        server_id,
                    )

                    if deactivated_count and deactivated_count > 0:
                        await conn.execute(
                            """
                            UPDATE vpn_keys
                            SET is_active = FALSE
                            WHERE server_id = $1 AND is_active = TRUE
                            """,
                            server_id,
                        )
                        logger.info(
                            f"Deactivated {deactivated_count} keys for server {server_id} (server paused)"
                        )
        
        status_text = "активирован" if new_status else "приостановлен"
        await message.answer(f"✅ Сервер {status_text}")
    
    @dp.message(Command("delete_server"))
    async def cmd_delete_server(message: Message):
        """Удаление сервера"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ У вас нет доступа к этой команде.")
            return
        
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("❌ Использование: /delete_server <server_id>")
            return
        
        try:
            server_id = int(parts[1])
        except ValueError:
            await message.answer("❌ Server ID должен быть числом")
            return
        
        async with get_connection() as conn:
            server = await conn.fetchrow('SELECT name FROM servers WHERE id = $1', server_id)
            if not server:
                await message.answer("❌ Сервер не найден")
                return
            
            # Удаляем все ключи этого сервера перед удалением (чтобы не нарушить foreign key constraint)
            deleted_keys_count = await conn.fetchval('''
                SELECT COUNT(*) FROM vpn_keys
                WHERE server_id = $1
            ''', server_id)
            
            if deleted_keys_count and deleted_keys_count > 0:
                await conn.execute('''
                    DELETE FROM vpn_keys
                    WHERE server_id = $1
                ''', server_id)
                logger.info(f"Deleted {deleted_keys_count} keys for server {server_id} before deletion")
            
            # Удаляем сервер
            await conn.execute('DELETE FROM servers WHERE id = $1', server_id)
        
        await message.answer(f"✅ Сервер '{server['name']}' удален")
    
    # ==================== УПРАВЛЕНИЕ БАЛАНСОМ ====================
    
    @dp.callback_query(F.data == "admin_balance")
    async def handle_admin_balance(callback: CallbackQuery, state: FSMContext):
        """Управление балансом"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "💳 <b>Управление балансом</b>\n\n"
            "Введите user_id пользователя (число) или @username:",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.BALANCE_USER_SELECT)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.BALANCE_USER_SELECT)
    async def process_balance_user(message: Message, state: FSMContext):
        """Обработка выбора пользователя для баланса"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        user_input = message.text.strip()
        user_id = None
        
        if user_input.startswith('@'):
            async with get_connection() as conn:
                user_data = await conn.fetchrow('SELECT user_id FROM users WHERE username = $1 OR username = $2', user_input, user_input[1:])
                if user_data:
                    user_id = user_data['user_id']
        else:
            try:
                user_id = int(user_input)
            except ValueError:
                await message.answer("❌ Неверный формат. Введите user_id (число) или @username")
                return
        
        if not user_id:
            await message.answer("❌ Пользователь не найден")
            return
        
        async with get_connection() as conn:
            balance_row = await conn.fetchrow('SELECT balance FROM user_balances WHERE user_id = $1', user_id)
            current_balance = balance_row['balance'] if balance_row else 0
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Пополнить", callback_data=f"balance_add:{user_id}"))
        builder.row(InlineKeyboardButton(text="➖ Списать", callback_data=f"balance_sub:{user_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await message.answer(
            f"💳 <b>Управление балансом</b>\n\n"
            f"Пользователь: <i>{user_id}</i>\n"
            f"Текущий баланс: <i>{current_balance / 100:.2f}₽</i>\n\n"
            f"Выберите действие:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.update_data(target_user_id=user_id)
        await state.set_state(AdminStates.BALANCE_AMOUNT)
    
    @dp.callback_query(F.data.startswith("balance_add:") | F.data.startswith("balance_sub:"))
    async def handle_balance_action(callback: CallbackQuery, state: FSMContext):
        """Обработка действия с балансом"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        parts = callback.data.split(":")
        action = parts[0]
        user_id = int(parts[1])
        
        action_text = "пополнения" if action == "balance_add" else "списания"
        await callback.message.edit_text(f"💳 Введите сумму для {action_text} (в рублях):")
        await state.update_data(target_user_id=user_id, balance_action=action)
        await state.set_state(AdminStates.BALANCE_AMOUNT)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.BALANCE_AMOUNT)
    async def process_balance_amount(message: Message, state: FSMContext):
        """Обработка суммы баланса"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        try:
            amount = float(message.text.strip())
            amount_cents = int(amount * 100)
        except ValueError:
            await message.answer("❌ Неверный формат суммы. Введите число (например: 100.50)")
            return
        
        data = await state.get_data()
        user_id = data.get('target_user_id')
        action = data.get('balance_action')
        
        async with get_connection() as conn:
            # Получаем текущий баланс или создаем запись
            balance_row = await conn.fetchrow('SELECT balance FROM user_balances WHERE user_id = $1', user_id)
            current_balance = balance_row['balance'] if balance_row else 0
            
            if action == "balance_add":
                new_balance = current_balance + amount_cents
            else:
                new_balance = max(0, current_balance - amount_cents)
            
            # Обновляем или создаем баланс
            await conn.execute('''
                INSERT INTO user_balances (user_id, balance, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE
                SET balance = $2, updated_at = CURRENT_TIMESTAMP
            ''', user_id, new_balance)
        
        await message.answer(
            f"✅ <b>Баланс обновлен</b>\n\n"
            f"Пользователь: <i>{user_id}</i>\n"
            f"Было: <i>{current_balance / 100:.2f}₽</i>\n"
            f"Изменение: <i>{'+' if action == 'balance_add' else '-'}{amount:.2f}₽</i>\n"
            f"Стало: <i>{new_balance / 100:.2f}₽</i>",
            parse_mode="HTML"
        )
        await state.clear()
    
    # ==================== УПРАВЛЕНИЕ АДМИНАМИ ====================
    
    @dp.callback_query(F.data == "admin_manage_admins")
    async def handle_manage_admins(callback: CallbackQuery):
        """Управление админами"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        admin_list = ", ".join([str(admin_id) for admin_id in config.bot.admin_ids])
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add_admin"))
        builder.row(InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove_admin"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            f"👥 <b>Управление админами</b>\n\n"
            f"Текущие админы: <code>{admin_list or 'нет'}</code>\n\n"
            f"⚠️ <b>Внимание:</b> Изменения вступят в силу после перезапуска бота\n\n"
            f"Админы настраиваются через переменную окружения ADMIN_IDS",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_add_admin")
    async def handle_add_admin(callback: CallbackQuery, state: FSMContext):
        """Добавить админа"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "➕ <b>Добавить админа</b>\n\n"
            "⚠️ <b>Внимание:</b> Админы настраиваются через переменную окружения ADMIN_IDS.\n\n"
            "Чтобы добавить админа:\n"
            "1. Добавьте user_id в переменную ADMIN_IDS в .env файле\n"
            "2. Перезапустите бота\n\n"
            "Формат: ADMIN_IDS=123456789,987654321",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_remove_admin")
    async def handle_remove_admin(callback: CallbackQuery, state: FSMContext):
        """Удалить админа"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "➖ <b>Удалить админа</b>\n\n"
            "⚠️ <b>Внимание:</b> Админы настраиваются через переменную окружения ADMIN_IDS.\n\n"
            "Чтобы удалить админа:\n"
            "1. Удалите user_id из переменной ADMIN_IDS в .env файле\n"
            "2. Перезапустите бота",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    # ==================== УПРАВЛЕНИЕ МЕНЕДЖЕРАМИ ====================
    
    @dp.callback_query(F.data == "admin_manage_managers")
    async def handle_manage_managers(callback: CallbackQuery):
        """Управление менеджерами"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            managers = await conn.fetch('SELECT user_id, username, first_name, support_link, is_active FROM managers WHERE is_active = TRUE')
        
        text = "🛟 <b>Управление менеджерами</b>\n\n"
        if managers:
            for manager in managers:
                link = manager['support_link'] or "не указана"
                text += f"• {manager['first_name']} (@{manager['username'] or 'нет'})\n  Ссылка: {link}\n\n"
        else:
            text += "Менеджеры не добавлены\n\n"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Добавить менеджера", callback_data="admin_add_manager"))
        builder.row(InlineKeyboardButton(text="➖ Удалить менеджера", callback_data="admin_remove_manager"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_add_manager")
    async def handle_add_manager_callback(callback: CallbackQuery, state: FSMContext):
        """Добавить менеджера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await callback.message.edit_text(
            "➕ <b>Добавить менеджера</b>\n\n"
            "Введите user_id или @username:",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.ADD_MANAGER)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.ADD_MANAGER)
    async def process_add_manager(message: Message, state: FSMContext):
        """Обработка добавления менеджера"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        user_input = message.text.strip()
        user_id = None
        
        if user_input.startswith('@'):
            async with get_connection() as conn:
                user_data = await conn.fetchrow('SELECT user_id, username, first_name FROM users WHERE username = $1 OR username = $2', user_input, user_input[1:])
                if user_data:
                    user_id = user_data['user_id']
        else:
            try:
                user_id = int(user_input)
            except ValueError:
                await message.answer("❌ Неверный формат. Введите user_id (число) или @username")
                return
        
        if not user_id:
            await message.answer("❌ Пользователь не найден в базе. Сначала он должен запустить бота через /start")
            return
        
        await message.answer("Теперь введите ссылку на техподдержку (например, https://t.me/support):")
        await state.update_data(target_user_id=user_id)
        await state.set_state(AdminStates.MANAGER_SUPPORT_LINK)
    
    @dp.message(AdminStates.MANAGER_SUPPORT_LINK)
    async def process_manager_support_link(message: Message, state: FSMContext):
        """Обработка ссылки на техподдержку"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        support_link = message.text.strip()
        data = await state.get_data()
        user_id = data.get('target_user_id')
        
        async with get_connection() as conn:
            user_data = await conn.fetchrow('SELECT username, first_name FROM users WHERE user_id = $1', user_id)
            username = user_data['username'] if user_data else None
            first_name = user_data['first_name'] if user_data else "Unknown"
            
            await conn.execute('''
                INSERT INTO managers (user_id, username, first_name, support_link, is_active, created_at)
                VALUES ($1, $2, $3, $4, TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE
                SET username = $2, first_name = $3, support_link = $4, is_active = TRUE
            ''', user_id, username, first_name, support_link)
        
        await message.answer(f"✅ Менеджер добавлен! Ссылка на техподдержку: {support_link}")
        await state.clear()
    
    @dp.callback_query(F.data == "admin_remove_manager")
    async def handle_remove_manager(callback: CallbackQuery, state: FSMContext):
        """Удалить менеджера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            managers = await conn.fetch('SELECT user_id, username, first_name FROM managers WHERE is_active = TRUE')
        
        if not managers:
            await callback.message.edit_text("❌ Нет активных менеджеров")
            await safe_callback_answer(callback)
            return
        
        builder = InlineKeyboardBuilder()
        for manager in managers:
            builder.row(InlineKeyboardButton(
                text=f"❌ {manager['first_name']} (@{manager['username'] or 'нет'})",
                callback_data=f"manager_delete:{manager['user_id']}"
            ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_manage_managers"))
        
        await callback.message.edit_text(
            "➖ <b>Удалить менеджера</b>\n\n"
            "Выберите менеджера для удаления:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("manager_delete:"))
    async def confirm_delete_manager(callback: CallbackQuery):
        """Подтверждение удаления менеджера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        user_id = int(callback.data.split(":")[1])
        
        async with get_connection() as conn:
            await conn.execute('UPDATE managers SET is_active = FALSE WHERE user_id = $1', user_id)
        
        await callback.message.edit_text("✅ Менеджер удален")
        await safe_callback_answer(callback)
    
    # ==================== УПРАВЛЕНИЕ РЕФЕРАЛЬНОЙ СИСТЕМОЙ ====================
    
    @dp.callback_query(F.data == "admin_referral")
    async def handle_admin_referral(callback: CallbackQuery):
        """Управление реферальной системой"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
            if not settings:
                inviter_days = 5
                invited_days = 3
            else:
                inviter_days = settings['inviter_bonus_days']
                invited_days = settings['invited_bonus_days']
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Дни для приглашающего", callback_data="admin_referral_inviter"))
        builder.row(InlineKeyboardButton(text="✏️ Дни для приглашенного", callback_data="admin_referral_invited"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            f"🎁 <b>Управление реферальной системой</b>\n\n"
            f"Текущие настройки:\n"
            f"• Приглашающий получает: <b>{inviter_days} дней</b>\n"
            f"• Приглашенный получает: <b>{invited_days} дней</b>\n\n"
            f"Выберите, что хотите изменить:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_referral_inviter")
    async def handle_referral_inviter(callback: CallbackQuery, state: FSMContext):
        """Настройка дней для приглашающего"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral"))
        
        await callback.message.edit_text(
            "✏️ <b>Дни для приглашающего</b>\n\n"
            "Введите количество дней, которое получает приглашающий за каждого приглашенного друга:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.REFERRAL_INVITER_DAYS)
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_referral_invited")
    async def handle_referral_invited(callback: CallbackQuery, state: FSMContext):
        """Настройка дней для приглашенного"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_referral"))
        
        await callback.message.edit_text(
            "✏️ <b>Дни для приглашенного</b>\n\n"
            "Введите количество дней, которое получает приглашенный друг:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.REFERRAL_INVITED_DAYS)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.REFERRAL_INVITER_DAYS)
    async def process_referral_inviter_days(message: Message, state: FSMContext):
        """Обработка дней для приглашающего"""
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
            # Проверяем, есть ли уже настройки
            existing = await conn.fetchrow('SELECT id FROM referral_settings ORDER BY id DESC LIMIT 1')
            if not existing:
                # Получаем текущее значение для invited_bonus_days или используем 3 по умолчанию
                await conn.execute('''
                    INSERT INTO referral_settings (inviter_bonus_days, invited_bonus_days, updated_at)
                    VALUES ($1, 3, CURRENT_TIMESTAMP)
                ''', days)
            else:
                await conn.execute('''
                    UPDATE referral_settings
                    SET inviter_bonus_days = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM referral_settings ORDER BY id DESC LIMIT 1)
                ''', days)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к реферальной системе", callback_data="admin_referral"))
        
        await message.answer(
            f"✅ Количество дней для приглашающего установлено: {days}",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    
    @dp.message(AdminStates.REFERRAL_INVITED_DAYS)
    async def process_referral_invited_days(message: Message, state: FSMContext):
        """Обработка дней для приглашенного"""
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
            # Проверяем, есть ли уже настройки
            existing = await conn.fetchrow('SELECT id FROM referral_settings ORDER BY id DESC LIMIT 1')
            if not existing:
                # Используем 5 по умолчанию для inviter_bonus_days
                await conn.execute('''
                    INSERT INTO referral_settings (inviter_bonus_days, invited_bonus_days, updated_at)
                    VALUES (5, $1, CURRENT_TIMESTAMP)
                ''', days)
            else:
                await conn.execute('''
                    UPDATE referral_settings
                    SET invited_bonus_days = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM referral_settings ORDER BY id DESC LIMIT 1)
                ''', days)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к реферальной системе", callback_data="admin_referral"))
        
        await message.answer(
            f"✅ Количество дней для приглашенного установлено: {days}",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    
    # ==================== УПРАВЛЕНИЕ ПРОБНЫМ ПЕРИОДОМ ====================
    
    @dp.callback_query(F.data == "admin_trial")
    async def handle_admin_trial(callback: CallbackQuery):
        """Управление пробным периодом"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
            trial_days = settings['days'] if settings and settings['days'] else 0
        
        status_text = f"{trial_days} {'день' if trial_days == 1 else 'дня' if trial_days < 5 else 'дней'}" if trial_days > 0 else "Выключен (0 дней)"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Количество дней", callback_data="admin_trial_days"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(
            f"🆓 <b>Управление пробным периодом</b>\n\n"
            f"Текущее значение: <b>{status_text}</b>\n"
            f"(0 = пробный период отключен)\n\n"
            f"Выберите действие:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data == "admin_trial_days")
    async def handle_trial_days(callback: CallbackQuery, state: FSMContext):
        """Установка количества дней пробного периода"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_trial"))
        
        await callback.message.edit_text(
            "✏️ <b>Количество дней пробного периода</b>\n\n"
            "Введите количество дней для пробного периода:\n\n"
            "• <code>0</code> - пробный период отключен\n"
            "• <code>1</code> - пробный период на 1 день\n"
            "• <code>7</code> - пробный период на 7 дней\n\n"
            "И так далее...",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        await state.set_state(AdminStates.TRIAL_DAYS)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.TRIAL_DAYS)
    async def process_trial_days(message: Message, state: FSMContext):
        """Обработка количества дней для пробного периода"""
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
            # Проверяем, есть ли уже настройки
            existing = await conn.fetchrow('SELECT id FROM trial_settings ORDER BY id DESC LIMIT 1')
            if not existing:
                await conn.execute('''
                    INSERT INTO trial_settings (days, updated_at)
                    VALUES ($1, CURRENT_TIMESTAMP)
                ''', days)
            else:
                await conn.execute('''
                    UPDATE trial_settings
                    SET days = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT id FROM trial_settings ORDER BY id DESC LIMIT 1)
                ''', days)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к пробному периоду", callback_data="admin_trial"))
        
        status_text = f"{days} {'день' if days == 1 else 'дня' if days < 5 else 'дней'}" if days > 0 else "Выключен (0 дней)"
        await message.answer(
            f"✅ Пробный период установлен: <b>{status_text}</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    # ==================== УПРАВЛЕНИЕ СЕРВЕРАМИ (CALLBACK) ====================
    
    @dp.callback_query(F.data == "admin_servers")
    async def handle_admin_servers_callback(callback: CallbackQuery):
        """Управление серверами"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        # Сначала отвечаем на callback, чтобы избежать таймаута
        await safe_callback_answer(callback)
        
        try:
            async with get_connection() as conn:
                servers = await conn.fetch(
                    """
                    SELECT id, name, ip, is_active, display_order,
                           COALESCE(exclude_from_subscription, FALSE) AS exclude_from_subscription
                    FROM servers
                    ORDER BY display_order, id
                    """
                )
            
            if not servers:
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_server_add"))
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
                
                await callback.message.edit_text(
                    "🖥️ <b>Управление серверами</b>\n\n"
                    "Серверы не найдены.\n\n"
                    "Выберите действие:",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            else:
                text = "🖥️ <b>Управление серверами</b>\n\n"
                builder = InlineKeyboardBuilder()
                
                for server in servers:
                    status = "✅ Активен" if server["is_active"] else "⏸️ На паузе"
                    hid = " 🙈" if server.get("exclude_from_subscription") else ""
                    button_text = f"{server['name']} ({server['ip']}) - {status}{hid}"
                    builder.row(
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"admin_server_view:{server['id']}",
                        )
                    )
                
                builder.row(InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_server_add"))
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
                
                await callback.message.edit_text(
                    text,
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error in handle_admin_servers: {e}", exc_info=True)
            await callback.message.edit_text(
                f"❌ <b>Ошибка загрузки серверов</b>\n\n"
                f"<code>{str(e)}</code>",
                parse_mode="HTML"
            )
    
    @dp.callback_query(F.data.startswith("admin_server_view:"))
    async def handle_admin_server_view_callback(callback: CallbackQuery):
        """Просмотр информации о сервере"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        server_id = int(callback.data.split(":")[1])
        
        try:
            async with get_connection() as conn:
                server = await conn.fetchrow(
                    """
                    SELECT id, name, ip, port, protocol, username, password, inbound_id, base_url,
                           is_active, created_at, display_order,
                           COALESCE(exclude_from_subscription, FALSE) AS exclude_from_subscription
                    FROM servers WHERE id = $1
                    """,
                    server_id,
                )
                
                if not server:
                    await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                    return
                
                # Статистика по серверу
                keys_count = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE server_id = $1', server_id)
                active_keys = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE server_id = $1 AND is_active = TRUE', server_id)
            
            status = "✅ Активен" if server["is_active"] else "⏸️ На паузе"
            excl = bool(server.get("exclude_from_subscription"))
            sub_line = (
                "🙈 <b>Скрыт из подписки Happ</b> — в общем списке vless не показывается (узел «ТГ безлимит» и лимиты работают)."
                if excl
                else "📋 В подписке Happ — как обычный узел для всех с активной подпиской."
            )
            created_at = server["created_at"].strftime("%d.%m.%Y %H:%M") if server["created_at"] else "Неизвестно"
            
            # Обрезаем длинные значения для предотвращения MESSAGE_TOO_LONG
            base_url = server['base_url'] or ""
            if len(base_url) > 100:
                base_url = base_url[:97] + "..."
            
            text = (
                f"🖥️ <b>Информация о сервере</b>\n\n"
                f"<b>ID:</b> {server['id']}\n"
                f"<b>Название:</b> {server['name']}\n"
                f"<b>IP:</b> {server['ip']}\n"
                f"<b>Порт:</b> {server['port']}\n"
                f"<b>Протокол:</b> {server['protocol'].upper()}\n"
                f"<b>Base URL:</b> <code>{base_url}</code>\n"
                f"<b>Inbound ID:</b> {server['inbound_id']}\n"
                f"<b>Порядок в списке:</b> {server.get('display_order', 100)}\n"
                f"<b>Тип:</b> {'⚙️ Системный (скрыт)' if server.get('is_system') else '🌍 Публичный'}\n"
                f"<b>Статус:</b> {status}\n"
                f"{sub_line}\n"
                f"<b>Создан:</b> {created_at}\n\n"
                f"<b>Статистика:</b>\n"
                f"• Всего ключей: {keys_count}\n"
                f"• Активных ключей: {active_keys}\n"
            )
            
            # Проверяем длину сообщения (лимит Telegram - 4096 символов)
            if len(text) > 4096:
                text = text[:4050] + "\n\n⚠️ <i>Сообщение обрезано из-за ограничения длины</i>"
            
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(
                    text="⏸️ Пауза" if server["is_active"] else "▶️ Активировать",
                    callback_data=f"admin_server_toggle:{server_id}",
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="📋 Показывать в подписке Happ" if excl else "🙈 Скрыть из подписки Happ",
                    callback_data=f"admin_server_toggle_sub_exclude:{server_id}",
                )
            )
            builder.row(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"admin_server_edit:{server_id}"))
            builder.row(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"admin_server_delete:{server_id}"))
            builder.row(InlineKeyboardButton(text="◀️ Назад к серверам", callback_data="admin_servers"))
            
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            await safe_callback_answer(callback)
        except Exception as e:
            logger.error(f"Error in handle_admin_server_view: {e}", exc_info=True)
            await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)
    
    @dp.callback_query(F.data.startswith("admin_server_toggle:"))
    async def handle_admin_server_toggle_callback(callback: CallbackQuery):
        """Переключение статуса сервера (пауза/активация)"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        server_id = int(callback.data.split(":")[1])
        
        try:
            async with get_connection() as conn:
                server = await conn.fetchrow('SELECT is_active FROM servers WHERE id = $1', server_id)
                if not server:
                    await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                    return
                
                new_status = not server['is_active']
                await conn.execute('UPDATE servers SET is_active = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2', new_status, server_id)
                
                # Если сервер переведен на паузу — деактивируем ключи (кроме узла «ТГ безлимит»)
                if not new_status:
                    relay_id = await conn.fetchval(
                        """
                        SELECT tg_relay_server_id FROM traffic_settings ORDER BY id DESC LIMIT 1
                        """
                    )
                    if relay_id is not None and int(relay_id) == int(server_id):
                        logger.info(
                            "Server %s is TG relay — keys stay active while server paused",
                            server_id,
                        )
                    else:
                        deactivated_count = await conn.fetchval(
                            """
                            SELECT COUNT(*) FROM vpn_keys
                            WHERE server_id = $1 AND is_active = TRUE
                            """,
                            server_id,
                        )

                        if deactivated_count and deactivated_count > 0:
                            await conn.execute(
                                """
                                UPDATE vpn_keys
                                SET is_active = FALSE
                                WHERE server_id = $1 AND is_active = TRUE
                                """,
                                server_id,
                            )
                            logger.info(
                                f"Deactivated {deactivated_count} keys for server {server_id} (server paused)"
                            )
            
            status_text = "активирован" if new_status else "приостановлен"
            await safe_callback_answer(callback, f"✅ Сервер {status_text}")

            # Обновляем интерфейс
            new_callback = callback.model_copy(update={"data": f"admin_server_view:{server_id}"})
            await handle_admin_server_view_callback(new_callback)
        except Exception as e:
            logger.error(f"Error in handle_admin_server_toggle: {e}", exc_info=True)
            await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

    @dp.callback_query(F.data.startswith("admin_server_toggle_sub_exclude:"))
    async def handle_admin_server_toggle_sub_exclude_callback(callback: CallbackQuery):
        """Скрыть/показать сервер в общей подписке Happ (vless-список)."""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        server_id = int(callback.data.split(":")[1])
        try:
            async with get_connection() as conn:
                await conn.execute(
                    """
                    UPDATE servers
                    SET exclude_from_subscription = NOT COALESCE(exclude_from_subscription, FALSE),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    server_id,
                )
            new_callback = callback.model_copy(update={"data": f"admin_server_view:{server_id}"})
            await handle_admin_server_view_callback(new_callback)
        except Exception as e:
            logger.error(f"Error in handle_admin_server_toggle_sub_exclude: {e}", exc_info=True)
            await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

    @dp.callback_query(F.data.startswith("admin_server_delete:"))
    async def handle_admin_server_delete_callback(callback: CallbackQuery):
        """Удаление сервера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        server_id = int(callback.data.split(":")[1])
        
        try:
            async with get_connection() as conn:
                server = await conn.fetchrow('SELECT name FROM servers WHERE id = $1', server_id)
                if not server:
                    await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                    return
                
                # Удаляем все ключи этого сервера перед удалением (чтобы не нарушить foreign key constraint)
                deleted_keys_count = await conn.fetchval('''
                    SELECT COUNT(*) FROM vpn_keys
                    WHERE server_id = $1
                ''', server_id)
                
                if deleted_keys_count and deleted_keys_count > 0:
                    await conn.execute('''
                        DELETE FROM vpn_keys
                        WHERE server_id = $1
                    ''', server_id)
                    logger.info(f"Deleted {deleted_keys_count} keys for server {server_id} before deletion")
                
                # Удаляем сервер
                await conn.execute('DELETE FROM servers WHERE id = $1', server_id)
            
            await safe_callback_answer(callback, f"✅ Сервер '{server['name']}' удален")
            await handle_admin_servers_callback(callback)
        except Exception as e:
            logger.error(f"Error in handle_admin_server_delete: {e}", exc_info=True)
            await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)
    
    @dp.callback_query(F.data == "admin_server_add")
    async def handle_admin_server_add_callback(callback: CallbackQuery, state: FSMContext):
        """Начало добавления сервера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_servers"))
        
        await callback.message.edit_text(
            "➕ <b>Добавление сервера</b>\n\n"
            "Введите название сервера:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        await state.set_state(AdminStates.SERVER_NAME)
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_server_edit:"))
    async def handle_admin_server_edit_callback(callback: CallbackQuery, state: FSMContext):
        """Начало редактирования сервера"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        server_id = int(callback.data.split(":")[1])
        
        async with get_connection() as conn:
            server = await conn.fetchrow('''
                SELECT id, name, ip, port, protocol, username, password, inbound_id, base_url, display_order
                FROM servers WHERE id = $1
            ''', server_id)
            
            if not server:
                await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                return
        
        await state.update_data(server_id=server_id, edit_mode=True)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Название", callback_data=f"admin_server_edit_field:name:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ IP", callback_data=f"admin_server_edit_field:ip:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Порт", callback_data=f"admin_server_edit_field:port:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Протокол", callback_data=f"admin_server_edit_field:protocol:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Username", callback_data=f"admin_server_edit_field:username:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Password", callback_data=f"admin_server_edit_field:password:{server_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Inbound ID", callback_data=f"admin_server_edit_field:inbound_id:{server_id}"))
        builder.row(InlineKeyboardButton(text="🔢 Порядок отображения", callback_data=f"admin_server_edit_field:display_order:{server_id}"))
        builder.row(InlineKeyboardButton(text="⚙️ Сделать системным/обычным", callback_data=f"admin_server_edit_field:is_system:{server_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_server_view:{server_id}"))
        
        await callback.message.edit_text(
            f"✏️ <b>Редактирование сервера</b>\n\n"
            f"<b>Текущие данные:</b>\n"
            f"Название: <i>{server['name']}</i>\n"
            f"IP: <i>{server['ip']}</i>\n"
            f"Порт: <i>{server['port']}</i>\n"
            f"Протокол: <i>{server['protocol'].upper()}</i>\n"
            f"Username: <i>{server['username']}</i>\n"
            f"Inbound ID: <i>{server['inbound_id']}</i>\n"
            f"Порядок: <i>{server['display_order']}</i>\n"
            f"Тип: <i>{'Системный' if server.get('is_system') else 'Обычный'}</i>\n\n"
            f"Выберите поле для редактирования:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_server_edit_field:"))
    async def handle_admin_server_edit_field(callback: CallbackQuery, state: FSMContext):
        """Обработка выбора поля для редактирования"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        parts = callback.data.split(":")
        field = parts[1]
        server_id = int(parts[2])
        
        # Если это переключение типа сервера (is_system), делаем это сразу
        if field == 'is_system':
            async with get_connection() as conn:
                await conn.execute('UPDATE servers SET is_system = NOT is_system, updated_at = CURRENT_TIMESTAMP WHERE id = $1', server_id)
            await safe_callback_answer(callback, "✅ Тип сервера изменен")
            # Возвращаемся в меню редактирования
            new_callback = callback.model_copy(update={'data': f"admin_server_edit:{server_id}"})
            await handle_admin_server_edit(new_callback)
            return

        await state.update_data(server_id=server_id, edit_field=field)
        
        field_names = {
            'name': 'название',
            'ip': 'IP адрес',
            'port': 'порт',
            'protocol': 'протокол (http/https)',
            'username': 'username',
            'password': 'password',
            'inbound_id': 'Inbound ID',
            'display_order': 'порядок отображения',
            'is_system': 'тип сервера'
        }
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_server_edit:{server_id}"))
        
        await callback.message.edit_text(
            f"✏️ <b>Редактирование {field_names.get(field, field)}</b>\n\n"
            f"Введите новое значение:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        await state.set_state(AdminStates.SERVER_EDIT)
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.SERVER_EDIT)
    async def process_server_edit_callback(message: Message, state: FSMContext):
        """Обработка редактирования сервера"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        data = await state.get_data()
        server_id = data.get('server_id')
        field = data.get('edit_field')
        
        if not server_id or not field:
            await message.answer("❌ Ошибка данных")
            await state.clear()
            return
        
        new_value = message.text.strip()
        
        # Валидация в зависимости от поля
        if field == 'port':
            try:
                new_value = int(new_value)
            except ValueError:
                await message.answer("❌ Порт должен быть числом")
                return
        elif field == 'protocol':
            new_value = new_value.lower()
            if new_value not in ['http', 'https']:
                await message.answer("❌ Протокол должен быть http или https")
                return
        elif field in ['inbound_id', 'display_order']:
            try:
                new_value = int(new_value)
            except ValueError:
                await message.answer(f"❌ {'Inbound ID' if field == 'inbound_id' else 'Порядок'} должен быть числом")
                return
        
        async with get_connection() as conn:
            # Если меняется IP, порт или протокол, нужно обновить base_url
            if field in ['ip', 'port', 'protocol']:
                server = await conn.fetchrow('SELECT ip, port, protocol FROM servers WHERE id = $1', server_id)
                ip = new_value if field == 'ip' else server['ip']
                port = new_value if field == 'port' else server['port']
                protocol = new_value if field == 'protocol' else server['protocol']
                base_url = f"{protocol}://{ip}:{port}"
                
                # Обновляем поле и base_url
                await conn.execute(f'UPDATE servers SET {field} = $1, base_url = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $3', new_value, base_url, server_id)
            else:
                await conn.execute(f'UPDATE servers SET {field} = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2', new_value, server_id)
            
            # Обновляем VLESS ссылки для всех пользователей при любом изменении сервера
            # (название, IP, порт, base_url и т.д. влияют на ссылку)
            try:
                # Запускаем обновление ссылок в фоне, чтобы не блокировать ответ админу
                import asyncio
                asyncio.create_task(update_vless_links_for_server(server_id))
                logger.info(f"Scheduled VLESS links update for server {server_id} after editing {field}")
            except Exception as e:
                logger.error(f"Error scheduling VLESS links update for server {server_id}: {e}")
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к серверу", callback_data=f"admin_server_view:{server_id}"))
        
        await message.answer(
            f"✅ Поле <b>{field}</b> обновлено!\n\n"
            f"🔄 VLESS ссылки пользователей обновляются...",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    @dp.message(AdminStates.SERVER_NAME)
    async def process_server_name_callback(message: Message, state: FSMContext):
        """Обработка названия сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        await state.update_data(name=message.text)
        await message.answer("Введите IP адрес сервера:")
        await state.set_state(AdminStates.SERVER_IP)
    
    @dp.message(AdminStates.SERVER_IP)
    async def process_server_ip_callback(message: Message, state: FSMContext):
        """Обработка IP сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        await state.update_data(ip=message.text)
        await message.answer("Введите порт (по умолчанию 54321, можно просто нажать Enter):")
        await state.set_state(AdminStates.SERVER_PORT)
    
    @dp.message(AdminStates.SERVER_PORT)
    async def process_server_port_callback(message: Message, state: FSMContext):
        """Обработка порта сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        port_text = message.text.strip()
        port = int(port_text) if port_text else 54321
        await state.update_data(port=port)
        await message.answer("Введите протокол (http/https, по умолчанию https):")
        await state.set_state(AdminStates.SERVER_PROTOCOL)
    
    @dp.message(AdminStates.SERVER_PROTOCOL)
    async def process_server_protocol_callback(message: Message, state: FSMContext):
        """Обработка протокола сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        protocol = message.text.strip().lower() if message.text.strip() else 'https'
        if protocol not in ['http', 'https']:
            protocol = 'https'
        
        data = await state.get_data()
        port = data.get('port', 54321)
        ip = data.get('ip')
        base_url = f"{protocol}://{ip}:{port}"
        
        await state.update_data(protocol=protocol, base_url=base_url)
        await message.answer("Введите username для панели 3x-ui:")
        await state.set_state(AdminStates.SERVER_USERNAME)
    
    @dp.message(AdminStates.SERVER_USERNAME)
    async def process_server_username_callback(message: Message, state: FSMContext):
        """Обработка username сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        await state.update_data(username=message.text)
        await message.answer("Введите password для панели 3x-ui:")
        await state.set_state(AdminStates.SERVER_PASSWORD)
    
    @dp.message(AdminStates.SERVER_PASSWORD)
    async def process_server_password_callback(message: Message, state: FSMContext):
        """Обработка password сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        await state.update_data(password=message.text)
        await message.answer("Введите Inbound ID (число):")
        await state.set_state(AdminStates.SERVER_INBOUND_ID)
    
    @dp.message(AdminStates.SERVER_INBOUND_ID)
    async def process_server_inbound_id_callback(message: Message, state: FSMContext):
        """Обработка Inbound ID и сохранение сервера (для callback)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        try:
            inbound_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Inbound ID должен быть числом. Попробуйте снова:")
            return
        
        data = await state.get_data()
        name = data.get('name')
        ip = data.get('ip')
        port = data.get('port', 54321)
        protocol = data.get('protocol', 'https')
        username = data.get('username')
        password = data.get('password')
        base_url = data.get('base_url')
        
        # Проверяем подключение к серверу
        try:
            test_client = XUIClient(
                base_url=base_url,
                username=username,
                password=password,
                inbound_id=inbound_id
            )
            await test_client.login()
            await test_client.close()
        except Exception as e:
            if 'test_client' in locals():
                await test_client.close()
            error_msg = str(e)
            await message.answer(
                f"❌ <b>Ошибка подключения к серверу:</b>\n"
                f"<code>{error_msg}</code>\n\n"
                f"Проверьте данные и попробуйте снова. Используйте /admin для возврата в админ-панель.",
                parse_mode="HTML"
            )
            await state.clear()
            return
        
        # Сохраняем сервер в БД
        async with get_connection() as conn:
            await conn.execute('''
                SELECT setval('servers_id_seq', COALESCE((SELECT MAX(id) FROM servers), 0) + 1, false)
            ''')
            server_id = await conn.fetchval('''
                INSERT INTO servers (name, ip, port, protocol, username, password, inbound_id, base_url, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
                RETURNING id
            ''', name, ip, port, protocol, username, password, inbound_id, base_url)
            
            # Создаём ключи для нового сервера всем активным пользователям
            try:
                # Создаём ключи в фоне, чтобы не блокировать ответ админу
                asyncio.create_task(create_keys_for_specific_server(server_id))
                logger.info(f"Scheduled key creation for new server {name} (ID: {server_id})")
            except Exception as e:
                logger.error(f"Error scheduling key creation for new server: {e}")
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к серверам", callback_data="admin_servers"))
        
        await message.answer(
            f"✅ <b>Сервер успешно добавлен!</b>\n\n"
            f"ID: <i>{server_id}</i>\n"
            f"Название: <i>{name}</i>\n"
            f"IP: <i>{ip}</i>\n"
            f"Протокол: <i>{protocol.upper()}</i>\n"
            f"Порт: <i>{port}</i>\n\n"
            f"🔑 Ключи для активных пользователей создаются автоматически...",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
    
    # ==================== УПРАВЛЕНИЕ ПРИЛОЖЕНИЯМИ ДЛЯ УСТРОЙСТВ ====================
    
    @dp.callback_query(F.data == "admin_device_apps")
    async def handle_admin_device_apps(callback: CallbackQuery):
        """Управление приложениями для устройств"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        # Получаем список всех приложений
        async with get_connection() as conn:
            apps = await conn.fetch('''
                SELECT id, device_type, app_name, app_url, display_order, is_active
                FROM device_apps
                ORDER BY device_type, display_order, id
            ''')
        
        # Группируем по устройствам
        devices_dict = {}
        for app in apps:
            device_type = app['device_type']
            if device_type not in devices_dict:
                devices_dict[device_type] = []
            devices_dict[device_type].append(app)
        
        text = "📱 <b>Управление приложениями для устройств</b>\n\n"
        
        if not devices_dict:
            text += "Приложений пока нет.\n"
        else:
            for device_type, apps_list in devices_dict.items():
                device_name = DEVICE_TYPES.get(device_type, device_type)
                text += f"<b>{device_name}:</b>\n"
                for app in apps_list:
                    status = "✅" if app['is_active'] else "❌"
                    text += f"  {status} {app['app_name']} - {app['app_url'][:50]}...\n"
                text += "\n"
        
        builder = InlineKeyboardBuilder()
        for device_key, device_name in DEVICE_TYPES.items():
            builder.row(InlineKeyboardButton(
                text=f"➕ Добавить для {device_name}",
                callback_data=f"admin_add_device_app:{device_key}"
            ))
        
        # Кнопка просмотра/редактирования существующих
        if devices_dict:
            builder.row(InlineKeyboardButton(
                text="✏️ Управление приложениями",
                callback_data="admin_list_device_apps"
            ))
        
        builder.row(InlineKeyboardButton(
            text="📸 Управление фото инструкций",
            callback_data="admin_device_instructions"
        ))
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_add_device_app:"))
    async def handle_admin_add_device_app(callback: CallbackQuery, state: FSMContext):
        """Добавление приложения для устройства"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        device_type = callback.data.split(":")[1]
        device_name = DEVICE_TYPES.get(device_type, device_type)
        
        await state.update_data(device_type=device_type)
        await state.set_state(AdminStates.DEVICE_APP_NAME)
        
        await callback.message.edit_text(
            f"📱 <b>Добавление приложения для {device_name}</b>\n\n"
            "Введите название приложения (например: Shadowrocket, v2rayNG, Clash):",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.DEVICE_APP_NAME)
    async def process_device_app_name(message: Message, state: FSMContext):
        """Обработка названия приложения"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        app_name = message.text.strip()
        if not app_name:
            await message.answer("❌ Название не может быть пустым. Попробуйте снова:")
            return
        
        await state.update_data(app_name=app_name)
        await state.set_state(AdminStates.DEVICE_APP_URL)
        
        await message.answer(
            "📎 Теперь отправьте URL ссылку на приложение\n"
            "(например: https://apps.apple.com/app/shadowrocket/id932747118):"
        )
    
    @dp.message(AdminStates.DEVICE_APP_URL)
    async def process_device_app_url(message: Message, state: FSMContext):
        """Обработка URL приложения"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        app_url = message.text.strip()
        if not app_url.startswith(('http://', 'https://')):
            await message.answer("❌ URL должен начинаться с http:// или https://. Попробуйте снова:")
            return
        
        await state.update_data(app_url=app_url)
        await state.set_state(AdminStates.DEVICE_APP_ORDER)
        
        await message.answer(
            "🔢 Введите порядковый номер для отображения (чем меньше, тем выше в списке)\n"
            "Или отправьте 0 для автоматического порядка:"
        )
    
    @dp.message(AdminStates.DEVICE_APP_ORDER)
    async def process_device_app_order(message: Message, state: FSMContext):
        """Обработка порядка отображения"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        try:
            display_order = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Введите число. Попробуйте снова:")
            return
        
        data = await state.get_data()
        device_type = data['device_type']
        app_name = data['app_name']
        app_url = data['app_url']
        
        async with get_connection() as conn:
            try:
                await conn.execute('''
                    INSERT INTO device_apps (device_type, app_name, app_url, display_order, is_active)
                    VALUES ($1, $2, $3, $4, TRUE)
                ''', device_type, app_name, app_url, display_order)
                
                device_name = DEVICE_TYPES.get(device_type, device_type)
                await message.answer(
                    f"✅ Приложение <b>{app_name}</b> успешно добавлено для {device_name}!",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error adding device app: {e}")
                await message.answer(f"❌ Ошибка при добавлении приложения: {str(e)}")
        
        await state.clear()
    
    @dp.callback_query(F.data == "admin_list_device_apps")
    async def handle_admin_list_device_apps(callback: CallbackQuery):
        """Список всех приложений для управления"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            apps = await conn.fetch('''
                SELECT id, device_type, app_name, app_url, display_order, is_active
                FROM device_apps
                ORDER BY device_type, display_order, id
            ''')
        
        if not apps:
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_apps"))
            await callback.message.edit_text(
                "📱 <b>Список приложений</b>\n\nПриложений пока нет.",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            await safe_callback_answer(callback)
            return
        
        builder = InlineKeyboardBuilder()
        for app in apps:
            device_name = DEVICE_TYPES.get(app['device_type'], app['device_type'])
            status = "✅" if app['is_active'] else "❌"
            builder.row(InlineKeyboardButton(
                text=f"{status} {device_name} - {app['app_name']}",
                callback_data=f"admin_edit_device_app:{app['id']}"
            ))
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_apps"))
        
        text = "📱 <b>Список приложений</b>\n\nВыберите приложение для редактирования:"
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_edit_device_app:"))
    async def handle_admin_edit_device_app(callback: CallbackQuery):
        """Редактирование приложения"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        app_id = int(callback.data.split(":")[1])
        
        async with get_connection() as conn:
            app = await conn.fetchrow('''
                SELECT id, device_type, app_name, app_url, display_order, is_active
                FROM device_apps
                WHERE id = $1
            ''', app_id)
        
        if not app:
            await callback.answer("❌ Приложение не найдено", show_alert=True)
            return
        
        device_name = DEVICE_TYPES.get(app['device_type'], app['device_type'])
        status_text = "✅ Активно" if app['is_active'] else "❌ Неактивно"
        
        text = (
            f"📱 <b>Редактирование приложения</b>\n\n"
            f"Устройство: <b>{device_name}</b>\n"
            f"Название: <b>{app['app_name']}</b>\n"
            f"URL: <code>{app['app_url']}</code>\n"
            f"Порядок: <b>{app['display_order']}</b>\n"
            f"Статус: {status_text}\n"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="🔄 Изменить статус",
            callback_data=f"admin_toggle_device_app:{app_id}"
        ))
        builder.row(InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"admin_delete_device_app:{app_id}"
        ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_list_device_apps"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_toggle_device_app:"))
    async def handle_admin_toggle_device_app(callback: CallbackQuery):
        """Переключение статуса приложения"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        app_id = int(callback.data.split(":")[1])
        
        async with get_connection() as conn:
            app = await conn.fetchrow('SELECT is_active FROM device_apps WHERE id = $1', app_id)
            if not app:
                await callback.answer("❌ Приложение не найдено", show_alert=True)
                return
            
            new_status = not app['is_active']
            await conn.execute('UPDATE device_apps SET is_active = $1 WHERE id = $2', new_status, app_id)
        
        await callback.answer(f"✅ Статус изменен на {'активен' if new_status else 'неактивен'}")
        # Обновляем экран
        await handle_admin_edit_device_app(callback)
    
    @dp.callback_query(F.data.startswith("admin_delete_device_app:"))
    async def handle_admin_delete_device_app(callback: CallbackQuery, state: FSMContext):
        """Удаление приложения"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        app_id = int(callback.data.split(":")[1])
        
        async with get_connection() as conn:
            app = await conn.fetchrow('SELECT app_name FROM device_apps WHERE id = $1', app_id)
            if not app:
                await callback.answer("❌ Приложение не найдено", show_alert=True)
                return
            
            await conn.execute('DELETE FROM device_apps WHERE id = $1', app_id)
        
        await callback.answer(f"✅ Приложение {app['app_name']} удалено")
        
        # Возвращаемся к списку
        new_callback = callback.model_copy(update={'data': "admin_list_device_apps"})
        await handle_admin_list_device_apps(new_callback)
    
    @dp.callback_query(F.data == "admin_device_instructions")
    async def handle_admin_device_instructions(callback: CallbackQuery):
        """Управление фото инструкций для устройств"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        text = "📸 <b>Управление фото инструкций</b>\n\n"
        
        # Получаем количество фото для каждого устройства
        for device_key, device_name in DEVICE_TYPES.items():
            photos_count = len(await get_device_instruction_photos(device_key))
            if photos_count > 0:
                text += f"<b>{device_name}:</b> {photos_count} фото\n"
            else:
                text += f"<b>{device_name}:</b> нет фото\n"
        
        builder = InlineKeyboardBuilder()
        for device_key, device_name in DEVICE_TYPES.items():
            builder.row(InlineKeyboardButton(
                text=f"📸 Управление фото {device_name}",
                callback_data=f"admin_manage_device_photos:{device_key}"
            ))
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_apps"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_manage_device_photos:"))
    async def handle_admin_manage_device_photos(callback: CallbackQuery):
        """Управление фото для конкретного устройства"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        device_type = callback.data.split(":")[1]
        device_name = DEVICE_TYPES.get(device_type, device_type)
        
        photos = await get_device_instruction_photos_list(device_type)
        
        text = f"📸 <b>Управление фото инструкций для {device_name}</b>\n\n"
        
        if not photos:
            text += "Фото пока не загружены.\n\n"
            text += "Вы можете загрузить несколько фото. Каждое отправленное фото будет добавлено к существующим."
        else:
            text += f"Загружено фото: <b>{len(photos)}</b>\n\n"
            text += "Для добавления еще фото отправьте новое фото.\n"
            text += "Для удаления фото выберите его из списка ниже."
        
        builder = InlineKeyboardBuilder()
        
        # Кнопка для добавления фото
        builder.row(InlineKeyboardButton(
            text="➕ Добавить фото",
            callback_data=f"admin_add_device_photo:{device_type}"
        ))
        
        # Кнопки для удаления существующих фото
        if photos:
            for photo in photos[:10]:  # Ограничиваем 10 фото для удобства
                builder.row(InlineKeyboardButton(
                    text=f"🗑️ Удалить фото #{photo['id']}",
                    callback_data=f"admin_delete_device_photo:{photo['id']}"
                ))
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_instructions"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    
    @dp.callback_query(F.data.startswith("admin_add_device_photo:"))
    async def handle_admin_add_device_photo(callback: CallbackQuery, state: FSMContext):
        """Добавление фото инструкции для устройства"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        device_type = callback.data.split(":")[1]
        device_name = DEVICE_TYPES.get(device_type, device_type)
        
        await state.update_data(device_type=device_type)
        await state.set_state(AdminStates.DEVICE_INSTRUCTION_PHOTO_MULTIPLE)
        
        await callback.message.edit_text(
            f"📸 <b>Загрузка фото инструкции для {device_name}</b>\n\n"
            "Отправьте одно или несколько фото (скриншоты инструкции по подключению).\n"
            "Каждое фото будет добавлено к существующим.\n\n"
            "Для завершения отправки нажмите /cancel или вернитесь назад.",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
    
    @dp.message(AdminStates.DEVICE_INSTRUCTION_PHOTO_MULTIPLE, F.photo)
    async def process_device_instruction_photo_multiple(message: Message, state: FSMContext):
        """Обработка фото инструкции (можно несколько)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        # Получаем file_id фото (берем самое большое разрешение)
        photo_id = message.photo[-1].file_id
        
        data = await state.get_data()
        device_type = data['device_type']
        device_name = DEVICE_TYPES.get(device_type, device_type)
        
        await add_device_instruction_photo(device_type, photo_id)
        
        # Получаем текущее количество фото
        photos = await get_device_instruction_photos(device_type)
        count = len(photos)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="✅ Завершить",
            callback_data=f"admin_manage_device_photos:{device_type}"
        ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_instructions"))
        
        await message.answer(
            f"✅ Фото добавлено!\n\n"
            f"Для <b>{device_name}</b> сейчас загружено: <b>{count}</b> фото.\n\n"
            f"Отправьте ещё фото или нажмите 'Завершить'.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    
    @dp.message(AdminStates.DEVICE_INSTRUCTION_PHOTO_MULTIPLE)
    async def process_device_instruction_photo_multiple_invalid(message: Message, state: FSMContext):
        """Обработка некорректного сообщения (не фото)"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return
        
        if message.text and message.text.strip().lower() in ['/cancel', 'отмена', 'cancel']:
            data = await state.get_data()
            device_type = data.get('device_type')
            await state.clear()
            
            if device_type:
                device_name = DEVICE_TYPES.get(device_type, device_type)
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(
                    text="◀️ Назад к управлению фото",
                    callback_data=f"admin_manage_device_photos:{device_type}"
                ))
                await message.answer(
                    f"✅ Загрузка фото отменена.\n\n"
                    f"Вы вернулись из режима загрузки фото для {device_name}.",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            else:
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_device_instructions"))
                await message.answer("✅ Загрузка фото отменена.", reply_markup=builder.as_markup())
            return
        
        await message.answer("❌ Пожалуйста, отправьте фото (не текст или другой тип файла)\n\nДля завершения отправьте /cancel")
    
    @dp.callback_query(F.data.startswith("admin_delete_device_photo:"))
    async def handle_admin_delete_device_photo(callback: CallbackQuery):
        """Удаление фото инструкции"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        try:
            photo_db_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("❌ Неверный формат данных", show_alert=True)
            return
        
        # Получаем device_type перед удалением
        async with get_connection() as conn:
            result = await conn.fetchrow('SELECT device_type FROM device_instruction_photos WHERE id = $1', photo_db_id)
            if not result:
                await callback.answer("❌ Фото не найдено", show_alert=True)
                return
            device_type = result['device_type']
        
        await delete_device_instruction_photo(photo_db_id)
        
        await callback.answer("✅ Фото удалено")
        
        # Возвращаемся к управлению фото
        new_callback = callback.model_copy(update={'data': f"admin_manage_device_photos:{device_type}"})
        await handle_admin_manage_device_photos(new_callback)

    # ═══════════════════════════════════════════
    #  UTM TRACKING
    # ═══════════════════════════════════════════

    @dp.callback_query(F.data == "admin_utm")
    async def handle_admin_utm(callback: CallbackQuery):
        """Главная страница UTM"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            campaigns = await conn.fetch('SELECT * FROM utm_campaigns ORDER BY created_at DESC')
            total_visits = await conn.fetchval('SELECT COUNT(*) FROM utm_visits') or 0
            total_new = await conn.fetchval('SELECT COUNT(*) FROM utm_visits WHERE is_new_user = TRUE') or 0
            visits_today = await conn.fetchval(
                "SELECT COUNT(*) FROM utm_visits WHERE DATE(created_at) = CURRENT_DATE"
            ) or 0
        
        text = (
            f"📈 <b>UTM метки</b>\n\n"
            f"📊 Всего переходов: <b>{total_visits}</b>\n"
            f"👤 Новых пользователей: <b>{total_new}</b>\n"
            f"📅 Переходов сегодня: <b>{visits_today}</b>\n\n"
        )
        
        if campaigns:
            text += "<b>Настроенные кампании:</b>\n"
            for c in campaigns:
                status = "✅" if c['is_active'] else "❌"
                bonus = f"+{c['bonus_days']}д" if c['bonus_days'] else "без бонуса"
                text += f"{status} <code>{c['tag']}</code> — {c['description'] or 'без описания'} ({bonus})\n"
        else:
            text += "<i>Нет настроенных кампаний</i>\n"
        
        text += (
            "\n💡 Любая ссылка вида <code>https://t.me/SvoyVPN_robot?start=tag</code> "
            "будет засчитана автоматически, даже без создания кампании в админке."
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Создать кампанию", callback_data="utm_create"))
        builder.row(InlineKeyboardButton(text="📊 Детальная статистика", callback_data="utm_stats_detail"))
        if campaigns:
            for c in campaigns:
                builder.row(InlineKeyboardButton(
                    text=f"⚙️ {c['tag']}",
                    callback_data=f"utm_manage:{c['tag']}"
                ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await safe_callback_answer(callback)

    @dp.callback_query(F.data == "utm_create")
    async def handle_utm_create(callback: CallbackQuery, state: FSMContext):
        """Начать создание UTM кампании"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        await state.set_state(AdminStates.UTM_TAG)
        await callback.message.edit_text(
            "📈 <b>Создание UTM кампании</b>\n\n"
            "Введите тег (латиницей, без пробелов).\n"
            "Пример: <code>googleads</code>, <code>youtube_channel</code>, <code>blogger_ivan</code>\n\n"
            "Ссылка будет: <code>https://t.me/SvoyVPN_robot?start=ваш_тег</code>",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)

    @dp.message(AdminStates.UTM_TAG)
    async def process_utm_tag(message: Message, state: FSMContext):
        """Сохранить UTM тег"""
        tag = message.text.strip().lower().replace(' ', '_')
        if not tag or len(tag) < 2:
            await message.answer("❌ Тег слишком короткий (минимум 2 символа)")
            return
        if tag.startswith('ref_'):
            await message.answer("❌ Тег не может начинаться с <code>ref_</code> (зарезервировано для рефералов)", parse_mode="HTML")
            return
        
        # Проверяем уникальность
        async with get_connection() as conn:
            existing = await conn.fetchrow('SELECT id FROM utm_campaigns WHERE tag = $1', tag)
            if existing:
                await message.answer(f"❌ Тег <code>{tag}</code> уже существует", parse_mode="HTML")
                return
        
        await state.update_data(utm_tag=tag)
        await state.set_state(AdminStates.UTM_DESCRIPTION)
        await message.answer(
            f"Тег: <code>{tag}</code>\n\n"
            "Введите описание кампании (например: 'Реклама в Google')\n"
            "Или отправьте <code>-</code> чтобы пропустить:",
            parse_mode="HTML"
        )

    @dp.message(AdminStates.UTM_DESCRIPTION)
    async def process_utm_description(message: Message, state: FSMContext):
        """Сохранить описание"""
        desc = message.text.strip()
        if desc == '-':
            desc = ''
        
        await state.update_data(utm_description=desc)
        await state.set_state(AdminStates.UTM_BONUS_DAYS)
        await message.answer(
            "Сколько бонусных дней VPN давать новым пользователям по этой ссылке?\n\n"
            "Введите число (например: <code>7</code>) или <code>0</code> если без бонуса:",
            parse_mode="HTML"
        )

    @dp.message(AdminStates.UTM_BONUS_DAYS)
    async def process_utm_bonus_days(message: Message, state: FSMContext):
        """Сохранить бонусные дни и создать кампанию"""
        try:
            bonus_days = int(message.text.strip())
            if bonus_days < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите неотрицательное число")
            return
        
        data = await state.get_data()
        tag = data['utm_tag']
        desc = data.get('utm_description', '')
        
        async with get_connection() as conn:
            await conn.execute('''
                INSERT INTO utm_campaigns (tag, description, bonus_days)
                VALUES ($1, $2, $3)
            ''', tag, desc, bonus_days)
        
        await state.clear()
        
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={tag}"
        
        bonus_text = f"+{bonus_days} дней VPN" if bonus_days > 0 else "без бонуса"
        await message.answer(
            f"✅ UTM кампания создана!\n\n"
            f"🏷 Тег: <code>{tag}</code>\n"
            f"📝 Описание: {desc or '—'}\n"
            f"🎁 Бонус: {bonus_text}\n\n"
            f"🔗 Ссылка:\n<code>{link}</code>",
            parse_mode="HTML"
        )

    @dp.callback_query(F.data.startswith("utm_manage:"))
    async def handle_utm_manage(callback: CallbackQuery):
        """Управление конкретной UTM кампанией"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        tag = callback.data.split(":", 1)[1]
        
        async with get_connection() as conn:
            campaign = await conn.fetchrow('SELECT * FROM utm_campaigns WHERE tag = $1', tag)
            if not campaign:
                await safe_callback_answer(callback, "❌ Кампания не найдена", show_alert=True)
                return
            
            total_visits = await conn.fetchval(
                'SELECT COUNT(*) FROM utm_visits WHERE utm_tag = $1', tag
            ) or 0
            new_users = await conn.fetchval(
                'SELECT COUNT(*) FROM utm_visits WHERE utm_tag = $1 AND is_new_user = TRUE', tag
            ) or 0
            visits_7d = await conn.fetchval(
                "SELECT COUNT(*) FROM utm_visits WHERE utm_tag = $1 AND created_at >= CURRENT_DATE - INTERVAL '7 days'",
                tag
            ) or 0
            # Конверсия: из новых пользователей сколько купили подписку
            conversions = await conn.fetchval('''
                SELECT COUNT(DISTINCT p.user_id)
                FROM payments p
                JOIN utm_visits uv ON p.user_id = uv.user_id
                WHERE uv.utm_tag = $1 AND uv.is_new_user = TRUE AND p.status = 'completed'
            ''', tag) or 0
        
        conversion_rate = (conversions / new_users * 100) if new_users > 0 else 0
        status = "✅ Активна" if campaign['is_active'] else "❌ Неактивна"
        bonus = f"+{campaign['bonus_days']} дней" if campaign['bonus_days'] else "без бонуса"
        
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={tag}"
        
        text = (
            f"⚙️ <b>Кампания: {tag}</b>\n\n"
            f"📝 Описание: {campaign['description'] or '—'}\n"
            f"📊 Статус: {status}\n"
            f"🎁 Бонус: {bonus}\n\n"
            f"📈 Всего переходов: <b>{total_visits}</b>\n"
            f"👤 Новых пользователей: <b>{new_users}</b>\n"
            f"💰 Конверсия в оплату: <b>{conversions}</b> ({conversion_rate:.1f}%)\n"
            f"📅 За 7 дней: <b>{visits_7d}</b>\n\n"
            f"🔗 Ссылка:\n<code>{link}</code>"
        )
        
        toggle_text = "🔴 Деактивировать" if campaign['is_active'] else "🟢 Активировать"
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text=toggle_text, callback_data=f"utm_toggle:{tag}"))
        builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"utm_delete:{tag}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_utm"))
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await safe_callback_answer(callback)

    @dp.callback_query(F.data.startswith("utm_toggle:"))
    async def handle_utm_toggle(callback: CallbackQuery):
        """Переключить активность кампании"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        tag = callback.data.split(":", 1)[1]
        async with get_connection() as conn:
            await conn.execute(
                'UPDATE utm_campaigns SET is_active = NOT is_active WHERE tag = $1', tag
            )
        
        await safe_callback_answer(callback, "✅ Статус изменён")
        # Возвращаемся к управлению кампанией
        new_callback = callback.model_copy(update={'data': f"utm_manage:{tag}"})
        await handle_utm_manage(new_callback)

    @dp.callback_query(F.data.startswith("utm_delete:"))
    async def handle_utm_delete(callback: CallbackQuery):
        """Удалить UTM кампанию"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        tag = callback.data.split(":", 1)[1]
        async with get_connection() as conn:
            await conn.execute('DELETE FROM utm_campaigns WHERE tag = $1', tag)
        
        await safe_callback_answer(callback, "✅ Кампания удалена")
        new_callback = callback.model_copy(update={'data': 'admin_utm'})
        await handle_admin_utm(new_callback)

    @dp.callback_query(F.data == "utm_stats_detail")
    async def handle_utm_stats_detail(callback: CallbackQuery):
        """Детальная статистика по всем UTM меткам"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return
        
        async with get_connection() as conn:
            # Статистика по каждому тегу
            stats = await conn.fetch('''
                SELECT 
                    utm_tag,
                    COUNT(*) as total_visits,
                    COUNT(*) FILTER (WHERE is_new_user = TRUE) as new_users,
                    COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE - INTERVAL '7 days') as visits_7d,
                    COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE - INTERVAL '30 days') as visits_30d,
                    MIN(created_at) as first_visit,
                    MAX(created_at) as last_visit
                FROM utm_visits
                GROUP BY utm_tag
                ORDER BY total_visits DESC
                LIMIT 30
            ''')
        
        if not stats:
            text = "📊 <b>UTM статистика</b>\n\n<i>Нет данных о переходах</i>"
        else:
            text = "📊 <b>Детальная UTM статистика</b>\n\n"
            for s in stats:
                last_visit = s['last_visit'].strftime('%d.%m %H:%M') if s['last_visit'] else '—'
                text += (
                    f"🏷 <code>{s['utm_tag']}</code>\n"
                    f"   Всего: {s['total_visits']} | Новых: {s['new_users']} "
                    f"| 7д: {s['visits_7d']} | 30д: {s['visits_30d']}\n"
                    f"   Последний: {last_visit}\n\n"
                )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_utm"))
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await safe_callback_answer(callback)

    # ─── eSIM beta: одобрение заявок (кнопки в личке админу) ───
    @dp.callback_query(F.data.startswith("ebya:"))
    async def esim_beta_approve_callback(callback: CallbackQuery):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        try:
            rid = int(callback.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Некорректные данные", show_alert=True)
            return
        ws = WebhookServer.get_instance()
        if not ws:
            await safe_callback_answer(callback, "Webhook-сервер не готов", show_alert=True)
            return
        msg = await ws.resolve_esim_beta_request(rid, True, callback.from_user.id)
        await safe_callback_answer(callback)
        try:
            orig = callback.message.html_text or callback.message.text or ""
            await callback.message.edit_text(
                orig + "\n\n✅ <b>" + html_std.escape(msg) + "</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    @dp.callback_query(F.data.startswith("ebyr:"))
    async def esim_beta_reject_callback(callback: CallbackQuery):
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        try:
            rid = int(callback.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await safe_callback_answer(callback, "Некорректные данные", show_alert=True)
            return
        ws = WebhookServer.get_instance()
        if not ws:
            await safe_callback_answer(callback, "Webhook-сервер не готов", show_alert=True)
            return
        msg = await ws.resolve_esim_beta_request(rid, False, callback.from_user.id)
        await safe_callback_answer(callback)
        try:
            orig = callback.message.html_text or callback.message.text or ""
            await callback.message.edit_text(
                orig + "\n\n⛔ <b>" + html_std.escape(msg) + "</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    # Инфо о пользователе
    @dp.callback_query(F.data == "admin_user_info")
    async def handle_admin_user_info_start(callback: CallbackQuery, state: FSMContext):
        """Запрос ID для поиска информации"""
        if not is_admin(callback.from_user.id, config):
            await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
            return

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))

        await callback.message.edit_text(
            "👤 <b>Поиск информации о пользователе</b>\n\n"
            "Введите Telegram <b>ID</b> пользователя:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.USER_INFO_ID)
        await safe_callback_answer(callback)

    @dp.message(AdminStates.USER_INFO_ID)
    async def process_admin_user_info(message: Message, state: FSMContext):
        """Вывод детальной информации о пользователе"""
        if not is_admin(message.from_user.id, config):
            await message.answer("❌ Нет доступа")
            await state.clear()
            return

        user_id_str = message.text.strip()
        if not user_id_str.isdigit():
            await message.answer("❌ ID должен состоять только из цифр. Попробуйте снова:")
            return

        target_user_id = int(user_id_str)
        
        async with get_connection() as conn:
            # 1. Основная информация
            user = await conn.fetchrow('''
                SELECT user_id, username, first_name, registration_date, last_activity, 
                       pay_subscribed, subscription_end, invited_by, referral_count, balance, trial_used, utm_source
                FROM users WHERE user_id = $1
            ''', target_user_id)

            if not user:
                await message.answer(f"❌ Пользователь с ID <code>{target_user_id}</code> не найден в базе.", parse_mode="HTML")
                await state.clear()
                return

            # 2. Информация о платежах
            payments = await conn.fetch('''
                SELECT amount, currency, timestamp, status, plan_id 
                FROM payments 
                WHERE user_id = $1 
                ORDER BY timestamp DESC LIMIT 10
            ''', target_user_id)

            # 3. Кто пригласил (если есть)
            inviter_name = "Никто"
            if user['invited_by']:
                inviter = await conn.fetchrow('SELECT first_name, username FROM users WHERE user_id = $1', user['invited_by'])
                if inviter:
                    inviter_name = f"{inviter['first_name']} (@{inviter['username'] or '—'}) [<code>{user['invited_by']}</code>]"
            
            # Формируем текст
            sub_status = "✅ Активна" if user['pay_subscribed'] and user['subscription_end'] and user['subscription_end'] >= datetime.now() else "❌ Неактивна"
            sub_end = user['subscription_end'].strftime("%d.%m.%Y %H:%M") if user['subscription_end'] else "—"
            reg_date = user['registration_date'].strftime("%d.%m.%Y %H:%M") if user['registration_date'] else "—"
            last_act = user['last_activity'].strftime("%d.%m.%Y %H:%M") if user['last_activity'] else "—"
            
            username_display = f"@{user['username']}" if user['username'] else "—"

            report = (
                f"👤 <b>Карточка пользователя</b> <code>{target_user_id}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Имя:</b> {user['first_name'] or '—'}\n"
                f"🔗 <b>Username:</b> {username_display}\n"
                f"📅 <b>Регистрация:</b> <code>{reg_date}</code>\n"
                f"🕒 <b>Активность:</b> <code>{last_act}</code>\n"
                f"📍 <b>Источник (UTM):</b> <code>{user['utm_source'] or 'Прямой вход'}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 <b>Подписка:</b> {sub_status}\n"
                f"⏳ <b>Истекает:</b> <code>{sub_end}</code>\n"
                f"🎁 <b>Триал:</b> {'✅ Юзал' if user['trial_used'] else '❌ Нет'}\n"
                f"💰 <b>Баланс:</b> <code>{user['balance'] or 0}</code> коп.\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👥 <b>Рефералы:</b> {user['referral_count']} чел.\n"
                f"🤝 <b>Кто пригласил:</b> {inviter_name}\n"
            )

            if payments:
                report += f"━━━━━━━━━━━━━━━━━━\n💳 <b>Последние 10 платежей:</b>\n"
                for p in payments:
                    p_status = "✅" if p['status'] == 'completed' else "⏳" if p['status'] == 'pending' else "❌"
                    if p['currency'] == 'RUB':
                        rub_amount = p['amount'] / 100
                        p_sum = f"{rub_amount:.2f}".rstrip("0").rstrip(".")
                    else:
                        p_sum = str(p['amount'])
                    p_curr = "₽" if p['currency'] == 'RUB' else "⭐"
                    p_date = p['timestamp'].strftime("%d.%m.%y")
                    report += f"• {p_date}: <b>{p_sum}{p_curr}</b> {p_status} (<i>{p['plan_id']}</i>)\n"
            else:
                report += f"━━━━━━━━━━━━━━━━━━\n💳 <b>Платежи:</b> отсутствуют\n"

            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="◀️ Назад к поиску", callback_data="admin_user_info"))
            builder.row(InlineKeyboardButton(text="🏠 В админку", callback_data="admin_back"))

            await message.answer(report, reply_markup=builder.as_markup(), parse_mode="HTML")
            await state.clear()
