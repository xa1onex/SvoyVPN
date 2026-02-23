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
    
    # TODO: Добавить остальные обработчики админ-панели
    # - admin_stats
    # - admin_prices
    # - admin_discounts
    # - admin_broadcast
    # - edit_announcement
    # - admin_referral
    # - admin_trial
    # - admin_servers
    # - admin_balance
    # - admin_manage_admins
    # - admin_manage_managers
    # - admin_device_apps
    # - admin_manual_reminder
