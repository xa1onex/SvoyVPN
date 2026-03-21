import os
import asyncio
import secrets
import logging
import time
import signal
import random
from datetime import datetime, timedelta
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery, LabeledPrice, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

from .config import load_config
from .xui_client import XUIClient
from .database import init_db, get_connection, check_expired_subscriptions, ensure_subscription_token, generate_subscription_token
from .flyer_client import FlyerClient
from .unijump_client import UniJumpClient, UNIJUMP_TASKS
from .webhook_server import FlyerWebhookServer
from .yookassa_client import YooKassaClient

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Публичный базовый URL, по которому доступна подписка.
# Пример: https://MY_DOMAIN  (тогда бот будет отправлять https://MY_DOMAIN/sub/<token>)
SUBSCRIPTION_BASE_URL = (os.getenv("SUBSCRIPTION_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


def build_subscription_url(subscription_token: str) -> str:
    if SUBSCRIPTION_BASE_URL:
        return f"{SUBSCRIPTION_BASE_URL}/sub/{subscription_token}"
    # Fallback (лучше, чем ничего). В проде обязательно задайте SUBSCRIPTION_BASE_URL.
    return f"/sub/{subscription_token}"


async def get_user_subscription_url(user_id: int) -> str:
    token = await ensure_subscription_token(user_id)
    return build_subscription_url(token)

async def safe_callback_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасный ответ на callback query с обработкой устаревших запросов"""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        # Игнорируем ошибки устаревших callback queries
        error_msg = str(e).lower()
        if any(phrase in error_msg for phrase in [
            "query is too old",
            "query id is invalid",
            "response timeout expired"
        ]):
            logger.debug(f"Ignoring expired callback query: {e}")
        else:
            # Другие ошибки логируем
            logger.warning(f"Callback answer error: {e}")

# Планы подписки (базовые значения, могут быть переопределены из БД)
SUBSCRIPTION_PLANS_BASE = {

}

RENEWAL_PLANS_BASE = {

}

# Функции для получения планов с динамическими ценами
async def get_subscription_plans():
    """Получить планы подписки с динамическими ценами из БД"""
    plans = SUBSCRIPTION_PLANS_BASE.copy()
    async with get_connection() as conn:
        price_settings = await conn.fetch('SELECT plan_id, price_rub, price_stars FROM price_settings')
        for setting in price_settings:
            plan_id = setting['plan_id']
            if plan_id in plans:
                # Убеждаемся, что цены - целые числа
                plans[plan_id]['price_rub'] = int(setting['price_rub']) if setting['price_rub'] is not None else plans[plan_id]['price_rub']
                plans[plan_id]['price_stars'] = int(setting['price_stars']) if setting['price_stars'] is not None else plans[plan_id]['price_stars']
    return plans

async def get_renewal_plans():
    """Получить планы продления с динамическими ценами из БД"""
    plans = RENEWAL_PLANS_BASE.copy()
    async with get_connection() as conn:
        price_settings = await conn.fetch('SELECT plan_id, price_rub, price_stars FROM price_settings')
        for setting in price_settings:
            plan_id = setting['plan_id']
            if plan_id in plans:
                # Убеждаемся, что цены - целые числа
                plans[plan_id]['price_rub'] = int(setting['price_rub']) if setting['price_rub'] is not None else plans[plan_id]['price_rub']
                plans[plan_id]['price_stars'] = int(setting['price_stars']) if setting['price_stars'] is not None else plans[plan_id]['price_stars']
    return plans

# Для обратной совместимости
SUBSCRIPTION_PLANS = SUBSCRIPTION_PLANS_BASE
RENEWAL_PLANS = RENEWAL_PLANS_BASE

# Функции форматирования цен
def format_price_rub(price_cents: int) -> str:
    """Форматирует цену в рублях"""
    return f"{price_cents // 100}₽"

def format_price_stars(price_stars: int) -> str:
    """Форматирует цену в звездах"""
    return f"{price_stars}⭐"

def format_price_both(price_rub: int, price_stars: int) -> str:
    """Форматирует цену в рублях и звездах"""
    return f"{format_price_rub(price_rub)} | {format_price_stars(price_stars)}"

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

# Методы оплаты
PAYMENT_METHODS = {
    "stars": {
        "title": "Telegram Stars",
        "provider_token": "",
        "currency": "XTR"
    },
    "yookassa": {
        "title": "💳 Банковская карта (ЮKassa)",
        "provider_token": "",
        "currency": "RUB"
    }
}

POLICY_LINK = "https://telegra.ph/Konfidencialnost-i-usloviya-02-01"

class SubscriptionSteps(StatesGroup):
    CHOOSING_PLAN = State()
    CHOOSING_PAYMENT_METHOD = State()
    CHOOSING_SERVER = State()

class AddServerSteps(StatesGroup):
    WAITING_NAME = State()
    WAITING_PANEL_URL = State()
    WAITING_USERNAME = State()
    WAITING_PASSWORD = State()
    WAITING_INBOUND_ID = State()
    CONFIRMING = State()

class AdminEditStates(StatesGroup):
    EDIT_ANNOUNCEMENT = State()

class KeyManagementStates(StatesGroup):
    CHOOSING_SERVER_FOR_KEY = State()
    ENTERING_KEY_NAME = State()
    VIEWING_KEY = State()
    CONFIRMING_DELETE = State()
    CONFIRMING_REPLACE = State()

class AdminManualReminderStates(StatesGroup):
    CHOOSING_END_DAY = State()
    CHOOSING_TIME_BEFORE = State()

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
    MANAGER_SUPPORT_LINK = State()
    REFERRAL_INVITER_DAYS = State()
    REFERRAL_INVITED_DAYS = State()
    DISCOUNT_DAYS_THRESHOLD = State()
    DISCOUNT_ENABLE_FOR_ALL = State()
    TRIAL_DAYS = State()
    SERVER_NAME = State()
    SERVER_IP = State()
    SERVER_PORT = State()
    SERVER_PROTOCOL = State()
    SERVER_USERNAME = State()
    SERVER_PASSWORD = State()
    SERVER_INBOUND_ID = State()
    SERVER_BASE_URL = State()
    SERVER_EDIT = State()
    DEVICE_APP_DEVICE_TYPE = State()
    DEVICE_APP_NAME = State()
    DEVICE_APP_URL = State()
    DEVICE_APP_ORDER = State()
    DEVICE_APP_DELETE = State()
    DEVICE_INSTRUCTION_PHOTO = State()
    DEVICE_INSTRUCTION_PHOTO_MULTIPLE = State()

async def get_announcement_text() -> str:
    """Получает текст объявления из БД"""
    async with get_connection() as conn:
        result = await conn.fetchrow('SELECT text FROM announcements ORDER BY id DESC LIMIT 1')
        if result:
            return result['text']
    # Дефолтный текст, если в БД ничего нет
    return "!!!ВНИМАНИЕ!!! Это бета-тест, VPN работает нестабильно, платежи также находятся в тестировании - они не реальны!!!\n"

async def set_announcement_text(new_text: str):
    """Сохраняет текст объявления в БД"""
    async with get_connection() as conn:
        # Проверяем наличие колонки updated_at
        columns_result = await conn.fetch("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'announcements'
        """)
        columns = [row['column_name'] for row in columns_result]
        has_updated_at = 'updated_at' in columns
        
        # Удаляем старые объявления и добавляем новое
        await conn.execute('DELETE FROM announcements')
        if has_updated_at:
            await conn.execute('''
                INSERT INTO announcements (text, updated_at) VALUES ($1, CURRENT_TIMESTAMP)
            ''', new_text.strip())
        else:
            await conn.execute('''
                INSERT INTO announcements (text) VALUES ($1)
            ''', new_text.strip())

cfg = load_config()
bot = Bot(token=cfg.bot.bot_token)
dp = Dispatcher()
xui_client = XUIClient(cfg.xui)
flyer_client = FlyerClient(cfg.flyer) if cfg.flyer.enabled else None
unijump_client = UniJumpClient(cfg.unijump) if cfg.unijump.enabled else None
yookassa_client = YooKassaClient(cfg.yookassa) if cfg.yookassa.enabled else None
# Веб-сервер нужен не только для вебхуков, но и для subscription endpoint (/sub/{token})
flyer_webhook_server = FlyerWebhookServer(cfg.flyer, bot, cfg.yookassa, yookassa_client)

# init_db() будет вызван в main()

# Глобальные переменные для graceful shutdown
scheduler: AsyncIOScheduler | None = None
_shutdown_in_progress = False

async def get_main_keyboard(user_id: int):
    """Получает главную клавиатуру с проверкой пробного периода"""
    builder = InlineKeyboardBuilder()
    if is_admin(user_id):
        builder.row(InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel"))
    
    # Проверяем, должен ли показываться пробный период
    async with get_connection() as conn:
        # Получаем настройки пробного периода
        trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
        trial_days = trial_settings['days'] if trial_settings and trial_settings['days'] else 0
        
        # Получаем информацию о пользователе
        user_info = await conn.fetchrow('SELECT trial_used, pay_subscribed, subscription_end FROM users WHERE user_id = $1', user_id)
        
        # Показываем кнопку пробного периода если:
        # 1. Пробный период включен (days > 0)
        # 2. Пользователь еще не использовал пробный период (trial_used = False)
        # 3. У пользователя нет активной подписки
        show_trial = False
        if trial_days > 0 and user_info:
            trial_used = user_info.get('trial_used', False)
            has_active_sub = False
            
            if user_info.get('pay_subscribed') and user_info.get('subscription_end'):
                try:
                    subscription_end = user_info['subscription_end']
                    if isinstance(subscription_end, str):
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    has_active_sub = end_date_only >= today
                except:
                    pass
            
            show_trial = not trial_used and not has_active_sub
        
        if show_trial:
            builder.row(InlineKeyboardButton(text="🆓 Пробный период", callback_data="activate_trial"))
    
    builder.row(
        InlineKeyboardButton(text="💳 Подписка", callback_data="open_premium"),
        InlineKeyboardButton(text="🎁 Подарок", callback_data="open_invite")
    )
    builder.row(
        InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"),
    )
    if cfg.unijump.enabled:
        builder.row(InlineKeyboardButton(text="📋 Задания", callback_data="open_assignments"))
    builder.row(
        InlineKeyboardButton(text="🆘 Помощь", callback_data="open_help")
    )
    return builder.as_markup()

async def get_subscription_status(user_id: int) -> str:
    """Получает статус подписки пользователя"""
    try:
        async with get_connection() as conn:
            user_data = await conn.fetchrow('''
                SELECT subscription_end, pay_subscribed 
                FROM users 
                WHERE user_id = $1
            ''', user_id)

            if user_data and user_data['pay_subscribed'] and user_data['subscription_end']:
                try:
                    subscription_end = user_data['subscription_end']
                    # Парсим дату с учетом возможного формата с временем
                    if isinstance(subscription_end, str):
                        if ' ' in subscription_end:
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    
                    # Сравниваем только даты
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    if end_date_only >= today:
                        return f"активен до {end_date.strftime('%d.%m.%Y')}"
                except Exception as e:
                    logger.error(f"Error parsing subscription date in get_subscription_status: {e}, date: {user_data['subscription_end']}")
                    return "неактивен"
    except Exception as e:
        logger.error(f"Error in get_subscription_status: {e}")
    return "неактивен"

async def get_main_text(first_name: str, subscription_status: str, user_id: int = None) -> str:
    """Возвращает основной текст с объявлением"""
    ann = await get_announcement_text()
    msg = (
        f"👋 Рады видеть тебя снова, <b>{first_name}</b>!\n\n"
        f"Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель`"
        f"📌 <b>Команды:</b>\n"
        "<i>/start</i> - Перезагрузить бота\n"
        "<i>/prem</i> - Покупка VPN\n"
        "<i>/invite</i> - Пригласи друга\n\n"
        f"{ann}"
    )
    return msg

# Эмодзи для капчи
# Капча удалена

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    args = message.text.split()

    # Парсим реферальный код
    referral_code = args[1][4:] if len(args) > 1 and args[1].startswith('ref_') else None

    async with get_connection() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

        # Продолжаем обычную логику
        if not user:
            # Создаем нового пользователя (если ещё не создан, хотя такого быть не должно)
            new_referral_code = secrets.token_hex(4)
            sub_token = generate_subscription_token()
            await conn.execute('''
                INSERT INTO users (
                    user_id, 
                    username, 
                    first_name, 
                    registration_date,
                    last_activity,
                    subscribed,
                    referral_code,
                    invited_by,
                    pay_subscribed,
                    subscription_end,
                    subscription_token
                ) VALUES ($1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, FALSE, $4, NULL, FALSE, NULL, $5)
            ''', user_id, username, first_name, new_referral_code, sub_token)

            # Обработка реферального кода (только если капча пройдена)
            has_referral = False
            if referral_code:
                inviter = await conn.fetchrow('SELECT user_id FROM users WHERE referral_code = $1', referral_code)

                if inviter:
                    # Получаем настройки реферальной системы
                    referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
                    if not referral_settings:
                        inviter_bonus_days = 5
                        invited_bonus_days = 3
                    else:
                        inviter_bonus_days = referral_settings['inviter_bonus_days']
                        invited_bonus_days = referral_settings['invited_bonus_days']
                    
                    inviter_id = inviter['user_id']
                    # Обновляем данные пригласившего
                    await conn.execute(f'''
                        UPDATE users SET
                            referral_count = referral_count + 1,
                            subscription_end = CASE 
                                WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                                THEN CURRENT_DATE + INTERVAL '{inviter_bonus_days} days'
                                ELSE subscription_end + INTERVAL '{inviter_bonus_days} days'
                            END,
                            pay_subscribed = TRUE
                        WHERE user_id = $1
                    ''', inviter_id)

                    # Обновляем данные нового пользователя
                    await conn.execute(f'''
                        UPDATE users SET
                            invited_by = $1,
                            subscription_end = CURRENT_DATE + INTERVAL '{invited_bonus_days} days',
                            pay_subscribed = TRUE
                        WHERE user_id = $2
                    ''', inviter_id, user_id)

                    # Уведомления
                    try:
                        from datetime import timedelta
                        end_date = datetime.now() + timedelta(days=inviter_bonus_days)
                        await bot.send_message(
                            inviter_id,
                            f"🎉 Вы получили +{inviter_bonus_days} дней VPN за приглашение друга!\n"
                            f"Теперь ваш VPN активен до: {end_date.strftime('%d.%m.%Y')}\n\n"
                            "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
                        )
                    except Exception as e:
                        logging.error(f"Ошибка отправки уведомления: {e}")

                    has_referral = True
            else:
                # Проверяем, есть ли сохранённый реферальный код (во временном поле pending_referral_code)
                user_check = await conn.fetchrow('SELECT pending_referral_code FROM users WHERE user_id = $1', user_id)
                if user_check and user_check.get('pending_referral_code'):
                    # Обрабатываем рефералку, которая была сохранена до прохождения капчи
                    pending_code = user_check['pending_referral_code']
                    inviter = await conn.fetchrow('SELECT user_id FROM users WHERE referral_code = $1', pending_code)
                    if inviter:
                        inviter_id = inviter['user_id']
                        referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
                        if not referral_settings:
                            inviter_bonus_days = 5
                            invited_bonus_days = 3
                        else:
                            inviter_bonus_days = referral_settings['inviter_bonus_days']
                            invited_bonus_days = referral_settings['invited_bonus_days']
                        
                        # Обновляем данные пригласившего
                        await conn.execute(f'''
                            UPDATE users SET
                                referral_count = referral_count + 1,
                                subscription_end = CASE 
                                    WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                                    THEN CURRENT_DATE + INTERVAL '{inviter_bonus_days} days'
                                    ELSE subscription_end + INTERVAL '{inviter_bonus_days} days'
                                END,
                                pay_subscribed = TRUE
                            WHERE user_id = $1
                        ''', inviter_id)

                        # Обновляем данные нового пользователя
                        await conn.execute(f'''
                            UPDATE users SET
                                invited_by = $1,
                                subscription_end = CURRENT_DATE + INTERVAL '{invited_bonus_days} days',
                                pay_subscribed = TRUE,
                                pending_referral_code = NULL
                            WHERE user_id = $2
                        ''', inviter_id, user_id)

                        # Уведомления
                        try:
                            end_date = datetime.now() + timedelta(days=inviter_bonus_days)
                            await bot.send_message(
                                inviter_id,
                                f"🎉 Вы получили +{inviter_bonus_days} дней VPN за приглашение друга!\n"
                                f"Теперь ваш VPN активен до: {end_date.strftime('%d.%m.%Y')}\n\n"
                                "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
                            )
                        except Exception as e:
                            logging.error(f"Ошибка отправки уведомления: {e}")

                        has_referral = True

            # Уведомление админам о регистрации
            referral_info = "по реферальной ссылке" if has_referral else "без рефералки"
            await notify_admins(
                f"👤 <b>Новая регистрация</b>\n\n"
                f"ID: <code>{user_id}</code>\n"
                f"Имя: {first_name}\n"
                f"Username: @{username if username else 'нет'}\n"
                f"Реферальный код: <code>{new_referral_code}</code>\n"
                f"Регистрация: {referral_info}"
            )

            # Используем старый текст приветствия
            subscription_status = await get_subscription_status(user_id)
            await message.answer(
                await get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(user_id)
            )
        else:
            # Обновляем активность
            await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)

            subscription_status = await get_subscription_status(user_id)
            await message.answer(
                await get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(user_id)
            )

async def process_referral_after_captcha(user_id: int):
    """Обработать рефералку после успешного прохождения капчи"""
    async with get_connection() as conn:
        user = await conn.fetchrow('SELECT pending_referral_code FROM users WHERE user_id = $1', user_id)
        if not user or not user.get('pending_referral_code'):
            return False
        
        pending_code = user['pending_referral_code']
        inviter = await conn.fetchrow('SELECT user_id FROM users WHERE referral_code = $1', pending_code)
        if not inviter:
            # Очищаем невалидный реферальный код
            await conn.execute('UPDATE users SET pending_referral_code = NULL WHERE user_id = $1', user_id)
            return False
        
        inviter_id = inviter['user_id']
        referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
        if not referral_settings:
            inviter_bonus_days = 5
            invited_bonus_days = 3
        else:
            inviter_bonus_days = referral_settings['inviter_bonus_days']
            invited_bonus_days = referral_settings['invited_bonus_days']
        
        # Обновляем данные пригласившего
        await conn.execute(f'''
            UPDATE users SET
                referral_count = referral_count + 1,
                subscription_end = CASE 
                    WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                    THEN CURRENT_DATE + INTERVAL '{inviter_bonus_days} days'
                    ELSE subscription_end + INTERVAL '{inviter_bonus_days} days'
                END,
                pay_subscribed = TRUE
            WHERE user_id = $1
        ''', inviter_id)

        # Обновляем данные нового пользователя
        await conn.execute(f'''
            UPDATE users SET
                invited_by = $1,
                subscription_end = CURRENT_DATE + INTERVAL '{invited_bonus_days} days',
                pay_subscribed = TRUE,
                pending_referral_code = NULL
            WHERE user_id = $2
        ''', inviter_id, user_id)

        # Уведомления
        try:
            end_date = datetime.now() + timedelta(days=inviter_bonus_days)
            await bot.send_message(
                inviter_id,
                f"🎉 Вы получили +{inviter_bonus_days} дней VPN за приглашение друга!\n"
                f"Теперь ваш VPN активен до: {end_date.strftime('%d.%m.%Y')}\n\n"
                "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
        
        return True

async def _get_subscription_info(user_id: int):
    """Вспомогательная функция для получения информации о подписке"""
    async with get_connection() as conn:
        # Получаем данные пользователя - работаем с тем что есть
        # Сначала проверяем, какие колонки есть в таблице
        columns_result = await conn.fetch("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'users'
        """)
        columns = [row['column_name'] for row in columns_result]
        
        # Формируем запрос только с существующими колонками
        select_fields = ['subscription_end', 'pay_subscribed']
        if 'subscription_token' in columns:
            select_fields.insert(1, 'subscription_token')
        else:
            select_fields.insert(1, 'NULL as subscription_token')
        
        try:
            query = f'SELECT {", ".join(select_fields)} FROM users WHERE user_id = $1'
            result = await conn.fetchrow(query, user_id)
        except Exception as e:
            # Если ошибка - используем дефолтные значения
            logger.error(f"Database error in subscription info: {e}")
            result = None
    
    # Обрабатываем данные пользователя
    if result:
        # asyncpg возвращает dict-like объекты
        subscription_end = result.get('subscription_end')
        subscription_token = result.get('subscription_token') if 'subscription_token' in result else None
        pay_subscribed = result.get('pay_subscribed', 0)
        
        is_active = False
        
        # Проверяем, активна ли подписка
        if pay_subscribed and subscription_end:
            try:
                # Парсим дату окончания
                if isinstance(subscription_end, str):
                    # Может быть формат 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS'
                    if ' ' in subscription_end:
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                # Проверяем, не истекла ли подписка (сравниваем только даты)
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                
                if end_date_only >= today:
                    is_active = True
                    days_remaining = (end_date_only - today).days
                    end_date_str = end_date.strftime("%d.%m.%Y")
                else:
                    days_remaining = 0
                    end_date_str = None
            except Exception as e:
                logger.error(f"Error parsing subscription date: {e}, date: {subscription_end}")
                is_active = False
                days_remaining = 0
                end_date_str = None
        else:
            is_active = False
            days_remaining = 0
            end_date_str = None
    else:
        # Пользователь не найден в базе - используем дефолтные значения
        subscription_end = None
        subscription_token = None
        pay_subscribed = 0
        is_active = False
        days_remaining = 0
        end_date_str = None
    
    return {
        'user_id': user_id,
        'is_active': is_active,
        'subscription_end': subscription_end,
        'subscription_token': subscription_token,
        'pay_subscribed': pay_subscribed,
        'days_remaining': days_remaining,
        'end_date_str': end_date_str
    }

async def _build_subscription_message(info: dict, state: FSMContext):
    """Строит сообщение и клавиатуру для подписки"""
    builder = InlineKeyboardBuilder()
    is_active = info['is_active']
    days_remaining = info['days_remaining']
    end_date_str = info['end_date_str']
    subscription_url = await get_user_subscription_url(info['user_id'])
    
    if is_active:
        # Если подписка активна - показываем информацию и VPN ссылку
        # Форматируем дни: если < 1, то "СЕГОДНЯ"
        if days_remaining < 1:
            days_display = "СЕГОДНЯ"
        else:
            days_display = f"{days_remaining} {('день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней')}"
        text = (
            "✅ Ваш <b>VPN</b> <b>активен</b>!\n\n"
            f"📅 Дата окончания: <i>{end_date_str}</i>\n"
            f"⏰ Осталось: <i>{days_display}</i>\n\n"
        )
        
        # Бот больше не выдаёт VLESS-ключи напрямую — только ссылку подписки
        text += (
            "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
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
        
        # Логирование для отладки
        logger.info(f"_build_subscription_message: days_remaining={days_remaining}, show_discount={show_discount}")
        
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
        await state.set_state(SubscriptionSteps.CHOOSING_PLAN)
        
        # Кнопка "Назад" всегда
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    else:
        # Если подписка неактивна или пользователя нет - показываем планы
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        
        # Проверяем, должна ли показываться скидка (для режима "скидка для всех")
        # Если у пользователя нет подписки, days_remaining = 0, но в режиме enable_for_all скидка должна показываться
        show_discount = await should_show_discount(0)
        
        text = "💳 <b>Информация о вашем VPN:</b>\n\n"
        text += (
            "❌ Ваш VPN <b>неактивен</b>!\n\n"
            "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
        )
        
        if show_discount:
            # Если скидка активна - показываем текст про скидку
            text += "🎁 <b>Специальное предложение!</b>\n\n"
            text += "🔥 Получи <b>VPN</b> по специальной цене:\n\n"
            
            # Показываем скидочные планы с зачеркнутыми обычными ценами в тексте
            for plan_id in renewal_plans:
                renew_plan = renewal_plans[plan_id]
                # Находим обычную цену (убираем _renew из plan_id)
                base_plan_id = plan_id.replace('_renew', '')
                base_plan = subscription_plans.get(base_plan_id, {})
                old_price = format_price_rub(base_plan.get('price_rub', 0))
                new_price = format_price_rub(renew_plan['price_rub'])
                
                text += f"{renew_plan['title'].replace(' 🔥', '')} <s>{old_price}</s> - {new_price}\n"
            text += "\n"
            
            # Показываем кнопки со скидочными ценами
            for plan_id, plan_data in renewal_plans.items():
                builder.button(
                    text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                    callback_data=f"plan:{plan_id}"
                )
        else:
            # Если скидки нет - показываем обычные планы
            text += "Выберите план подписки:\n"
        for plan_id, plan_data in subscription_plans.items():
            builder.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
        await state.set_state(SubscriptionSteps.CHOOSING_PLAN)
    
    return text, builder


@dp.callback_query(F.data == "get_vpn_link")
async def handle_get_vpn_link(callback: CallbackQuery):
    """Отправляет пользователю ссылку подписки вида https://MY_DOMAIN/sub/<subscription_token>"""
    user_id = callback.from_user.id
    link = await get_user_subscription_url(user_id)
    await callback.message.answer(
        "🔗 <b>Получить VPN</b>\n\n"
        "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "open_assignments")
async def handle_open_assignments(callback: CallbackQuery):
    """Меню заданий Uni Jump: список заданий, ссылка на игру, проверка выполнения."""
    await safe_callback_answer(callback)
    if not unijump_client or not cfg.unijump.enabled:
        await callback.message.edit_text("Задания временно недоступны.", parse_mode="HTML")
        return
    game_link = (cfg.unijump.game_link or "").strip()
    task_names = [t.strip() for t in cfg.unijump.task_names.split(",") if t.strip()]
    if not task_names:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
        await callback.message.edit_text(
            "📋 <b>Задания</b>\n\nНет активных заданий. Ожидайте обновлений.",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
        return
    lines = ["📋 <b>Задания Uni Jump</b>\n"]
    if game_link:
        lines.append("Сначала зайдите в игру по ссылке ниже, выполните задание и нажмите «Проверить».\n")
    else:
        lines.append("Добавьте UNIJUMP_GAME_LINK в настройки бота для ссылки на игру.\n")
    builder = InlineKeyboardBuilder()
    if game_link:
        builder.row(InlineKeyboardButton(text="🎮 Играть в Uni Jump", url=game_link))
    for task_name in task_names:
        title = UNIJUMP_TASKS.get(task_name, task_name)
        builder.row(
            InlineKeyboardButton(text=f"✅ Проверить: {title}", callback_data=f"unijump_check:{task_name}"),
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("unijump_check:"))
async def handle_unijump_check(callback: CallbackQuery):
    """Проверка выполнения задания Uni Jump по API."""
    await safe_callback_answer(callback)
    if not unijump_client:
        await callback.answer("Задания отключены.", show_alert=True)
        return
    task_name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    title = UNIJUMP_TASKS.get(task_name, task_name)
    result = await unijump_client.check_task(task_name, user_id)
    if result is True:
        await callback.message.answer(
            f"✅ Задание «{title}» выполнено!",
            parse_mode="HTML",
        )
    elif result is False:
        await callback.message.answer(
            f"❌ Задание «{title}» пока не выполнено. Зайдите в игру и выполните его, затем нажмите «Проверить» снова.",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            "⚠️ Не удалось проверить задание. Попробуйте позже или обратитесь в поддержку.",
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "open_premium")
async def handle_open_premium_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки Premium (callback)"""
    user_id = callback.from_user.id
    
    
    await safe_callback_answer(callback)
    
    info = await _get_subscription_info(user_id)
    text, builder = await _build_subscription_message(info, state)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(Command("prem"))
async def handle_prem_command(message: Message, state: FSMContext):
    """Обработчик команды /prem"""
    user_id = message.from_user.id
    
    info = await _get_subscription_info(user_id)
    text, builder = await _build_subscription_message(info, state)
    
    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

async def handle_sub_info(callback: CallbackQuery, state: FSMContext):
    """Обертка для обратной совместимости - вызывает callback обработчик"""
    await handle_open_premium_callback(callback, state)

@dp.callback_query(F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":")[1]
    user_id = callback.from_user.id

    # Получаем динамические планы из БД
    subscription_plans = await get_subscription_plans()
    renewal_plans = await get_renewal_plans()
    ALL_PLANS = {**subscription_plans, **renewal_plans}

    if plan_id not in ALL_PLANS:
        await callback.answer("❌ Неверный план")
        return

    # Получаем динамические планы
    subscription_plans = await get_subscription_plans()
    renewal_plans = await get_renewal_plans()
    
    is_renewal = plan_id in renewal_plans or '_renew' in plan_id
    plan_data = renewal_plans.get(plan_id) if is_renewal else subscription_plans.get(plan_id)
    
    if not plan_data:
        await safe_callback_answer(callback, "❌ План не найден", show_alert=True)
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
        # Возвращаем к меню подписки
        await handle_open_premium_callback(callback, state)
        return
    
    # Проверяем наличие активной подписки для продления
    if is_renewal:
        if not active_sub:
            await callback.answer("❌ У вас нет активной подписки для продления!", show_alert=True)
            await handle_open_premium_callback(callback, state)
            return

    # Устанавливаем состояние FSM, если его еще нет
    current_state = await state.get_state()
    if current_state is None:
        await state.set_state(SubscriptionSteps.CHOOSING_PLAN)
    
    await state.update_data(
        selected_plan_id=plan_id,
        selected_plan_data=plan_data,
        is_renewal=is_renewal
    )
    
    # Сразу переходим к выбору метода оплаты (без выбора сервера)
    # Пользователь создаст ключ позже в разделе "Мои ключи"
    await show_payment_methods(callback, state)

# Обработчик выбора сервера для подписки больше не используется
# Пользователь создает ключи в разделе "Мои ключи" после покупки подписки

async def show_payment_methods(callback: CallbackQuery, state: FSMContext):
    """Показать методы оплаты"""
    data = await state.get_data()
    plan_data = data.get('selected_plan_data')
    
    builder = InlineKeyboardBuilder()
    for method_id, method_data in PAYMENT_METHODS.items():
        builder.button(
            text=method_data['title'],
            callback_data=f"method:{method_id}"
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="sub_back_to_plan"))
    builder.adjust(1)

    # Форматируем цены для отображения
    price_rub_formatted = format_price_rub(plan_data['price_rub'])
    price_stars_formatted = format_price_stars(plan_data['price_stars'])

    await callback.message.edit_text(
        f"📝 Выбранный план: <i>{plan_data['title']}</i>\n"
        f"💳 Сумма оплаты: <i>{price_rub_formatted}</i> или <i>{price_stars_formatted}</i>\n\n"
        "Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

    await state.set_state(SubscriptionSteps.CHOOSING_PAYMENT_METHOD)

@dp.callback_query(SubscriptionSteps.CHOOSING_PAYMENT_METHOD, F.data.startswith("method:"))
async def process_payment(callback: CallbackQuery, state: FSMContext):
    method_id = callback.data.split(":")[1]
    user_data = await state.get_data()
    plan_id = user_data.get('selected_plan_id')
    plan_data = user_data.get('selected_plan_data')

    if not all([method_id, plan_id, plan_data]):
        await callback.answer("❌ Ошибка данных")
        return

    # Не сохраняем server_id в payload - пользователь создаст ключ позже в разделе "Мои ключи"
    payload = f"{plan_id}|{method_id}"

    currency_type = 'stars' if PAYMENT_METHODS[method_id]['currency'] == 'XTR' else 'rub'
    price_key = f"price_{currency_type}"
    price = plan_data.get(price_key)
    
    # Логирование для отладки
    logger.info(f"Processing payment: plan_id={plan_id}, method_id={method_id}, currency_type={currency_type}, price_key={price_key}, price={price}, plan_data={plan_data}")
    
    # Валидация цены
    if price is None:
        await safe_callback_answer(callback, f"❌ Ошибка: цена не найдена для плана. Ключ: {price_key}", show_alert=True)
        logger.error(f"Price key '{price_key}' not found in plan_data for plan {plan_id}. Available keys: {list(plan_data.keys())}")
        return
    
    # Преобразуем цену в int (на случай если она float или str)
    try:
        price = int(float(price))
    except (ValueError, TypeError) as e:
        await safe_callback_answer(callback, "❌ Ошибка: неверный формат цены. Обратитесь к администратору.", show_alert=True)
        logger.error(f"Invalid price format for plan {plan_id}, method {method_id}: {price}, error: {e}")
        return
    
    if price <= 0:
        await safe_callback_answer(callback, "❌ Ошибка: цена должна быть больше нуля. Обратитесь к администратору.", show_alert=True)
        logger.error(f"Invalid price value for plan {plan_id}, method {method_id}: {price}")
        return

    # Обработка разных методов оплаты
    if method_id == "yookassa":
        # Оплата через ЮKassa
        if not yookassa_client or not yookassa_client.config.enabled:
            await safe_callback_answer(callback, "❌ ЮKassa не настроена. Обратитесь к администратору.", show_alert=True)
            logger.error("YooKassa is not enabled or configured")
            return
        
        # Проверяем минимальную сумму для ЮKassa (минимум 1 рубль = 100 копеек)
        if price < 100:
            await safe_callback_answer(callback, "❌ Минимальная сумма оплаты - 1 рубль", show_alert=True)
            logger.error(f"Price too small for YooKassa: {price}")
            return
        
        try:
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
            
            payment_message = await callback.message.edit_text(
                f"💳 <b>Оплата через ЮKassa</b>\n\n"
                f"План: <i>{plan_data['title']}</i>\n"
                f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                f"Нажмите кнопку ниже, чтобы перейти к оплате.\n"
                f"После успешной оплаты подписка будет активирована автоматически.\n\n"
                f"⏰ <i>Платеж не должен задерживаться больше 1 часа.</i>",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            
            # Сохраняем message_id в БД
            async with get_connection() as conn:
                await conn.execute('''
                    UPDATE payments 
                    SET message_id = $1 
                    WHERE yookassa_payment_id = $2 AND status = 'pending'
                ''', payment_message.message_id, payment_id)
            
            await safe_callback_answer(callback)
            
        except Exception as e:
            logger.error(f"Error creating YooKassa payment: {e}", exc_info=True)
            await safe_callback_answer(callback, "❌ Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
            return
    
    else:
        # Оплата через Telegram (Stars или другие методы)
    # Проверяем минимальную сумму для Telegram
    # Для RUB минимальная сумма - 1 копейка (1), для XTR - 1 звезда (1)
        if price < 1:
            await safe_callback_answer(callback, "❌ Ошибка: сумма слишком мала. Обратитесь к администратору.", show_alert=True)
            logger.error(f"Price too small for plan {plan_id}, method {method_id}: {price}")
            return

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"VPN подписка - {plan_data['title']}",
        description=f"Нажимая кнопку «Заплатить» Вы соглашаетесь с правилами VPN бота (/help)",
        provider_token=PAYMENT_METHODS[method_id]['provider_token'],
        currency=PAYMENT_METHODS[method_id]['currency'],
        prices=[LabeledPrice(label="VPN подписка", amount=price)],
        payload=payload,
        start_parameter='subscription'
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    try:
        payload = message.successful_payment.invoice_payload
        if "|" not in payload:
            raise ValueError("Неверный формат платежа")

        parts = payload.split("|")
        if len(parts) < 2:
            raise ValueError("Неверный формат payload")
        
        # Обработка подписки
        plan_id = parts[0]
        method_id = parts[1]

        # Получаем динамические планы
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()

        # Определение типа подписки
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
        duration_months = plan_data['duration']
        traffic_gb = plan_data['traffic_gb']

        user_id = message.from_user.id
        username = message.from_user.username or f"user_{user_id}"
        
        # Обновление подписки в базе данных (БЕЗ создания ключа)
        # Пользователь создаст ключ позже в разделе "Мои ключи"
        async with get_connection() as conn:
            if is_new_subscription:
                # Новая подписка
                days = duration_months * 30
                await conn.execute(f'''
                    UPDATE users 
                    SET 
                        pay_subscribed = TRUE,
                        subscription_end = CURRENT_DATE + INTERVAL '{days} days',
                        renewal_used = FALSE
                    WHERE user_id = $1
                ''', user_id)
            else:
                # Продление существующей подписки
                await conn.execute(f'''
                    UPDATE users 
                    SET 
                        subscription_end = subscription_end + INTERVAL '{duration_months} months',
                        renewal_used = TRUE
                    WHERE user_id = $1
                ''', user_id)

            # Получаем обновленную дату окончания
            subscription_end_row = await conn.fetchrow('''
                SELECT subscription_end FROM users WHERE user_id = $1
            ''', user_id)
            subscription_end = subscription_end_row['subscription_end']
            
            # Для новой подписки - создаём ключи для всех серверов
            # Для продления - синхронизируем существующие ключи
            if is_new_subscription:
                # Создаём ключи для всех активных серверов в фоне
                try:
                    asyncio.create_task(create_keys_for_all_servers(user_id))
                except Exception as e:
                    logger.warning(f"Could not create keys after subscription activation: {e}")
            else:
                # Синхронизируем ключи с новой датой подписки (продлеваем ключи)
                # Это обновит expires_at в БД и на серверах x-ui
                try:
                    asyncio.create_task(sync_user_keys(user_id))
                except Exception as e:
                    logger.warning(f"Could not sync keys after subscription renewal: {e}")
            
            # Сохраняем платеж
            await conn.execute('''
                INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, telegram_payment_charge_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', (
                user_id,
                plan_data[f"price_{'stars' if method_data['currency'] == 'XTR' else 'rub'}"],
                method_data['currency'],
                plan_id,
                'subscription',
                'completed',
                message.successful_payment.telegram_payment_charge_id
            ))
            

        # Форматирование дат
        activation_date = datetime.now().strftime("%d.%m.%Y")
        end_date = datetime.strptime(subscription_end, "%Y-%m-%d").strftime("%d.%m.%Y")

        # Форматирование цены
        price_key = f"price_{'stars' if method_data['currency'] == 'XTR' else 'rub'}"
        price = plan_data[price_key]

        if method_data['currency'] == 'XTR':
            formatted_price = f"{price} Stars (≈ {price * 0.01:.2f}₽)"
        else:
            formatted_price = format_price_rub(price)

        # Получаем лимит ключей для пользователя
        max_keys = await get_user_max_keys(user_id)

        # Формирование квитанции
        receipt = (
            f"💳 <b>VPN подписка</b> успешно активирована!\n\n"
            f"<b>Чек на оплату</b>\n"
            f"Дата активации: <i>{activation_date}</i>\n"
            f"Дата окончания: <i>{end_date}</i>\n"
            f"Способ оплаты: <i>{method_data['title']}</i>\n"
            f"Сумма оплаты: <i>{formatted_price}</i>\n\n"
            f"<b>Детали подписки</b>:\n"
            f"• План: <i>{plan_data['title']}</i>\n"
            f"• Трафик: <i>{traffic_gb} ГБ</i>\n"
            f"• Срок: <i>{duration_months} месяцев</i>\n\n"
            f"✅ Теперь вы можете создать до {max_keys} VPN ключей (выберите сервера)!\n"
            f"Затем нажмите <b>🔗 Получить VPN</b>, чтобы получить ссылку подписки.\n\n"
            f"ID транзакции: <blockquote>{message.successful_payment.telegram_payment_charge_id}</blockquote>"
        )

        await message.answer(receipt, parse_mode='HTML')
        
        # Уведомление админам о покупке подписки
        username = message.from_user.username or "нет"
        first_name = message.from_user.first_name or "Пользователь"
        await notify_admins(
            f"💳 <b>Покупка подписки</b>\n\n"
            f"Пользователь: {first_name} (@{username})\n"
            f"ID: <code>{user_id}</code>\n"
            f"План: {plan_data['title']}\n"
            f"Способ оплаты: {method_data['title']}\n"
            f"Сумма: {formatted_price}\n"
            f"Срок: {duration_months} месяцев\n"
            f"Трафик: {traffic_gb if traffic_gb else 'Безлимитный'} ГБ\n"
            f"Активирована до: {end_date}"
        )

    except Exception as e:
        logging.error(f"Ошибка обработки платежа: {str(e)}", exc_info=True)
        await message.answer(
            "❌ Произошла ошибка при обработке платежа. "
            "Пожалуйста, обратитесь в поддержку."
        )

@dp.callback_query(F.data == "open_invite")
async def handle_open_invite_callback(callback: CallbackQuery):
    """Обработчик кнопки Рефералка (callback)"""
    user_id = callback.from_user.id


    async with get_connection() as conn:
        result = await conn.fetchrow('''
            SELECT referral_code, referral_count 
            FROM users 
            WHERE user_id = $1
        ''', user_id)

        if not result:
            await callback.answer("❌ Сначала запустите бота через /start", show_alert=True)
            return

        referral_code = result['referral_code']
        referral_count = result['referral_count']

        # Если код по какой-то причине отсутствует в БД
        if not referral_code:
            referral_code = secrets.token_hex(4)
            await conn.execute('''
                UPDATE users
                SET referral_code = $1
                WHERE user_id = $2
            ''', referral_code, user_id)
        
        # Получаем настройки реферальной системы
        referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
        if not referral_settings:
            inviter_days = 5
            invited_days = 3
        else:
            inviter_days = referral_settings['inviter_bonus_days']
            invited_days = referral_settings['invited_bonus_days']

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    text = (
        f"🎁 <b>Пригласи друга и получи +{inviter_days} {'день' if inviter_days == 1 else 'дня' if inviter_days < 5 else 'дней'} VPN!</b>\n\n"
        f"Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"

    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={ref_link}&text={quote('Присоединяйся к VPN боту с моей подпиской!')}"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
    ])

    # Редактируем исходное сообщение с кнопкой
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await safe_callback_answer(callback)

@dp.message(Command("invite"))
async def handle_invite_command(message: Message):
    """Обработчик команды /invite"""
    user_id = message.from_user.id

    async with get_connection() as conn:
        result = await conn.fetchrow('''
            SELECT referral_code, referral_count 
            FROM users 
            WHERE user_id = $1
        ''', user_id)

        if not result:
            await message.answer("❌ Пожалуйста, сначала запустите бота с помощью команды /start")
            return

        referral_code = result['referral_code']
        referral_count = result['referral_count']

        # Если реферальный код отсутствует, генерируем новый
        if not referral_code:
            referral_code = secrets.token_hex(4)
            await conn.execute('''
                UPDATE users
                SET referral_code = $1
                WHERE user_id = $2
            ''', referral_code, user_id)
        
        # Получаем настройки реферальной системы
        referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
        if not referral_settings:
            inviter_days = 5
            invited_days = 3
        else:
            inviter_days = referral_settings['inviter_bonus_days']
            invited_days = referral_settings['invited_bonus_days']

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    text = (
        f"🎁 <b>Пригласи друга и получи +{inviter_days} {'день' if inviter_days == 1 else 'дня' if inviter_days < 5 else 'дней'} VPN!</b>\n\n"
        f"Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
    )

    # Клавиатура с кнопкой поделиться
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={ref_link}&text={quote('Присоединяйся к VPN боту с моей подпиской!')}"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
    ])

    await message.answer(text, parse_mode='HTML', reply_markup=keyboard)

@dp.callback_query(F.data == "go_back")
async def go_back_handler(callback: CallbackQuery):
    """Обработчик кнопки Назад"""
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name or "Пользователь"
    subscription_status = await get_subscription_status(user_id)

    await callback.message.edit_text(
        text=await get_main_text(first_name, subscription_status, user_id),
        parse_mode='HTML',
        reply_markup=await get_main_keyboard(user_id)
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "sub_back_to_plan")
async def handle_sub_back_to_plan(callback: CallbackQuery, state: FSMContext):
    await handle_sub_info(callback, state)

@dp.callback_query(F.data == "open_help")
@dp.message(Command("help"))
async def handle_open_help(message_or_callback: Message | CallbackQuery):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        await message_or_callback.answer()
    else:
        message = message_or_callback
    
    # Получаем ссылку на техподдержку
    support_link = await get_support_link()
    
    # Получаем настройки реферальной системы
    async with get_connection() as conn:
        referral_settings = await conn.fetchrow('SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1')
        if not referral_settings:
            inviter_days = 5
            invited_days = 3
        else:
            inviter_days = referral_settings['inviter_bonus_days']
            invited_days = referral_settings['invited_bonus_days']
    
    builder = InlineKeyboardBuilder()
    if support_link:
        builder.row(InlineKeyboardButton(text="🛟 Техподдержка", url=support_link))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))

    help_text = (
        "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
    )

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(
            help_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            help_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "edit_announcement")
async def start_edit_announcement(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
        await message.answer("Нет прав", parse_mode="HTML")
        await state.clear()
        return
    new_ann = message.text[:2048] if message.text else ''
    if not new_ann.strip():
        await message.answer("Сообщение не может быть пустым. Попробуйте снова (или отмените командой /start)")
        return
    await set_announcement_text(new_ann)
    await message.answer("✅ Объявление обновлено! Теперь оно показывается всем пользователям.", parse_mode="HTML")
    await state.clear()


def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом"""
    return user_id in cfg.bot.admin_ids

async def notify_admins(message_text: str):
    """Отправить уведомление всем админам о действиях пользователей"""
    try:
        for admin_id in cfg.bot.admin_ids:
            try:
                await bot.send_message(admin_id, message_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send admin notification to {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Error in notify_admins: {e}")

async def is_manager(user_id: int) -> bool:
    """Проверка, является ли пользователь менеджером"""
    async with get_connection() as conn:
        manager = await conn.fetchrow('SELECT user_id FROM managers WHERE user_id = $1 AND is_active = TRUE', user_id)
        return manager is not None

async def get_support_link() -> str:
    """Получить ссылку на техподдержку"""
    async with get_connection() as conn:
        manager = await conn.fetchrow('SELECT support_link FROM managers WHERE is_active = TRUE AND support_link IS NOT NULL LIMIT 1')
        if manager and manager['support_link']:
            return manager['support_link']
    # Если нет ссылки, возвращаем пустую строку или можно вернуть дефолтную
    return ""

async def get_active_servers():
    """Получить список активных серверов"""
    async with get_connection() as conn:
        return await conn.fetch('''
            SELECT id, name, ip, inbound_id 
            FROM servers 
            WHERE is_active = TRUE
            ORDER BY name
        ''')

async def get_server_by_id(server_id: int):
    """Получить данные сервера по ID"""
    async with get_connection() as conn:
        return await conn.fetchrow('''
            SELECT id, name, ip, username, password, inbound_id, base_url
            FROM servers 
            WHERE id = $1
        ''', server_id)

async def check_user_subscription(user_id: int) -> bool:
    """Проверка, есть ли у пользователя активная подписка"""
    try:
        async with get_connection() as conn:
            result = await conn.fetchrow('''
                SELECT pay_subscribed, subscription_end 
                FROM users 
                WHERE user_id = $1
            ''', user_id)
            if not result or not result['pay_subscribed']:
                return False
            if result['subscription_end']:
                try:
                    subscription_end = result[1]
                    subscription_end = result['subscription_end']
                    # Парсим дату с учетом возможного формата с временем
                    if isinstance(subscription_end, str):
                        if ' ' in subscription_end:
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    
                    # Сравниваем только даты
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    return end_date_only >= today
                except Exception as e:
                    logger.error(f"Error parsing subscription date in check_user_subscription: {e}, date: {result.get('subscription_end')}")
                    return False
            return False
    except Exception as e:
        logger.error(f"Error in check_user_subscription: {e}")
        return False

async def get_no_subscription_message(user_id: int) -> str:
    """Получить сообщение для пользователя без подписки с учетом пробного периода"""
    async with get_connection() as conn:
        # Проверяем настройки пробного периода
        trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
        trial_days = trial_settings['days'] if trial_settings and trial_settings['days'] else 0
        
        # Если пробный период доступен (days > 0)
        if trial_days > 0:
            # Проверяем, использовал ли пользователь пробный период
            user_info = await conn.fetchrow('SELECT trial_used FROM users WHERE user_id = $1', user_id)
            if user_info and not user_info.get('trial_used', False):
                return "❌ Попробуй VPN бесплатно активировав пробный период!"
        
        return "❌ У вас нет активной подписки. Купите подписку через /prem"

async def get_user_keys_count(user_id: int) -> int:
    """Получить количество активных ключей пользователя"""
    async with get_connection() as conn:
        count = await conn.fetchval('''
            SELECT COUNT(*) FROM vpn_keys 
            WHERE user_id = $1 AND is_active = TRUE
        ''', user_id)
        return count or 0

async def get_user_max_keys(user_id: int) -> int:
    """Получить максимальное количество ключей для пользователя"""
    async with get_connection() as conn:
        max_keys = await conn.fetchval('''
            SELECT max_keys FROM users WHERE user_id = $1
        ''', user_id)
        return max_keys if max_keys is not None else 5  # По умолчанию 5

async def get_user_keys(user_id: int):
    """Получить список всех ключей пользователя"""
    async with get_connection() as conn:
        return await conn.fetch('''
            SELECT k.id, k.key_name, k.vless_link, k.created_at, k.expires_at, 
                   k.traffic_gb, k.is_active, s.name as server_name
            FROM vpn_keys k
            LEFT JOIN servers s ON k.server_id = s.id
            WHERE k.user_id = $1
            ORDER BY k.created_at DESC
        ''', user_id)

async def get_key_by_id(key_id: int, user_id: int):
    """Получить информацию о ключе по ID"""
    async with get_connection() as conn:
        return await conn.fetchrow('''
            SELECT k.id, k.key_name, k.vless_link, k.vless_client_id, k.created_at, 
                   k.expires_at, k.traffic_gb, k.is_active, k.server_id, s.name as server_name
            FROM vpn_keys k
            LEFT JOIN servers s ON k.server_id = s.id
            WHERE k.id = $1 AND k.user_id = $2
        ''', key_id, user_id)

async def get_device_apps(device_type: str):
    """Получить список приложений для устройства"""
    async with get_connection() as conn:
        return await conn.fetch('''
            SELECT id, app_name, app_url, display_order
            FROM device_apps
            WHERE device_type = $1 AND is_active = TRUE
            ORDER BY display_order, id
        ''', device_type)

async def get_device_instruction_photos(device_type: str):
    """Получить все фото инструкции для устройства"""
    async with get_connection() as conn:
        photos = await conn.fetch('''
            SELECT photo_id
            FROM device_instruction_photos
            WHERE device_type = $1
            ORDER BY display_order, id
        ''', device_type)
        return [row['photo_id'] for row in photos] if photos else []

async def add_device_instruction_photo(device_type: str, photo_id: str):
    """Добавить фото инструкции для устройства"""
    async with get_connection() as conn:
        # Получаем максимальный порядок для этого устройства
        max_order = await conn.fetchval('''
            SELECT COALESCE(MAX(display_order), -1)
            FROM device_instruction_photos
            WHERE device_type = $1
        ''', device_type)
        new_order = max_order + 1
        
        await conn.execute('''
            INSERT INTO device_instruction_photos (device_type, photo_id, display_order)
            VALUES ($1, $2, $3)
        ''', device_type, photo_id, new_order)

async def delete_device_instruction_photo(photo_db_id: int):
    """Удалить фото инструкции по ID записи в БД"""
    async with get_connection() as conn:
        await conn.execute('DELETE FROM device_instruction_photos WHERE id = $1', photo_db_id)

async def get_device_instruction_photos_list(device_type: str):
    """Получить список фото с ID для управления"""
    async with get_connection() as conn:
        photos = await conn.fetch('''
            SELECT id, photo_id, display_order
            FROM device_instruction_photos
            WHERE device_type = $1
            ORDER BY display_order, id
        ''', device_type)
        return photos if photos else []

DEVICE_TYPES = {
    "iphone": "📱 iPhone",
    "android": "🤖 Android",
    "windows": "🪟 Windows",
    "macbook": "💻 MacBook",
    "linux": "🐧 Linux"
}

# ==================== УПРАВЛЕНИЕ КЛЮЧАМИ ====================

@dp.callback_query(F.data == "manage_keys")
async def handle_manage_keys(callback: CallbackQuery):
    """Раздел управления ключами отключён для пользователей."""
    await safe_callback_answer(
        callback,
        "🔒 Управление отдельными ключами больше недоступно.\n"
        "Используйте кнопку «🔗 Получить VPN» и импортируйте подписку в приложение.",
        show_alert=True,
    )

@dp.callback_query(F.data == "create_key")
async def handle_create_key(callback: CallbackQuery, state: FSMContext):
    """Создание ключей пользователем отключено."""
    await safe_callback_answer(
        callback,
        "🔒 Создание отдельных ключей больше недоступно.\n"
        "Теперь достаточно один раз импортировать ссылку «🔗 Получить VPN» в приложение.",
        show_alert=True,
    )

@dp.callback_query(F.data.startswith("key_server:"))
async def handle_key_server_selection(callback: CallbackQuery, state: FSMContext):
    """Создание/выбор сервера для ключа отключено."""
    await safe_callback_answer(
        callback,
        "🔒 Создание ключей больше недоступно.\n"
        "Импортируйте ссылку «🔗 Получить VPN» в приложение, и оно само создаст профили серверов.",
        show_alert=True,
    )

@dp.message(KeyManagementStates.ENTERING_KEY_NAME)
async def handle_key_name_input(message: Message, state: FSMContext):
    """Обработка названия ключа - больше не используется, но оставляем для совместимости"""
    # Эта функция больше не используется, так как мы сразу создаем ключ после выбора сервера
    await message.answer("❌ Эта функция больше не используется. Выберите сервер заново через раздел '🔑 Ключи ВПН'.")
    await state.clear()

@dp.callback_query(F.data == "view_key_list")
async def handle_view_key_list(callback: CallbackQuery):
    """Показать список ключей для просмотра"""
    user_id = callback.from_user.id
    keys = await get_user_keys(user_id)
    
    if not keys:
        await callback.answer("У вас нет ключей", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for key_id, key_name, vless_link, created_at, expires_at, traffic_gb, is_active, server_name in keys:
        name = key_name or f"Ключ #{key_id}"
        builder.row(InlineKeyboardButton(
            text=f"{'✅' if is_active else '❌'} {name}",
            callback_data=f"view_key:{key_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="manage_keys"))
    
    await callback.message.edit_text(
        "🔑 <b>Выберите ключ для просмотра:</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("view_key:"))
async def handle_view_key(callback: CallbackQuery):
    """Просмотр информации о ключе"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    
    status = "✅ Активен" if is_active else "❌ Неактивен"
    name = key_name or f"Ключ #{key_id_db}"
    
    text = (
        f"🔑 <b>{name}</b>\n\n"
        f"Статус: <i>{status}</i>\n"
        f"Сервер: <i>{server_name or 'Неизвестно'}</i>\n"
    )
    
    if created_at:
        try:
            created = datetime.strptime(created_at.split()[0], "%Y-%m-%d").strftime("%d.%m.%Y")
            text += f"Создан: <i>{created}</i>\n"
        except:
            pass
    
    if expires_at:
        try:
            expires = datetime.strptime(expires_at, "%Y-%m-%d").strftime("%d.%m.%Y")
            text += f"Истекает: <i>{expires}</i>\n"
        except:
            pass
    
    if traffic_gb:
        text += f"Трафик: <i>{traffic_gb} ГБ</i>\n"
    subscription_url = await get_user_subscription_url(user_id)
    text += f"\nМы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_key:{key_id_db}"))
    builder.row(InlineKeyboardButton(text="🔄 Заменить", callback_data=f"replace_key:{key_id_db}"))
    builder.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="manage_keys"))
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("how_to_connect:"))
async def handle_how_to_connect(callback: CallbackQuery):
    """Обработчик кнопки 'Как подключиться?'"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for device_key, device_name in DEVICE_TYPES.items():
        builder.row(InlineKeyboardButton(
            text=device_name,
            callback_data=f"connect_device:{device_key}:{key_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"view_key:{key_id}"))
    
    await callback.message.edit_text(
        "📱 <b>Как подключиться?</b>\n\n"
        "С помощью кнопок выберите ваше устройство:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("connect_device:"))
async def handle_connect_device(callback: CallbackQuery):
    """Обработчик выбора устройства для подключения"""
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    device_type = parts[1]
    key_id = int(parts[2])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    
    # Получаем приложения для устройства
    apps = await get_device_apps(device_type)
    
    if not apps:
        await callback.answer(
            "❌ Приложения для этого устройства пока не настроены. Обратитесь к администратору.",
            show_alert=True
        )
        return
    
    device_name = DEVICE_TYPES.get(device_type, device_type)
    
    # Получаем все фото инструкции, если есть
    photo_ids = await get_device_instruction_photos(device_type)
    
    # Формируем текст инструкции
    text = f"📱 <b>Инструкция по подключению для {device_name}</b>\n\n"
    
    # Добавляем приложения с кнопками-ссылками
    builder = InlineKeyboardBuilder()
    for app in apps:
        app_name = app['app_name']
        app_url = app['app_url']
        builder.row(InlineKeyboardButton(
            text=f"⬇️ Скачать {app_name}",
            url=app_url
        ))
    
    # Добавляем инструкцию
    subscription_url = await get_user_subscription_url(user_id)
    text += (
        "📋 <b>Инструкция:</b>\n\n"
        "1️⃣ Скачайте приложение (кнопки ниже)\n\n"
        "2️⃣ Добавьте ссылку как <b>подписку</b>:\n"
        f"<code>{subscription_url}</code>\n\n"
        "3️⃣ Обновите/синхронизируйте подписку в приложении\n"
        "4️⃣ Нажмите кнопку включения VPN в приложении\n\n"
        "✅ Готово! VPN активирован."
    )
    
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"how_to_connect:{key_id}"))
    
    # Если есть фото, отправляем их
    if photo_ids:
        try:
            await callback.message.delete()
            # Ограничиваем количество фото до 10 (лимит Telegram API для медиагрупп)
            photos_to_send = photo_ids[:10]
            
            # Ограничиваем длину подписи до 1024 символов (лимит Telegram API)
            caption_text = text
            if len(caption_text) > 1024:
                caption_text = caption_text[:1021] + "..."
            
            # Если одно фото, отправляем с подписью
            if len(photos_to_send) == 1:
                await bot.send_photo(
                    chat_id=callback.message.chat.id,
                    photo=photos_to_send[0],
                    caption=caption_text,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup()
                )
            else:
                # Если несколько фото, отправляем медиагруппу (максимум 10 фото)
                media_group = []
                for i, photo_id in enumerate(photos_to_send):
                    if i == 0:
                        # Первое фото с подписью
                        media_group.append(InputMediaPhoto(media=photo_id, caption=caption_text, parse_mode="HTML"))
                    else:
                        # Остальные фото без подписи
                        media_group.append(InputMediaPhoto(media=photo_id))
                
                await bot.send_media_group(
                    chat_id=callback.message.chat.id,
                    media=media_group
                )
                # Отправляем кнопки отдельным сообщением
                await bot.send_message(
                    chat_id=callback.message.chat.id,
                    text="⬇️ <b>Действия:</b>",
                    parse_mode="HTML",
                    reply_markup=builder.as_markup()
                )
                
                # Если фото больше 10, отправляем остальные отдельными сообщениями
                if len(photo_ids) > 10:
                    for photo_id in photo_ids[10:]:
                        try:
                            await bot.send_photo(
                                chat_id=callback.message.chat.id,
                                photo=photo_id
                            )
                        except Exception as e:
                            logger.error(f"Error sending additional photo: {e}")
        except TelegramBadRequest as e:
            logger.error(f"TelegramBadRequest sending photo instruction: {e}")
            # Если не получилось отправить фото, отправляем просто текст
            try:
                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                    disable_web_page_preview=True
                )
            except:
                await bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Error sending photo instruction: {e}")
            # Если не получилось отправить фото, отправляем просто текст
            try:
                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                    disable_web_page_preview=True
                )
            except:
                await bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                    disable_web_page_preview=True
                )
    else:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True
        )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("delete_key:"))
async def handle_delete_key(callback: CallbackQuery, state: FSMContext):
    """Удаление ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    name = key_name or f"Ключ #{key_id_db}"
    
    await state.update_data(key_to_delete=key_id_db, key_client_id=vless_client_id, key_server_id=server_id)
    await state.set_state(KeyManagementStates.CONFIRMING_DELETE)
    
    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Вы уверены, что хотите удалить ключ <b>{name}</b>?\n\n"
        f"Это действие нельзя отменить.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete:{key_id_db}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_key:{key_id_db}")]
        ])
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("confirm_delete:"))
async def handle_confirm_delete(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        await state.clear()
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    name = key_name or f"Ключ #{key_id_db}"
    
    # Получаем данные сервера для удаления клиента
    server_data = await get_server_by_id(server_id)
    if server_data:
        server_id_db, server_name, server_ip, server_username, server_password, server_inbound_id, server_base_url = server_data
        
        # Удаляем клиент с сервера
        try:
            server_client = XUIClient(
                base_url=server_base_url,
                username=server_username,
                password=server_password,
                inbound_id=server_inbound_id
            )
            server_client.delete_client(vless_client_id)
            logger.info(f"Successfully deleted client {vless_client_id} from server {server_id}")
        except Exception as e:
            logger.error(f"Failed to delete client from server: {e}")
            # Продолжаем удаление из БД даже если не удалось удалить с сервера
    
    # Удаляем ключ из БД
    async with get_connection() as conn:
        await conn.execute('DELETE FROM vpn_keys WHERE id = $1 AND user_id = $2', key_id_db, user_id)
    
    await callback.message.edit_text(
        f"✅ Ключ <b>{name}</b> успешно удален!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к ключам", callback_data="manage_keys")]
        ])
    )
    await safe_callback_answer(callback)
    await state.clear()

@dp.callback_query(F.data.startswith("replace_key:"))
async def handle_replace_key(callback: CallbackQuery, state: FSMContext):
    """Замена ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = await get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    # При замене ключа мы удаляем старый и создаем новый, поэтому лимит не проверяем
    
    # Сохраняем ID ключа для замены
    await state.update_data(key_to_replace=key_id)
    
    # Показываем выбор сервера
    active_servers = await get_active_servers()
    if not active_servers:
        await callback.answer("❌ Нет доступных серверов", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for server_id, server_name, server_ip, inbound_id in active_servers:
        builder.row(InlineKeyboardButton(
            text=f"🖥️ {server_name}",
            callback_data=f"replace_key_server:{server_id}:{key_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"view_key:{key_id}"))
    
    await callback.message.edit_text(
        "🔄 <b>Замена ключа</b>\n\n"
        "Выберите сервер для нового ключа:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("replace_key_server:"))
async def handle_replace_key_server(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора сервера для замены ключа"""
    parts = callback.data.split(":")
    server_id = int(parts[1])
    old_key_id = int(parts[2])
    
    await state.update_data(selected_server_id=server_id, key_to_replace=old_key_id)
    
    # Сразу создаем ключ (используем ту же логику, что и при создании нового)
    # Вызываем обработчик выбора сервера, который теперь создает ключ сразу
    await handle_key_server_selection(callback, state)

# ==================== АДМИНСКИЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ СЕРВЕРАМИ ====================

@dp.message(Command("add_server"))
async def cmd_add_server(message: Message, state: FSMContext):
    """Команда для добавления нового сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    await message.answer(
        "🔧 <b>Добавление нового сервера</b>\n\n"
        "Введите название сервера (будет видно пользователям):",
        parse_mode="HTML"
    )
    await state.set_state(AddServerSteps.WAITING_NAME)

@dp.message(AddServerSteps.WAITING_NAME)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка названия сервера"""
    await state.update_data(name=message.text)
    await message.answer(
        "🔗 Введите полную ссылку на панель 3x-ui:\n\n"
        "Примеры:\n"
        "• <code>http://79.137.204.85:8080/</code>\n"
        "• <code>http://176.109.105.175:8080/YF0nOS5FD5nBM5MmWq/</code>\n"
        "• <code>https://example.com:54321/</code>\n\n"
        "⚠️ Важно: Укажите полную ссылку, включая протокол (http:// или https://), "
        "адрес, порт и путь (если есть).",
        parse_mode="HTML"
    )
    await state.set_state(AddServerSteps.WAITING_PANEL_URL)

@dp.message(AddServerSteps.WAITING_PANEL_URL)
async def process_server_panel_url(message: Message, state: FSMContext):
    """Обработка ссылки на панель"""
    from urllib.parse import urlparse
    
    panel_url = message.text.strip()
    
    # Убеждаемся, что URL заканчивается на /
    if not panel_url.endswith('/'):
        panel_url += '/'
    
    # Парсим URL
    try:
        parsed = urlparse(panel_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Неверный формат URL")
        
        protocol = parsed.scheme.lower()
        if protocol not in ['http', 'https']:
            await message.answer("❌ Поддерживаются только протоколы HTTP и HTTPS. Попробуйте снова:")
            return

        # Извлекаем IP/домен и порт
        netloc = parsed.netloc
        if ':' in netloc:
            host, port_str = netloc.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                await message.answer("❌ Неверный формат порта. Попробуйте снова:")
                return
        else:
            # Если порт не указан, используем стандартный
            host = netloc
            port = 443 if protocol == 'https' else 80
        
        # Путь из URL
        path = parsed.path
        
        # Формируем base_url (убираем путь из base_url, так как он будет использоваться в запросах)
        # Но сохраняем полный URL для отображения
        base_url = f"{protocol}://{host}:{port}{path}".rstrip('/')
        
        # Извлекаем IP или домен
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
            f"Путь: <i>{path if path else '/'}</i>\n"
            f"Base URL: <i>{base_url}</i>\n\n"
            f"Введите username для панели 3x-ui:",
            parse_mode="HTML"
        )
        await state.set_state(AddServerSteps.WAITING_USERNAME)
        
    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка парсинга URL:</b>\n<code>{str(e)}</code>\n\n"
            f"Пожалуйста, введите полную ссылку в формате:\n"
            f"<code>http://IP:ПОРТ/ПУТЬ/</code>\n\n"
            f"Пример: <code>http://79.137.204.85:8080/</code>",
            parse_mode="HTML"
        )

@dp.message(AddServerSteps.WAITING_USERNAME)
async def process_server_username(message: Message, state: FSMContext):
    """Обработка username"""
    await state.update_data(username=message.text)
    await message.answer("Введите password для панели 3x-ui:")
    await state.set_state(AddServerSteps.WAITING_PASSWORD)

@dp.message(AddServerSteps.WAITING_PASSWORD)
async def process_server_password(message: Message, state: FSMContext):
    """Обработка password"""
    await state.update_data(password=message.text)
    await message.answer("Введите Inbound ID (число):")
    await state.set_state(AddServerSteps.WAITING_INBOUND_ID)

@dp.message(AddServerSteps.WAITING_INBOUND_ID)
async def process_server_inbound_id(message: Message, state: FSMContext):
    """Обработка Inbound ID"""
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
        test_client.login()
        await message.answer(
            f"✅ <b>Подключение к серверу успешно!</b>\n\n"
            f"<b>Данные сервера:</b>\n"
            f"Название: <i>{name}</i>\n"
            f"IP: <i>{ip}</i>\n"
            f"Протокол: <i>{protocol.upper()}</i>\n"
            f"Порт: <i>{port}</i>\n"
            f"Base URL: <i>{base_url}</i>\n"
            f"Username: <i>{username}</i>\n"
            f"Inbound ID: <i>{inbound_id}</i>\n\n"
            f"Сохранить этот сервер? (да/нет)",
            parse_mode="HTML"
        )
        await state.update_data(inbound_id=inbound_id)
        await state.set_state(AddServerSteps.CONFIRMING)
    except Exception as e:
        error_msg = str(e)
        # Предлагаем попробовать другой протокол при SSL ошибке
        if "SSL" in error_msg or "WRONG_VERSION_NUMBER" in error_msg:
            suggestion = "\n\n💡 <b>Совет:</b> Попробуйте использовать HTTP вместо HTTPS. Используйте /add_server для повторного ввода."
        else:
            suggestion = "\n\nПроверьте данные и попробуйте снова. Используйте /add_server для повторного ввода."
        
        await message.answer(
            f"❌ <b>Ошибка подключения к серверу:</b>\n<code>{error_msg}</code>{suggestion}"
        )
        await state.clear()

@dp.message(AddServerSteps.CONFIRMING)
async def process_server_confirmation(message: Message, state: FSMContext):
    """Обработка подтверждения добавления сервера"""
    if message.text.lower() not in ['да', 'yes', 'y', 'д']:
        await message.answer("❌ Добавление сервера отменено.")
        await state.clear()
        return
    
    data = await state.get_data()
    name = data.get('name')
    ip = data.get('ip')
    port = data.get('port', 54321)
    protocol = data.get('protocol', 'https')
    username = data.get('username')
    password = data.get('password')
    base_url = data.get('base_url')
    inbound_id = data.get('inbound_id')
    
    # Сохраняем сервер в БД
    async with get_connection() as conn:
        # Синхронизируем последовательность с максимальным id в таблице
        await conn.execute('''
            SELECT setval('servers_id_seq', COALESCE((SELECT MAX(id) FROM servers), 0) + 1, false)
        ''')
        server_id = await conn.fetchval('''
            INSERT INTO servers (name, ip, port, protocol, username, password, inbound_id, base_url, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
            RETURNING id
        ''', name, ip, port, protocol, username, password, inbound_id, base_url)
    
    # Автоматически создаём ключи для нового сервера для всех активных пользователей
    try:
        active_users = await conn.fetch('''
            SELECT user_id
            FROM users
            WHERE pay_subscribed = TRUE 
              AND subscription_end IS NOT NULL
              AND DATE(subscription_end) >= CURRENT_DATE
        ''')
        
        if active_users:
            logger.info(f"Creating keys for {len(active_users)} active users on new server {name} (ID: {server_id})")
            # Создаём ключи в фоне, чтобы не блокировать ответ админу
            for user_row in active_users:
                user_id = user_row['user_id']
                try:
                    asyncio.create_task(create_keys_for_all_servers(user_id))
                except Exception as e:
                    logger.error(f"Failed to create keys for user {user_id} on new server: {e}")
    except Exception as e:
        logger.error(f"Error creating keys for active users on new server: {e}")
    
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
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    async with get_connection() as conn:
        servers = await conn.fetch('''
            SELECT id, name, ip, is_active 
            FROM servers 
            ORDER BY id
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
        status = "✅ Активен" if is_active else "❌ Неактивен"
        text += f"{server_id}. <b>{name}</b> ({ip})\n   {status}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_add_server")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh_servers")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("toggle_server"))
async def cmd_toggle_server(message: Message):
    """Активация/деактивация сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /toggle_server <server_id>")
        return
    
    try:
        server_id = int(args[1])
    except ValueError:
        await message.answer("❌ Server ID должен быть числом.")
        return
    
    async with get_connection() as conn:
        # Получаем текущий статус
        result = await conn.fetchrow('SELECT is_active FROM servers WHERE id = $1', server_id)
        
        if not result:
            await message.answer(f"❌ Сервер с ID {server_id} не найден.")
            return
        
        current_status = result['is_active']
        new_status = not current_status
        
        await conn.execute('''
            UPDATE servers 
            SET is_active = $1, updated_at = CURRENT_TIMESTAMP
            WHERE id = $2
        ''', new_status, server_id)
        
        status_text = "активирован" if new_status else "деактивирован"
        await message.answer(f"✅ Сервер {server_id} {status_text}.")

@dp.message(Command("delete_server"))
async def cmd_delete_server(message: Message):
    """Удаление сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /delete_server <server_id>")
        return
    
    try:
        server_id = int(args[1])
    except ValueError:
        await message.answer("❌ Server ID должен быть числом.")
        return
    
    async with get_connection() as conn:
        # Проверяем, используется ли сервер
        users_count = await conn.fetchval('SELECT COUNT(*) FROM users WHERE server_id = $1', server_id)
        
        if users_count > 0:
            await message.answer(
                f"❌ Нельзя удалить сервер, который используется {users_count} пользователями.\n"
                f"Сначала деактивируйте сервер: /toggle_server {server_id}"
            )
            return
        
        result = await conn.execute('DELETE FROM servers WHERE id = $1', server_id)
        
        if result and 'DELETE' in result:
            await message.answer(f"✅ Сервер {server_id} удален.")
        else:
            await message.answer(f"❌ Сервер с ID {server_id} не найден.")

async def handle_expired_subscriptions():
    """Обрабатывает истекшие подписки: удаляет ключи и отправляет уведомления"""
    logger.info("Checking for expired subscriptions...")
    
    try:
        async with get_connection() as conn:
            # Находим пользователей с истекшими подписками
            # Используем DATE() для корректного сравнения дат (без учета времени)
            expired_users = await conn.fetch('''
                SELECT user_id, subscription_end
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) < CURRENT_DATE
            ''')
            
            if not expired_users:
                logger.info("No expired subscriptions found")
                return
            
            processed_count = 0
            notified_count = 0
            error_count = 0
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_end = user['subscription_end']
                
                try:
                    # Получаем количество ключей пользователя перед удалением
                    keys_count = await conn.fetchval('''
                        SELECT COUNT(*) FROM vpn_keys 
                        WHERE user_id = $1 AND is_active = TRUE
                    ''', user_id)
                    
                    # Удаляем все ключи пользователя из БД
                    deleted_keys = await conn.execute('''
                        DELETE FROM vpn_keys 
                        WHERE user_id = $1
                    ''', user_id)
                    
                    # Обновляем статус подписки
                    await conn.execute('''
                        UPDATE users 
                        SET 
                            pay_subscribed = FALSE,
                            subscription_end = NULL,
                            renewal_used = FALSE 
                        WHERE user_id = $1
                    ''', user_id)
                    
                    processed_count += 1
                    
                    # Отправляем уведомление пользователю
                    if keys_count > 0:
                        try:
                            # Формируем сообщение в зависимости от количества ключей
                            if keys_count == 1:
                                keys_text = "ваш ключ был удален"
                            else:
                                keys_text = f"ваши {keys_count} ключа были удалены"
                            
                            # Форматируем дату окончания подписки
                            try:
                                if isinstance(subscription_end, str):
                                    if ' ' in subscription_end:
                                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                                    else:
                                        end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                                else:
                                    end_date = subscription_end
                                end_date_str = end_date.strftime("%d.%m.%Y")
                            except:
                                end_date_str = "недавно"
                            
                            message_text = (
                                f"⏰ <b>Ваша подписка истекла</b>\n\n"
                                f"📅 Дата окончания: <i>{end_date_str}</i>\n\n"
                                f"🔑 {keys_text.capitalize()} из-за окончания подписки.\n\n"
                                f"💳 Чтобы вернуть доступ, необходимо купить подписку.\n\n"
                                f"Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
                            )
                            
                            builder = InlineKeyboardBuilder()
                            builder.row(InlineKeyboardButton(text="💳 Подписка", callback_data="open_premium"))
                            
                            await bot.send_message(
                                user_id,
                                message_text,
                                reply_markup=builder.as_markup(),
                                parse_mode="HTML"
                            )
                            notified_count += 1
                        except Exception as e:
                            logger.error(f"Failed to send notification to user {user_id}: {e}")
                            error_count += 1
                    
                except Exception as e:
                    logger.error(f"Error processing expired subscription for user {user_id}: {e}")
                    error_count += 1
            
            logger.info(
                f"Expired subscriptions processed: {processed_count}, "
                f"notifications sent: {notified_count}, errors: {error_count}"
            )
            
    except Exception as e:
        logger.error(f"Error in handle_expired_subscriptions: {e}", exc_info=True)

async def create_keys_for_all_servers(user_id: int):
    """
    Автоматически создаёт ключи для всех активных серверов для пользователя.
    Вызывается при активации новой подписки.
    """
    try:
        async with get_connection() as conn:
            # Получаем информацию о подписке пользователя
            user_data = await conn.fetchrow('''
                SELECT subscription_end, pay_subscribed
                FROM users
                WHERE user_id = $1
                  AND pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
            ''', user_id)
            
            if not user_data:
                return
            
            subscription_end = user_data['subscription_end']
            
            # Парсим дату окончания подписки
            try:
                if isinstance(subscription_end, str):
                    if ' ' in subscription_end:
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                # Вычисляем expiry_time в миллисекундах (конец дня)
                from datetime import time as dt_time
                end_datetime = datetime.combine(end_date.date(), dt_time(23, 59, 59))
                expiry_time_unix_ms = int(end_datetime.timestamp() * 1000)
                expires_at = end_date.date() if isinstance(end_date, datetime) else end_date
            except Exception as e:
                logger.error(f"Error parsing subscription_end for user {user_id}: {e}")
                return
            
            # Получаем все активные серверы
            active_servers = await conn.fetch('''
                SELECT id, name, ip, username, password, inbound_id, base_url
                FROM servers 
                WHERE is_active = TRUE
                ORDER BY name
            ''')
            
            if not active_servers:
                logger.info(f"No active servers found for user {user_id}")
                return
            
            # Получаем серверы, для которых у пользователя уже есть ключи
            existing_keys = await conn.fetch('''
                SELECT DISTINCT server_id
                FROM vpn_keys
                WHERE user_id = $1
                  AND is_active = TRUE
            ''', user_id)
            
            existing_server_ids = {row['server_id'] for row in existing_keys if row.get('server_id')}
            created_count = 0
            
            # Создаём ключи для серверов, для которых их нет
            for server in active_servers:
                server_id = server['id']
                if server_id not in existing_server_ids:
                    try:
                        server_name = server['name']
                        server_base_url = server['base_url']
                        server_username = server['username']
                        server_password = server['password']
                        server_inbound_id = server['inbound_id']
                        
                        # Создаём клиента на сервере
                        server_client = XUIClient(
                            base_url=server_base_url,
                            username=server_username,
                            password=server_password,
                            inbound_id=server_inbound_id
                        )
                        
                        # Создаём клиента без лимита трафика (безлимит)
                        result = server_client.add_vless_client(
                            telegram_user_id=user_id,
                            display_name="temp",  # Временно, будет заменено после получения key_id
                            traffic_gb=None,  # None = безлимит
                            expiry_time_unix_ms=expiry_time_unix_ms,
                        )
                        
                        vless_client_id = result.get("id")
                        vless_link = result.get("link")
                        
                        if vless_client_id and vless_link:
                            # Синхронизируем последовательность
                            try:
                                max_id = await conn.fetchval('SELECT MAX(id) FROM vpn_keys')
                                if max_id is not None:
                                    await conn.execute(f"SELECT setval('vpn_keys_id_seq', {max_id + 1}, false)")
                            except Exception:
                                pass
                            
                            # Сохраняем ключ в БД
                            key_id = await conn.fetchval('''
                                INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link, 
                                                    key_name, expires_at, traffic_gb, is_active)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                                RETURNING id
                            ''', user_id, server_id, vless_client_id, vless_link, None, expires_at, None)
                            
                            # Обновляем key_name и vless_link с правильными значениями
                            if key_id:
                                key_name = f"{server_name} #{key_id}"
                                # Обновляем VLESS ссылку: формат #ServerName#vpn_keyID
                                if '#' in vless_link:
                                    vless_link = vless_link.rsplit('#', 1)[0] + f"#{server_name}#{key_id}"
                                else:
                                    vless_link = vless_link + f"#{server_name}#{key_id}"
                                
                                await conn.execute('''
                                    UPDATE vpn_keys 
                                    SET key_name = $1, vless_link = $2
                                    WHERE id = $3
                                ''', key_name, vless_link, key_id)
                                
                                created_count += 1
                                logger.info(f"Auto-created key {key_id} for user {user_id} on server {server_name}")
                    except Exception as e:
                        logger.error(f"Failed to auto-create key for user {user_id} on server {server.get('name', 'unknown')}: {e}")
            
            if created_count > 0:
                logger.info(f"Created {created_count} keys for user {user_id} on {len(active_servers)} servers")
    
    except Exception as e:
        logger.error(f"Error in create_keys_for_all_servers for user {user_id}: {e}")

async def sync_user_keys(user_id: int):
    """Синхронизирует ключи одного пользователя с датой окончания его подписки"""
    try:
        async with get_connection() as conn:
            # Получаем информацию о подписке пользователя
            user_data = await conn.fetchrow('''
                SELECT subscription_end, pay_subscribed
                FROM users
                WHERE user_id = $1
                  AND pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
            ''', user_id)
            
            if not user_data:
                return
            
            subscription_end = user_data['subscription_end']
            
            # Парсим дату окончания подписки
            try:
                if isinstance(subscription_end, str):
                    if ' ' in subscription_end:
                        sub_end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        sub_end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                else:
                    sub_end_date = subscription_end
                
                # Вычисляем expiry_time в миллисекундах (конец дня)
                from datetime import time as dt_time
                sub_end_datetime = datetime.combine(sub_end_date.date(), dt_time(23, 59, 59))
                subscription_expiry_ms = int(sub_end_datetime.timestamp() * 1000)
                
                # Получаем все активные ключи пользователя
                keys = await conn.fetch('''
                    SELECT k.id, k.server_id, k.vless_client_id, k.expires_at, s.name, s.base_url, 
                           s.username, s.password, s.inbound_id
                    FROM vpn_keys k
                    LEFT JOIN servers s ON k.server_id = s.id
                    WHERE k.user_id = $1 AND k.is_active = TRUE
                ''', user_id)
                
                for key_data in keys:
                    key_id = key_data['id']
                    server_id = key_data['server_id']
                    vless_client_id = key_data['vless_client_id']
                    key_expires_at = key_data['expires_at']
                    server_name = key_data['name']
                    server_base_url = key_data['base_url']
                    server_username = key_data['username']
                    server_password = key_data['password']
                    server_inbound_id = key_data['inbound_id']
                    
                    if not server_id or not vless_client_id:
                        continue
                    
                    # Парсим дату истечения ключа
                    try:
                        if isinstance(key_expires_at, str):
                            if ' ' in key_expires_at:
                                key_end_date = datetime.strptime(key_expires_at.split()[0], "%Y-%m-%d")
                            else:
                                key_end_date = datetime.strptime(key_expires_at, "%Y-%m-%d")
                        else:
                            key_end_date = key_expires_at
                        
                        # Вычисляем expiry_time ключа в миллисекундах
                        key_end_datetime = datetime.combine(key_end_date.date(), dt_time(23, 59, 59))
                        key_expiry_ms = int(key_end_datetime.timestamp() * 1000)
                        
                        # Если подписка продлена (дата окончания подписки > дата истечения ключа)
                        if subscription_expiry_ms > key_expiry_ms:
                            # Обновляем ключ в панели x-ui
                            try:
                                server_client = XUIClient(
                                    base_url=server_base_url,
                                    username=server_username,
                                    password=server_password,
                                    inbound_id=server_inbound_id
                                )
                                
                                server_client.update_client_expiry(
                                    client_id=vless_client_id,
                                    expiry_time_unix_ms=subscription_expiry_ms
                                )
                                
                                # Обновляем дату истечения ключа в БД
                                new_expires_at = sub_end_date.strftime("%Y-%m-%d")
                                await conn.execute('''
                                    UPDATE vpn_keys
                                    SET expires_at = $1
                                    WHERE id = $2
                                ''', new_expires_at, key_id)
                                
                                logger.info(f"Updated key {key_id} (client {vless_client_id}) for user {user_id} "
                                          f"from {key_expires_at} to {subscription_end}")
                                
                            except Exception as e:
                                logger.error(f"Failed to update key {key_id} for user {user_id}: {e}")
                        
                    except Exception as e:
                        logger.error(f"Error parsing key expiry date for key {key_id}: {e}")
            
            except Exception as e:
                logger.error(f"Error processing user {user_id} keys sync: {e}")
    
    except Exception as e:
        logger.error(f"Error in sync_user_keys for user {user_id}: {e}")

async def sync_subscriptions_and_keys():
    """Синхронизирует подписки и ключи: продлевает ключи до даты окончания подписки"""
    logger.info("Starting subscription and keys synchronization...")
    
    try:
        async with get_connection() as conn:
            # Получаем всех пользователей с активными подписками
            users = await conn.fetch('''
                SELECT user_id, subscription_end, pay_subscribed
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND subscription_end >= CURRENT_DATE
            ''')
            
            updated_count = 0
            error_count = 0
            
            for user in users:
                user_id = user['user_id']
                subscription_end = user['subscription_end']
                pay_subscribed = user['pay_subscribed']
                try:
                    # Парсим дату окончания подписки
                    if isinstance(subscription_end, str):
                        if ' ' in subscription_end:
                            sub_end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            sub_end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                    else:
                        sub_end_date = subscription_end
                    
                    # Вычисляем expiry_time в миллисекундах (конец дня)
                    from datetime import time as dt_time
                    sub_end_datetime = datetime.combine(sub_end_date.date(), dt_time(23, 59, 59))
                    subscription_expiry_ms = int(sub_end_datetime.timestamp() * 1000)
                    
                    # Получаем все активные ключи пользователя
                    keys = await conn.fetch('''
                        SELECT k.id, k.server_id, k.vless_client_id, k.expires_at, s.name, s.base_url, 
                               s.username, s.password, s.inbound_id
                        FROM vpn_keys k
                        LEFT JOIN servers s ON k.server_id = s.id
                        WHERE k.user_id = $1 AND k.is_active = TRUE
                    ''', user_id)
                    
                    for key_data in keys:
                        key_id = key_data['id']
                        server_id = key_data['server_id']
                        vless_client_id = key_data['vless_client_id']
                        key_expires_at = key_data['expires_at']
                        server_name = key_data['name']
                        server_base_url = key_data['base_url']
                        server_username = key_data['username']
                        server_password = key_data['password']
                        server_inbound_id = key_data['inbound_id']
                        
                        if not server_id or not vless_client_id:
                            continue
                        
                        # Парсим дату истечения ключа
                        try:
                            if isinstance(key_expires_at, str):
                                if ' ' in key_expires_at:
                                    key_end_date = datetime.strptime(key_expires_at.split()[0], "%Y-%m-%d")
                                else:
                                    key_end_date = datetime.strptime(key_expires_at, "%Y-%m-%d")
                            else:
                                key_end_date = key_expires_at
                            
                            # Вычисляем expiry_time ключа в миллисекундах
                            key_end_datetime = datetime.combine(key_end_date.date(), dt_time(23, 59, 59))
                            key_expiry_ms = int(key_end_datetime.timestamp() * 1000)
                            
                            # Если подписка продлена (дата окончания подписки > дата истечения ключа)
                            if subscription_expiry_ms > key_expiry_ms:
                                # Обновляем ключ в панели x-ui
                                try:
                                    server_client = XUIClient(
                                        base_url=server_base_url,
                                        username=server_username,
                                        password=server_password,
                                        inbound_id=server_inbound_id
                                    )
                                    
                                    server_client.update_client_expiry(
                                        client_id=vless_client_id,
                                        expiry_time_unix_ms=subscription_expiry_ms
                                    )
                                    
                                    # Обновляем дату истечения ключа в БД
                                    new_expires_at = sub_end_date.strftime("%Y-%m-%d")
                                    await conn.execute('''
                                        UPDATE vpn_keys
                                        SET expires_at = $1
                                        WHERE id = $2
                                    ''', new_expires_at, key_id)
                                    
                                    updated_count += 1
                                    logger.info(f"Updated key {key_id} (client {vless_client_id}) for user {user_id} "
                                              f"from {key_expires_at} to {subscription_end}")
                                    
                                except Exception as e:
                                    error_count += 1
                                    logger.error(f"Failed to update key {key_id} for user {user_id}: {e}")
                            
                        except Exception as e:
                            logger.error(f"Error parsing key expiry date for key {key_id}: {e}")
                            error_count += 1
                
                except Exception as e:
                    logger.error(f"Error processing user {user_id}: {e}")
                    error_count += 1
            
            logger.info(f"Subscription and keys sync completed: {updated_count} keys updated, {error_count} errors")
    
    except Exception as e:
        logger.error(f"Error in sync_subscriptions_and_keys: {e}")

async def send_feedback_request():
    """Отправляет опрос о качестве VPN через 3 дня после покупки подписки"""
    logger.info("Starting feedback requests...")
    
    try:
        async with get_connection() as conn:
            # Создаем таблицу feedback_ratings, если её нет
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS feedback_ratings (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    payment_id INTEGER,
                    rating INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Получаем платежи, которые были совершены 3 дня назад
            users_to_notify = await conn.fetch('''
                SELECT DISTINCT p.user_id, u.username, u.first_name
                FROM payments p
                JOIN users u ON p.user_id = u.user_id
                WHERE p.status = 'completed'
                  AND p.plan_type = 'subscription'
                  AND DATE(p.timestamp) = CURRENT_DATE - INTERVAL '3 days'
                  AND p.id NOT IN (
                      SELECT payment_id FROM feedback_ratings 
                      WHERE payment_id IS NOT NULL
                  )
            ''')
            
            for user_id, username, first_name in users_to_notify:
                try:
                    # Проверяем, что у пользователя все еще активная подписка
                    sub_check = await conn.fetchrow('''
                        SELECT pay_subscribed, subscription_end
                        FROM users
                        WHERE user_id = $1 AND pay_subscribed = TRUE
                          AND subscription_end >= CURRENT_DATE
                    ''', user_id)
                    
                    if not sub_check:
                        continue
                    
                    # Получаем ID последнего платежа для связи с рейтингом
                    payment_result = await conn.fetchrow('''
                        SELECT id FROM payments
                        WHERE user_id = $1 AND status = 'completed'
                          AND DATE(timestamp) = CURRENT_DATE - INTERVAL '3 days'
                        ORDER BY id DESC LIMIT 1
                    ''', user_id)
                    payment_id = payment_result['id'] if payment_result else None
                    
                    # Отправляем опрос - кнопки в строку с цифрами 1-5 и звездами
                    builder = InlineKeyboardBuilder()
                    buttons = []
                    for rating in range(1, 6):
                        buttons.append(InlineKeyboardButton(
                            text=f"{rating} ⭐️",
                            callback_data=f"feedback_rating:{rating}:{payment_id or 0}"
                        ))
                    builder.row(*buttons)
                    
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            "👋 Привет! Как тебе наш VPN?\n\n"
                            "Поделись своим мнением, это поможет нам стать лучше!"
                        ),
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML"
                    )
                    
                    logger.info(f"Sent feedback request to user {user_id}")
                    
                except Exception as e:
                    logger.error(f"Failed to send feedback request to user {user_id}: {e}")
            
            logger.info(f"Feedback requests completed: {len(users_to_notify)} users notified")
    
    except Exception as e:
        logger.error(f"Error in send_feedback_request: {e}")

async def send_subscription_reminder():
    """Отправляет напоминание о скидке за 3 дня до окончания подписки"""
    logger.info("Starting subscription reminders...")
    
    try:
        async with get_connection() as conn:
            # Получаем пользователей, у которых подписка истекает через 3 дня
            users_to_remind = await conn.fetch('''
                SELECT user_id, username, first_name, subscription_end
                FROM users
                WHERE pay_subscribed = TRUE
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) = CURRENT_DATE + INTERVAL '3 days'
            ''')
            
            for user in users_to_remind:
                user_id = user['user_id']
                username = user['username']
                first_name = user['first_name']
                subscription_end = user['subscription_end']
                try:
                    # Форматируем дату окончания
                    try:
                        if isinstance(subscription_end, str):
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = subscription_end
                        end_date_str = end_date.strftime("%d.%m.%Y")
                        # Вычисляем количество дней до окончания
                        days_remaining = (end_date - datetime.now()).days
                        if days_remaining < 1:
                            days_display = "СЕГОДНЯ"
                        else:
                            days_display = f"{days_remaining} {('день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней')}"
                    except:
                        end_date_str = subscription_end
                        days_display = "?"
                    
                    # Формируем кнопки для продления подписки (как в меню премиум при остатке <= 3 дней)
                    renewal_plans = await get_renewal_plans()
                    builder = InlineKeyboardBuilder()
                    for plan_id, plan_data in renewal_plans.items():
                        builder.button(
                            text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                            callback_data=f"plan:{plan_id}"
                        )
                    builder.adjust(1)
                    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
                    
                    # Формируем текст в зависимости от дней
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
                            "Не упусти возможность продолжить пользоваться VPN по специальной цене! 🎁\n\n"
                            "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
                        ),
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML"
                    )
                    
                    logger.info(f"Sent subscription reminder to user {user_id}")
                    
                except Exception as e:
                    logger.error(f"Failed to send reminder to user {user_id}: {e}")
            
            logger.info(f"Subscription reminders completed: {len(users_to_remind)} users notified")
    
    except Exception as e:
        logger.error(f"Error in send_subscription_reminder: {e}")

async def cancel_expired_payments():
    """Отменяет платежи через ЮKassa, которые старше 1 часа и удаляет сообщения"""
    logger.info("Starting expired payments cancellation...")
    
    try:
        async with get_connection() as conn:
            # Находим все платежи со статусом pending, которые старше 1 часа
            expired_payments = await conn.fetch('''
                SELECT id, user_id, message_id, yookassa_payment_id, timestamp
                FROM payments
                WHERE status = 'pending'
                  AND yookassa_payment_id IS NOT NULL
                  AND timestamp < NOW() - INTERVAL '1 hour'
            ''')
            
            canceled_count = 0
            deleted_messages_count = 0
            errors_count = 0
            
            for payment in expired_payments:
                payment_id = payment['id']
                user_id = payment['user_id']
                message_id = payment['message_id']
                yookassa_payment_id = payment['yookassa_payment_id']
                
                try:
                    # Обновляем статус на canceled
                    await conn.execute('''
                        UPDATE payments 
                        SET status = 'canceled'
                        WHERE id = $1
                    ''', payment_id)
                    canceled_count += 1
                    logger.info(f"Canceled payment {payment_id} (YooKassa ID: {yookassa_payment_id}) for user {user_id}")
                    
                    # Удаляем сообщение из чата, если message_id есть
                    if message_id:
                        try:
                            await bot.delete_message(chat_id=user_id, message_id=message_id)
                            deleted_messages_count += 1
                            logger.info(f"Deleted message {message_id} for user {user_id}")
                        except Exception as e:
                            # Сообщение может быть уже удалено или недоступно
                            logger.debug(f"Could not delete message {message_id} for user {user_id}: {e}")
                    
                except Exception as e:
                    errors_count += 1
                    logger.error(f"Error canceling payment {payment_id}: {e}")
            
            # Подсчитываем и удаляем записи со статусом canceled, которые старше 7 дней
            deleted_old_count = await conn.fetchval('''
                SELECT COUNT(*) FROM payments
                WHERE status = 'canceled'
                  AND timestamp < NOW() - INTERVAL '7 days'
            ''')
            if deleted_old_count and deleted_old_count > 0:
                await conn.execute('''
                    DELETE FROM payments
                    WHERE status = 'canceled'
                      AND timestamp < NOW() - INTERVAL '7 days'
                ''')
            
            logger.info(
                f"Expired payments cancellation completed: "
                f"{canceled_count} canceled, {deleted_messages_count} messages deleted, "
                f"{errors_count} errors, {deleted_old_count} old records deleted"
            )
            
    except Exception as e:
        logger.error(f"Error in cancel_expired_payments: {e}", exc_info=True)

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
            target_date_only = target_date.date()  # Преобразуем в date объект
            
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
                username = user['username']
                first_name = user['first_name']
                subscription_end = user['subscription_end']
                try:
                    # Форматируем дату окончания
                    try:
                        if isinstance(subscription_end, str):
                            end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            end_date = subscription_end
                        end_date_str = end_date.strftime("%d.%m.%Y")
                        # Вычисляем количество дней до окончания
                        days_remaining = (end_date - datetime.now()).days
                        if days_remaining < 1:
                            days_display = "СЕГОДНЯ"
                        else:
                            days_display = f"{days_remaining} {('день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней')}"
                    except:
                        end_date_str = subscription_end
                        days_display = "?"
                    
                    # Формируем кнопки для продления подписки
                    renewal_plans = await get_renewal_plans()
                    builder = InlineKeyboardBuilder()
                    for plan_id, plan_data in renewal_plans.items():
                        builder.button(
                            text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                            callback_data=f"plan:{plan_id}"
                        )
                    builder.adjust(1)
                    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
                    
                    # Формируем текст в зависимости от дней
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
                            "Не упусти возможность продолжить пользоваться VPN по специальной цене! 🎁\n\n"
                            "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
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

@dp.callback_query(F.data == "admin_manual_reminder")
async def handle_admin_manual_reminder(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки ручной отправки напоминаний"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📅 Сегодня", callback_data="reminder_end_day:0"))
    builder.row(InlineKeyboardButton(text="📅 Завтра (+1 день)", callback_data="reminder_end_day:1"))
    builder.row(InlineKeyboardButton(text="📅 Через 3 дня", callback_data="reminder_end_day:3"))
    builder.row(InlineKeyboardButton(text="📅 Через 5 дней", callback_data="reminder_end_day:5"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"))
    
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    end_day_offset = int(callback.data.split(":")[1])
    await state.update_data(reminder_end_day=end_day_offset)
    
    # Вычисляем дату для отображения
    target_date = datetime.now() + timedelta(days=end_day_offset)
    date_str = target_date.strftime('%d.%m.%Y')
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧪 Тестовая отправка (только мне)", callback_data=f"reminder_test:{end_day_offset}"))
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
    await state.set_state(AdminManualReminderStates.CHOOSING_TIME_BEFORE)

@dp.callback_query(F.data.startswith("reminder_test:"))
async def handle_reminder_test(callback: CallbackQuery, state: FSMContext):
    """Обработчик тестовой отправки напоминания админу"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    end_day_offset = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id
    
    # Вычисляем дату для отображения
    target_date = datetime.now() + timedelta(days=end_day_offset)
    date_str = target_date.strftime('%d.%m.%Y')
    
    try:
        # Формируем тестовое уведомление
        days_remaining = end_day_offset
        if days_remaining < 1:
            days_display = "СЕГОДНЯ"
            days_text = "<b>СЕГОДНЯ</b>"
        else:
            days_display = f"{days_remaining} {('день' if days_remaining == 1 else 'дня' if 2 <= days_remaining <= 4 else 'дней')}"
            days_text = f"<b>через {days_display}</b>"
        
        # Формируем кнопки для продления подписки
        renewal_plans = await get_renewal_plans()
        builder = InlineKeyboardBuilder()
        for plan_id, plan_data in renewal_plans.items():
            builder.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
        
        # Отправляем тестовое уведомление админу
        await bot.send_message(
            chat_id=admin_id,
            text=(
                "🧪 <b>ТЕСТОВОЕ УВЕДОМЛЕНИЕ</b>\n\n"
                "⏰ <b>Напоминание о подписке</b>\n\n"
                f"Ваша VPN подписка истекает {days_text} ({date_str})\n\n"
                "🔥 <b>Сейчас действует скидка!</b>\n"
                "Успей продлить подписку сейчас и получи выгодную цену.\n\n"
                "Не упусти возможность продолжить пользоваться VPN по специальной цене! 🎁\n\n"
                "Мы перехали в https://t.me/SvoyVPN_robot?start=old1_user - быстрее пробуй новые функции и успей активировать подписку до 3 недель"
            ),
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        day_names = {0: "Сегодня", 1: "Завтра", 3: "Через 3 дня", 5: "Через 5 дней"}
        day_name = day_names.get(end_day_offset, f"Через {end_day_offset} дней")
        
        builder_back = InlineKeyboardBuilder()
        builder_back.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"reminder_end_day:{end_day_offset}"))
        
        await callback.message.edit_text(
            f"✅ <b>Тестовое уведомление отправлено</b>\n\n"
            f"📅 День окончания: {day_name} ({date_str})\n\n"
            f"Проверьте ваши сообщения!",
            reply_markup=builder_back.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
        
    except Exception as e:
        logger.error(f"Error sending test reminder: {e}", exc_info=True)
        await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("reminder_time:"))
async def handle_reminder_time(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора времени до окончания и отправка напоминаний"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    time_before_hours = float(callback.data.split(":")[1])
    data = await state.get_data()
    end_day_offset = data.get('reminder_end_day', 0)
    
    # Показываем сообщение о начале отправки
    await callback.message.edit_text(
        "⏰ <b>Отправка напоминаний...</b>\n\n"
        "Пожалуйста, подождите...",
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)
    
    # Отправляем напоминания
    sent_count, result_message = await send_manual_subscription_reminder(end_day_offset, time_before_hours)
    
    # Формируем информацию о выбранных параметрах
    target_date = datetime.now() + timedelta(days=end_day_offset)
    day_names = {0: "Сегодня", 1: "Завтра", 3: "Через 3 дня", 5: "Через 5 дней"}
    day_name = day_names.get(end_day_offset, f"Через {end_day_offset} дней")
    
    time_names = {120: "5 дней", 72: "3 дня", 24: "1 день", 3: "3 часа"}
    time_name = time_names.get(int(time_before_hours), f"{time_before_hours} часов")
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад в админ панель", callback_data="admin_panel"))
    
    await callback.message.edit_text(
        f"✅ <b>Отправка завершена</b>\n\n"
        f"📅 День окончания: {day_name} ({target_date.strftime('%d.%m.%Y')})\n"
        f"⏰ Период до окончания: {time_name}\n\n"
        f"{result_message}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("feedback_rating:"))
async def handle_feedback_rating(callback: CallbackQuery):
    """Обработчик рейтинга от пользователя"""
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    rating = int(parts[1])
    payment_id = int(parts[2]) if len(parts) > 2 else 0
    
    try:
        # Сохраняем рейтинг в БД
        async with get_connection() as conn:
            # Проверяем, существует ли таблица feedback_ratings
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS feedback_ratings (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    payment_id INTEGER,
                    rating INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Сохраняем рейтинг
            await conn.execute('''
                INSERT INTO feedback_ratings (user_id, payment_id, rating)
                VALUES ($1, $2, $3)
            ''', user_id, payment_id if payment_id > 0 else None, rating)
        
        # Отправляем благодарность
        await callback.message.edit_text(
            f"🙏 <b>Спасибо за отзыв!</b>\n\n"
            f"Вы оценили наш VPN на <b>{'⭐' * rating}</b>\n\n"
            "Ваше мнение очень важно для нас!",
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)
        
        # Отправляем результат админу
        user_info = f"@{callback.from_user.username}" if callback.from_user.username else f"ID: {user_id}"
        user_name = callback.from_user.first_name or "Пользователь"
        
        for admin_id in cfg.bot.admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"⭐ <b>Новый отзыв о VPN</b>\n\n"
                        f"Пользователь: {user_name} ({user_info})\n"
                        f"Рейтинг: <b>{'⭐' * rating}</b> ({rating}/5)\n"
                        f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to send feedback to admin {admin_id}: {e}")
        
    except Exception as e:
        logger.error(f"Error handling feedback rating: {e}")
        await callback.answer("❌ Ошибка при сохранении отзыва", show_alert=True)

# ==================== АДМИН ПАНЕЛЬ ====================

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

@dp.message(Command("admin"))
async def handle_admin_panel(message: Message):
    """Главная админ панель"""
    if not is_admin(message.from_user.id):
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🔐 <b>Админ панель</b>\n\n"
        "Выберите действие:",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: CallbackQuery):
    """Подробная статистика"""
    if not is_admin(callback.from_user.id):
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
        
        # Доход и метрики за последние 30 дней
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
        # Для ARPU используем активных за 30 дней
        active_users_30d = active_30days = await conn.fetchval(
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
        
        # Статистика активности пользователей
        active_7days = await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM users WHERE last_activity >= CURRENT_DATE - INTERVAL \'7 days\'')
        # active_30days уже посчитан выше для ARPU
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
        
        # Анализ платежеспособных пользователей (долгосрочная оценка)
        # Пользователи, которые когда-либо платили
        paying_users_count = await conn.fetchval('''
            SELECT COUNT(DISTINCT user_id) FROM payments 
            WHERE status = 'completed'
        ''')
        
        # Анализ потенциально платежеспособных пользователей
        # Критерии: активность, использование сервиса, срок подписки
        potential_paying_users = await conn.fetch('''
            SELECT 
                u.user_id,
                u.last_activity,
                u.subscription_end,
                u.pay_subscribed,
                COUNT(DISTINCT k.id) as keys_count,
                COUNT(DISTINCT p.id) as payments_count,
                MAX(p.timestamp) as last_payment_date
            FROM users u
            LEFT JOIN vpn_keys k ON u.user_id = k.user_id AND k.is_active = TRUE
            LEFT JOIN payments p ON u.user_id = p.user_id AND p.status = 'completed'
            WHERE u.blacklisted = FALSE
            GROUP BY u.user_id, u.last_activity, u.subscription_end, u.pay_subscribed
            HAVING 
                (u.last_activity >= CURRENT_DATE - INTERVAL '30 days' OR u.last_activity IS NULL)
                AND (
                    (u.pay_subscribed = TRUE AND u.subscription_end >= CURRENT_DATE - INTERVAL '7 days')
                    OR (COUNT(DISTINCT k.id) > 0)
                    OR (COUNT(DISTINCT p.id) > 0)
                )
        ''')
        
        # Оценка платежеспособности на основе анализа
        high_potential = 0
        medium_potential = 0
        low_potential = 0
        
        for user in potential_paying_users:
            keys_count = user.get('keys_count', 0) or 0
            payments_count = user.get('payments_count', 0) or 0
            last_activity = user.get('last_activity')
            subscription_end = user.get('subscription_end')
            pay_subscribed = user.get('pay_subscribed', False)
            
            score = 0
            
            # Баллы за активность
            if last_activity:
                days_since_activity = (datetime.now() - last_activity).days if isinstance(last_activity, datetime) else 0
                if days_since_activity <= 7:
                    score += 3
                elif days_since_activity <= 30:
                    score += 1
            
            # Баллы за использование сервиса
            if keys_count > 0:
                score += 2
            
            # Баллы за историю платежей
            if payments_count > 0:
                score += 3
                if payments_count > 1:
                    score += 1
            
            # Баллы за активную подписку
            if pay_subscribed and subscription_end:
                try:
                    if isinstance(subscription_end, str):
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = subscription_end
                    days_remaining = (end_date - datetime.now()).days
                    if days_remaining > 0 and days_remaining <= 7:
                        score += 2  # Подписка скоро истекает - высокая вероятность продления
                except:
                    pass
            
            if score >= 5:
                high_potential += 1
            elif score >= 2:
                medium_potential += 1
            else:
                low_potential += 1
        
        total_potential = high_potential + medium_potential + low_potential
        
        # Аналитика пробного периода
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
        
        # Прогноз дня максимальных платежей
        # Анализируем подписки, которые заканчиваются в ближайшие 30 дней
        expiring_subscriptions = await conn.fetch('''
            SELECT 
                subscription_end,
                user_id,
                last_activity
            FROM users
            WHERE pay_subscribed = TRUE 
                AND subscription_end >= CURRENT_DATE
                AND subscription_end <= CURRENT_DATE + INTERVAL '30 days'
                AND blacklisted = FALSE
        ''')
        
        # Анализируем историю платежей для понимания паттернов
        payment_history = await conn.fetch('''
            SELECT 
                DATE(timestamp) as payment_date,
                COUNT(*) as payment_count,
                EXTRACT(DOW FROM timestamp) as day_of_week
            FROM payments
            WHERE status = 'completed'
                AND timestamp >= CURRENT_DATE - INTERVAL '90 days'
            GROUP BY DATE(timestamp), EXTRACT(DOW FROM timestamp)
            ORDER BY payment_date DESC
        ''')
        
        # Подсчитываем подписки по дням окончания
        expiration_by_day = {}
        for sub in expiring_subscriptions:
            try:
                if isinstance(sub['subscription_end'], str):
                    end_date = datetime.strptime(sub['subscription_end'].split()[0], "%Y-%m-%d")
                else:
                    end_date = sub['subscription_end']
                
                # Учитываем активность пользователя
                last_activity = sub.get('last_activity')
                activity_score = 1.0
                if last_activity:
                    if isinstance(last_activity, str):
                        last_act = datetime.strptime(last_activity.split()[0], "%Y-%m-%d")
                    else:
                        last_act = last_activity
                    days_since = (datetime.now() - last_act).days
                    if days_since <= 7:
                        activity_score = 1.0
                    elif days_since <= 30:
                        activity_score = 0.7
                    else:
                        activity_score = 0.3
                
                date_key = end_date.strftime('%Y-%m-%d')
                if date_key not in expiration_by_day:
                    expiration_by_day[date_key] = 0
                expiration_by_day[date_key] += activity_score
            except Exception as e:
                logger.error(f"Error processing subscription end date: {e}")
                continue
        
        # Учитываем исторические паттерны платежей
        day_of_week_pattern = {}
        for payment in payment_history:
            dow = int(payment['day_of_week'])
            count = payment['payment_count']
            if dow not in day_of_week_pattern:
                day_of_week_pattern[dow] = []
            day_of_week_pattern[dow].append(count)
        
        # Вычисляем средние значения по дням недели
        avg_by_dow = {}
        for dow, counts in day_of_week_pattern.items():
            avg_by_dow[dow] = sum(counts) / len(counts) if counts else 0
        
        # Находим день с максимальным прогнозом
        max_predicted_date = None
        max_predicted_count = 0
        
        for date_str, base_count in expiration_by_day.items():
            try:
                pred_date = datetime.strptime(date_str, '%Y-%m-%d')
                dow = pred_date.weekday()
                
                # Учитываем паттерн дня недели
                dow_multiplier = avg_by_dow.get(dow, 1.0) / (sum(avg_by_dow.values()) / len(avg_by_dow) if avg_by_dow else 1.0)
                if dow_multiplier == 0:
                    dow_multiplier = 1.0
                
                predicted_count = base_count * (1 + (dow_multiplier - 1) * 0.3)  # Умеренное влияние паттерна
                
                if predicted_count > max_predicted_count:
                    max_predicted_count = predicted_count
                    max_predicted_date = pred_date
            except Exception as e:
                logger.error(f"Error processing prediction date: {e}")
                continue
        
        # Исторический максимум для сравнения
        max_payments_day = await conn.fetchrow('''
            SELECT DATE(timestamp) as payment_date, COUNT(*) as payment_count
            FROM payments
            WHERE status = 'completed'
            GROUP BY DATE(timestamp)
            ORDER BY payment_count DESC
            LIMIT 1
        ''')
        
        max_payments_date_str = "Нет данных"
        max_payments_count = 0
        if max_payments_day:
            max_payments_date = max_payments_day['payment_date']
            max_payments_count = max_payments_day['payment_count']
            if isinstance(max_payments_date, str):
                max_payments_date_str = max_payments_date
            else:
                max_payments_date_str = max_payments_date.strftime('%d.%m.%Y')
        
        # Форматируем прогноз
        predicted_date_str = "Недостаточно данных"
        predicted_count_str = "—"
        confidence = "Низкая"
        
        if max_predicted_date:
            predicted_date_str = max_predicted_date.strftime('%d.%m.%Y')
            predicted_count_str = f"{int(max_predicted_count)}"
            
            # Оценка уверенности прогноза
            if len(expiring_subscriptions) >= 10 and len(payment_history) >= 7:
                confidence = "Высокая"
            elif len(expiring_subscriptions) >= 5:
                confidence = "Средняя"
            else:
                confidence = "Низкая"
        
        stats_text = (
            "📊 <b>Подробная статистика</b>\n\n"
            "👥 <b>Пользователи:</b>\n"
            f"• Всего пользователей: <i>{total_users}</i>\n"
            f"• Активных подписок: <i>{active_subscriptions}</i>\n"
            f"• Платежеспособных (платили): <i>{paying_users_count}</i>\n"
            f"• Новых сегодня: <i>{new_today}</i>\n"
            f"• Новых за неделю: <i>{new_week}</i>\n\n"
            
            "💎 <b>Оценка платежеспособности:</b>\n"
            f"• Высокий потенциал: <i>{high_potential}</i>\n"
            f"• Средний потенциал: <i>{medium_potential}</i>\n"
            f"• Низкий потенциал: <i>{low_potential}</i>\n"
            f"• Всего проанализировано: <i>{total_potential}</i>\n\n"
            
            "📈 <b>Активность пользователей:</b>\n"
            f"• Активных за 7 дней: <i>{active_7days}</i>\n"
            f"• Активных за 30 дней: <i>{active_30days}</i>\n"
            f"• Неактивных 30+ дней: <i>{inactive_30days}</i>\n\n"
            
            "💰 <b>Финансы:</b>\n"
            f"• Доход (RUB): <i>{total_revenue_rub / 100 if total_revenue_rub else 0:.2f}₽</i>\n"
            f"• Доход (Stars): <i>{total_revenue_stars}⭐</i>\n"
            f"• Платежей сегодня: <i>{payments_today}</i>\n"
            f"• Доход сегодня: <i>{revenue_today_rub / 100 if revenue_today_rub else 0:.2f}₽</i>\n"
            f"• Доход за 30 дней (RUB): <i>{revenue_30d_rub / 100 if revenue_30d_rub else 0:.2f}₽</i>\n"
            f"• ARPU 30д: <i>{arpu_30d:.2f}₽</i>\n"
            f"• ARPPU 30д: <i>{arppu_30d:.2f}₽</i>\n"
            f"• Исторический максимум: <i>{max_payments_count}</i> ({max_payments_date_str})\n\n"
            
            "🔮 <b>Прогноз максимальных платежей:</b>\n"
            f"• Прогнозируемая дата: <i>{predicted_date_str}</i>\n"
            f"• Ожидаемое количество: <i>{predicted_count_str}</i>\n"
            f"• Уверенность прогноза: <i>{confidence}</i>\n"
            f"• Анализ подписок: <i>{len(expiring_subscriptions)}</i> истекают в ближайшие 30 дней\n\n"
            
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
        builder.row(InlineKeyboardButton(text="🖥️ Статистика серверов", callback_data="admin_servers_stats"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
        
        await callback.message.edit_text(stats_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

@dp.callback_query(F.data == "admin_servers_stats")
async def handle_admin_servers_stats(callback: CallbackQuery):
    """Статистика серверов с инлайн кнопками"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    async with get_connection() as conn:
        servers_list = await conn.fetch('SELECT id, name, base_url, username, password, inbound_id FROM servers WHERE is_active = TRUE ORDER BY name')
        
        if not servers_list:
            await callback.message.edit_text(
                "🖥️ <b>Статистика серверов</b>\n\n"
                "❌ Нет активных серверов",
                reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats")).as_markup(),
                parse_mode="HTML"
            )
            await safe_callback_answer(callback)
            return
        
        builder = InlineKeyboardBuilder()
        for server in servers_list:
            builder.row(InlineKeyboardButton(
                text=f"🖥️ {server['name']}",
                callback_data=f"admin_server_stats:{server['id']}"
            ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stats"))
        
        await callback.message.edit_text(
            "🖥️ <b>Статистика серверов</b>\n\n"
            "Выберите сервер для просмотра детальной статистики:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("admin_server_stats:"))
async def handle_admin_server_stats_detail(callback: CallbackQuery):
    """Детальная статистика конкретного сервера"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    server_id = int(callback.data.split(":")[1])
    
    async with get_connection() as conn:
        server = await conn.fetchrow('SELECT id, name, base_url, username, password, inbound_id FROM servers WHERE id = $1', server_id)
        
        if not server:
            await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
            return
        
        server_name = server['name']
        stats_text = f"🖥️ <b>Статистика сервера: {server_name}</b>\n\n"
        
        try:
            server_client = XUIClient(
                base_url=server['base_url'],
                username=server['username'],
                password=server['password'],
                inbound_id=server['inbound_id']
            )
            
            stats = server_client.get_inbound_stats()
            
            up_gb = stats['up_gb']
            down_gb = stats['down_gb']
            total_gb = stats['total_gb']
            clients_count = stats['clients_count']
            active_clients = stats['active_clients']
            
            stats_text += (
                f"👥 <b>Клиенты:</b>\n"
                f"• Всего клиентов: <i>{clients_count}</i>\n"
                f"• Активных клиентов: <i>{active_clients}</i>\n\n"
                f"📊 <b>Трафик:</b>\n"
                f"• Исходящий: <i>{up_gb:.2f} GB</i>\n"
                f"• Входящий: <i>{down_gb:.2f} GB</i>\n"
            )
            if total_gb > 0:
                stats_text += f"• Лимит: <i>{total_gb:.2f} GB</i>\n"
        except Exception as e:
            logger.error(f"Error getting stats for server {server_name}: {e}")
            stats_text += f"❌ Ошибка получения статистики: <code>{str(e)}</code>\n"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admin_servers_stats"))
        
        await callback.message.edit_text(stats_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)

@dp.callback_query(F.data == "admin_back")
async def handle_admin_back(callback: CallbackQuery):
    """Вернуться в админ панель"""
    if not is_admin(callback.from_user.id):
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
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name or "Пользователь"
    
    # Обновляем активность
    async with get_connection() as conn:
        await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)
    
    subscription_status = await get_subscription_status(user_id)
    await callback.message.edit_text(
        await get_main_text(first_name, subscription_status, user_id),
        parse_mode="HTML",
        reply_markup=await get_main_keyboard(user_id)
    )
    await safe_callback_answer(callback)

# Рассылка
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
        'trial_used': 'Использовали пробный период'
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

@dp.callback_query(F.data == "admin_broadcast")
async def handle_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    """Начать рассылку - конструктор"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    # Определяем тип медиа и сохраняем file_id
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
    
    # Если текст еще не был добавлен, используем caption как текст
    data = await state.get_data()
    existing_text = data.get('broadcast_text', '')
    if not existing_text and caption:
        await state.update_data(broadcast_text=caption)
    elif existing_text and caption:
        # Если текст уже есть, но есть и caption - используем существующий текст как caption для медиа
        pass
    
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    data = await state.get_data()
    existing_buttons = data.get('broadcast_buttons', [])
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Получить VPN", callback_data="broadcast_add_menu_button:get_vpn"))
    builder.row(InlineKeyboardButton(text="🔑 Ключи ВПН", callback_data="broadcast_add_menu_button:my_keys"))
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    button_type = callback.data.split(":")[1]
    data = await state.get_data()
    existing_buttons = data.get('broadcast_buttons', [])
    
    # Проверяем, не добавлена ли уже эта кнопка
    menu_buttons_map = {
        "get_vpn": "🔗 Получить VPN",
        "my_keys": "🔑 Ключи ВПН",
        "referral": "🎁 Подарок",
        "premium": "💎 Подписка",
        "help": "🆘 Помощь",
        "trial": "🆓 Пробный период"
    }
    
    button_text = menu_buttons_map.get(button_type)
    if not button_text:
        await safe_callback_answer(callback, "❌ Неизвестный тип кнопки", show_alert=True)
        return
    
    # Проверяем, не добавлена ли уже эта кнопка
    for btn in existing_buttons:
        if btn.get('callback_data') == f"menu:{button_type}":
            await safe_callback_answer(callback, "⚠️ Эта кнопка уже добавлена", show_alert=True)
            return
    
    # Для пробного периода проверяем, что пользователь не использовал его
    if button_type == "trial":
        # Это будет обработано при отправке рассылки
        pass
    
    # Добавляем кнопку
    new_button = {
        'text': button_text,
        'callback_data': f"menu:{button_type}"
    }
    existing_buttons.append(new_button)
    await state.update_data(broadcast_buttons=existing_buttons)
    
    await safe_callback_answer(callback, f"✅ Кнопка '{button_text}' добавлена", show_alert=True)
    
    # Обновляем меню
    await handle_broadcast_add_buttons(callback, state)

@dp.callback_query(F.data == "broadcast_add_custom_button")
async def handle_broadcast_add_custom_button(callback: CallbackQuery, state: FSMContext):
    """Добавление своей кнопки в рассылку"""
    if not is_admin(callback.from_user.id):
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
            "<code>Telegram | https://t.me/channel</code>\n\n"
            "Когда закончите, отправьте /done"
        )
    else:
        text = (
            "🔘 <b>Добавление своей кнопки</b>\n\n"
            "Отправьте кнопку в формате:\n"
            "<code>Текст кнопки | URL</code>\n\n"
            "Пример:\n"
            "<code>Открыть сайт | https://example.com</code>\n"
            "<code>Telegram | https://t.me/channel</code>\n\n"
            "Когда закончите, отправьте /done"
        )
    
    await callback.message.edit_text(text, parse_mode="HTML")
    await state.set_state(AdminStates.BROADCAST_BUTTONS)
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "broadcast_buttons_done")
async def handle_broadcast_buttons_done(callback: CallbackQuery, state: FSMContext):
    """Завершение добавления кнопок"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    if message.text and message.text.strip().lower() == "/done":
        # Возвращаемся к конструктору
        data = await state.get_data()
        status_text, keyboard = await get_broadcast_constructor_menu(data)
        
        buttons = data.get('broadcast_buttons', [])
        if buttons:
            await message.answer(
                        f"✅ <b>Кнопки добавлены!</b>\n\n" + status_text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
        else:
            await message.answer(
                "⏭️ <b>Кнопки не добавлены</b>\n\n" + status_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            return
    
    # Парсим кнопки из текста
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
        await message.answer(
            "❌ Неверный формат. Используйте:\n"
            "<code>Текст кнопки | URL</code>\n\n"
            "Пример:\n"
            "<code>Открыть сайт | https://example.com</code>",
            parse_mode="HTML"
        )
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    # Показываем меню фильтров
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
    
    await callback.message.edit_text(
        "🔍 <b>Выберите фильтр для рассылки:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "broadcast_toggle_test")
async def handle_broadcast_toggle_test(callback: CallbackQuery, state: FSMContext):
    """Переключение тестового режима"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    data = await state.get_data()
    current_test = data.get('broadcast_test', False)
    await state.update_data(broadcast_test=not current_test)
    
    data = await state.get_data()
    status_text, keyboard = await get_broadcast_constructor_menu(data)
    
    await callback.message.edit_text(
        status_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("broadcast_filter:"))
async def handle_broadcast_filter(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора фильтра"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    filter_type = callback.data.split(":")[1]
    await state.update_data(broadcast_filter=filter_type)
    
    data = await state.get_data()
    status_text, keyboard = await get_broadcast_constructor_menu(data)
    
    await callback.message.edit_text(
        status_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "broadcast_confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и отправка рассылки"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    data = await state.get_data()
    broadcast_text = data.get('broadcast_text', '')
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')
    buttons = data.get('broadcast_buttons', [])
    filter_type = data.get('broadcast_filter', 'all')
    test_mode = data.get('broadcast_test', False)
    
    # Проверяем, что есть хотя бы текст или медиа
    if not broadcast_text and not media_type:
        await safe_callback_answer(callback, "❌ Добавьте текст или медиа", show_alert=True)
        return
    
    # Формируем клавиатуру с кнопками
    reply_markup = None
    if buttons:
        builder = InlineKeyboardBuilder()
        for btn in buttons:
            if 'url' in btn:
                # Обычная кнопка с URL
                builder.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
            elif 'callback_data' in btn:
                # Кнопка главного меню
                callback_data = btn['callback_data']
                if callback_data.startswith('menu:'):
                    menu_type = callback_data.split(':')[1]
                    # Преобразуем в правильный callback_data
                    if menu_type == 'my_keys':
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='view_key_list'))
                    elif menu_type == 'get_vpn':
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='get_vpn_link'))
                    elif menu_type == 'referral':
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_invite'))
                    elif menu_type == 'premium':
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_premium'))
                    elif menu_type == 'help':
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_help'))
                    elif menu_type == 'trial':
                        # Для пробного периода проверяем, что пользователь не использовал его
                        # Это будет обработано при отправке
                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='activate_trial'))
        reply_markup = builder.as_markup()

    # Получаем список пользователей по фильтру
    if test_mode:
        # В тестовом режиме отправляем только админу
        users = [{'user_id': callback.from_user.id}]
        await callback.message.answer(
            "🧪 <b>ТЕСТОВЫЙ РЕЖИМ:</b> Рассылка будет отправлена только вам",
            parse_mode="HTML"
        )
    else:
        async with get_connection() as conn:
            if filter_type == "all":
                users = await conn.fetch(
                    'SELECT user_id FROM users WHERE blacklisted = FALSE'
                )
            elif filter_type == "active":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND pay_subscribed = TRUE 
                    AND subscription_end >= CURRENT_DATE
                ''')
            elif filter_type == "inactive":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND (pay_subscribed = FALSE OR subscription_end < CURRENT_DATE OR subscription_end IS NULL)
                ''')
            elif filter_type == "active_7d":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND last_activity >= CURRENT_DATE - INTERVAL '7 days'
                ''')
            elif filter_type == "active_30d":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND last_activity >= CURRENT_DATE - INTERVAL '30 days'
                ''')
            elif filter_type == "with_referrals":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND referral_count > 0
                ''')
            elif filter_type == "trial_used":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND trial_used = TRUE
                ''')
            elif filter_type == "trial_not_used":
                users = await conn.fetch('''
                    SELECT user_id FROM users 
                    WHERE blacklisted = FALSE 
                    AND (trial_used = FALSE OR trial_used IS NULL)
                    AND (pay_subscribed = FALSE OR subscription_end < CURRENT_DATE OR subscription_end IS NULL)
                ''')
            else:
                users = []

    sent = 0
    failed = 0
    
    test_text = " (тестовый режим)" if test_mode else ""
    await callback.message.edit_text(f"📢 Рассылка начата{test_text}... Отправлено: {sent}, Ошибок: {failed}")
    
    # Проверяем, есть ли кнопка пробного периода
    has_trial_button = False
    trial_button_index = -1
    if buttons:
        for i, btn in enumerate(buttons):
            if btn.get('callback_data') == 'menu:trial':
                has_trial_button = True
                trial_button_index = i
                break
    
    for user_row in users:
        user_id = user_row['user_id']
        
        # Если есть кнопка пробного периода, проверяем пользователя
        if has_trial_button:
            async with get_connection() as conn:
                user_trial = await conn.fetchrow('SELECT trial_used FROM users WHERE user_id = $1', user_id)
                trial_used = user_trial.get('trial_used', False) if user_trial else False
                
                # Если пользователь использовал пробный период, убираем кнопку
                if trial_used:
                    # Создаем клавиатуру без кнопки пробного периода
                    builder = InlineKeyboardBuilder()
                    for i, btn in enumerate(buttons):
                        if i != trial_button_index:
                            if 'url' in btn:
                                builder.row(InlineKeyboardButton(text=btn['text'], url=btn['url']))
                            elif 'callback_data' in btn:
                                callback_data = btn['callback_data']
                                if callback_data.startswith('menu:'):
                                    menu_type = callback_data.split(':')[1]
                                    if menu_type == 'my_keys':
                                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='view_key_list'))
                                    elif menu_type == 'referral':
                                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_invite'))
                                    elif menu_type == 'premium':
                                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='open_premium'))
                                    elif menu_type == 'help':
                                        builder.row(InlineKeyboardButton(text=btn['text'], callback_data='help'))
                    user_reply_markup = builder.as_markup() if builder.buttons else None
                else:
                    user_reply_markup = reply_markup
        else:
            user_reply_markup = reply_markup
        
        try:
            if media_type and media_file_id:
                # Отправляем медиа
                if media_type == "photo":
                    await bot.send_photo(
                        user_id,
                        photo=media_file_id,
                        caption=broadcast_text if broadcast_text else None,
                        reply_markup=user_reply_markup,
                        parse_mode="HTML"
                    )
                elif media_type == "video":
                    await bot.send_video(
                        user_id,
                        video=media_file_id,
                        caption=broadcast_text if broadcast_text else None,
                        reply_markup=user_reply_markup,
                        parse_mode="HTML"
                    )
                elif media_type == "document":
                    await bot.send_document(
                        user_id,
                        document=media_file_id,
                        caption=broadcast_text if broadcast_text else None,
                        reply_markup=user_reply_markup,
                        parse_mode="HTML"
                    )
                elif media_type == "animation":
                    await bot.send_animation(
                        user_id,
                        animation=media_file_id,
                        caption=broadcast_text if broadcast_text else None,
                        reply_markup=user_reply_markup,
                        parse_mode="HTML" if broadcast_text else None
                    )
            else:
                # Отправляем только текст
                await bot.send_message(
                    user_id,
                    broadcast_text,
                    reply_markup=user_reply_markup,
                    parse_mode="HTML"
                )
            sent += 1
            if sent % 10 == 0:
                await callback.message.edit_text(f"📢 Рассылка... Отправлено: {sent}, Ошибок: {failed}")
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
    
    test_text_final = " (тестовый режим)" if test_mode else ""
    await callback.message.edit_text(
        f"✅ <b>Рассылка завершена{test_text_final}</b>\n\n"
        f"Отправлено: <i>{sent}</i>\n"
        f"Ошибок: <i>{failed}</i>",
        parse_mode="HTML"
    )
    await state.clear()
    await safe_callback_answer(callback)

# Управление балансом
@dp.callback_query(F.data == "admin_balance")
async def handle_admin_balance(callback: CallbackQuery, state: FSMContext):
    """Управление балансом"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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

# Управление ценами
@dp.callback_query(F.data == "admin_prices")
async def handle_admin_prices(callback: CallbackQuery):
    """Управление ценами"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    
    # Преобразуем amount в int
    try:
        amount = int(float(amount))
    except (ValueError, TypeError):
        await message.answer("❌ Сумма должна быть числом")
        return
    
    async with get_connection() as conn:
        if currency_type == "RUB":
            # Получаем текущую цену в stars
            price_row = await conn.fetchrow('SELECT price_stars FROM price_settings WHERE plan_id = $1', plan_id)
            price_stars = price_row['price_stars'] if price_row else RENEWAL_PLANS_BASE.get(plan_id, SUBSCRIPTION_PLANS_BASE.get(plan_id, {}))['price_stars']
            # Убеждаемся, что price_stars - int
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
            # Убеждаемся, что price_rub - int
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

# Управление админами
@dp.callback_query(F.data == "admin_manage_admins")
async def handle_manage_admins(callback: CallbackQuery):
    """Управление админами"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    admin_list = ", ".join([str(admin_id) for admin_id in cfg.bot.admin_ids])
    
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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

# Управление менеджерами
@dp.callback_query(F.data == "admin_manage_managers")
async def handle_manage_managers(callback: CallbackQuery):
    """Управление менеджерами"""
    if not is_admin(callback.from_user.id):
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
async def handle_add_manager(callback: CallbackQuery, state: FSMContext):
    """Добавить менеджера"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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

# Управление реферальной системой
@dp.callback_query(F.data == "admin_referral")
async def handle_admin_referral(callback: CallbackQuery):
    """Управление реферальной системой"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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

@dp.callback_query(F.data == "admin_remove_manager")
async def handle_remove_manager(callback: CallbackQuery, state: FSMContext):
    """Удалить менеджера"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    user_id = int(callback.data.split(":")[1])
    
    async with get_connection() as conn:
        await conn.execute('UPDATE managers SET is_active = FALSE WHERE user_id = $1', user_id)
    
    await callback.message.edit_text("✅ Менеджер удален")
    await safe_callback_answer(callback)

# Управление приложениями для устройств
@dp.callback_query(F.data == "admin_device_apps")
async def handle_admin_device_apps(callback: CallbackQuery):
    """Управление приложениями для устройств"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(callback.from_user.id):
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

# Управление скидками
@dp.callback_query(F.data == "admin_discounts")
async def handle_admin_discounts(callback: CallbackQuery):
    """Управление скидками"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    async with get_connection() as conn:
        # Проверяем, есть ли уже настройки
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

# Обработчик нажатия на кнопку пробного периода (показывает информацию)
@dp.callback_query(F.data == "activate_trial")
async def handle_activate_trial(callback: CallbackQuery):
    """Показывает информацию о пробном периоде"""
    user_id = callback.from_user.id
    
    
    async with get_connection() as conn:
        # Проверяем настройки пробного периода
        trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
        trial_days = trial_settings['days'] if trial_settings and trial_settings['days'] else 0
        
        if trial_days <= 0:
            await safe_callback_answer(callback, "❌ Пробный период временно недоступен", show_alert=True)
            return
        
        # Проверяем, использовал ли пользователь пробный период
        user_info = await conn.fetchrow('SELECT trial_used, pay_subscribed, subscription_end FROM users WHERE user_id = $1', user_id)
        
        if not user_info:
            await safe_callback_answer(callback, "❌ Пользователь не найден", show_alert=True)
            return
        
        if user_info['trial_used']:
            await safe_callback_answer(callback, "❌ Вы уже использовали пробный период", show_alert=True)
            return
        
        # Проверяем, есть ли активная подписка
        has_active_sub = False
        if user_info.get('pay_subscribed') and user_info.get('subscription_end'):
            try:
                subscription_end = user_info['subscription_end']
                if isinstance(subscription_end, str):
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                has_active_sub = end_date_only >= today
            except:
                pass
        
        if has_active_sub:
            await safe_callback_answer(callback, "❌ У вас уже есть активная подписка", show_alert=True)
            return
        
        # Получаем информацию о скидке (режим 1)
        discount_settings = await conn.fetchrow('SELECT days_threshold FROM discount_settings ORDER BY id DESC LIMIT 1')
        discount_days = discount_settings['days_threshold'] if discount_settings and discount_settings['days_threshold'] else 0
        
        # Формируем текст
        text = (
            f"🆓 <b>Пробный период VPN</b>\n\n"
            f"Вы можете активировать пробный период на <b>{trial_days} {'день' if trial_days == 1 else 'дня' if trial_days < 5 else 'дней'}</b>!\n\n"
        )
        
        # Добавляем информацию о скидке, если режим 1 активен
        if discount_days > 0:
            text += (
                f"🎁 <b>Бонус!</b> После активации пробного периода вы получите "
                f"скидку на продление подписки за <b>{discount_days} {'день' if discount_days == 1 else 'дня' if discount_days < 5 else 'дней'}</b> "
                f"до окончания пробного периода!\n\n"
            )
        
        text += (
            "Пробный период можно активировать только один раз.\n\n"
            "Нажмите кнопку ниже, чтобы активировать:"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✅ Активировать", callback_data="confirm_activate_trial"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
        
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await safe_callback_answer(callback)

# Обработчик подтверждения активации пробного периода
@dp.callback_query(F.data == "confirm_activate_trial")
async def handle_confirm_activate_trial(callback: CallbackQuery):
    """Активация пробного периода после подтверждения"""
    user_id = callback.from_user.id
    
    async with get_connection() as conn:
        # Проверяем настройки пробного периода
        trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
        trial_days = trial_settings['days'] if trial_settings and trial_settings['days'] else 0
        
        if trial_days <= 0:
            await safe_callback_answer(callback, "❌ Пробный период временно недоступен", show_alert=True)
            # Возвращаемся к главному меню
            first_name = callback.from_user.first_name or "Пользователь"
            subscription_status = await get_subscription_status(user_id)
            await callback.message.edit_text(
                await get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(user_id)
            )
            return
        
        # Проверяем, использовал ли пользователь пробный период
        user_info = await conn.fetchrow('SELECT trial_used, pay_subscribed, subscription_end FROM users WHERE user_id = $1', user_id)
        
        if not user_info:
            await safe_callback_answer(callback, "❌ Пользователь не найден", show_alert=True)
            return
        
        if user_info['trial_used']:
            await safe_callback_answer(callback, "❌ Вы уже использовали пробный период", show_alert=True)
            # Возвращаемся к главному меню
            first_name = callback.from_user.first_name or "Пользователь"
            subscription_status = await get_subscription_status(user_id)
            await callback.message.edit_text(
                await get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(user_id)
            )
            return
        
        # Проверяем, есть ли активная подписка
        has_active_sub = False
        if user_info.get('pay_subscribed') and user_info.get('subscription_end'):
            try:
                subscription_end = user_info['subscription_end']
                if isinstance(subscription_end, str):
                    end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                has_active_sub = end_date_only >= today
            except:
                pass
        
        if has_active_sub:
            await safe_callback_answer(callback, "❌ У вас уже есть активная подписка", show_alert=True)
            # Возвращаемся к главному меню
            first_name = callback.from_user.first_name or "Пользователь"
            subscription_status = await get_subscription_status(user_id)
            await callback.message.edit_text(
                await get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(user_id)
            )
            return
        
        # Активируем пробный период
        trial_end_date = datetime.now() + timedelta(days=trial_days)
        # Преобразуем в date для БД
        trial_end_date_only = trial_end_date.date()
        
        await conn.execute('''
            UPDATE users
            SET trial_used = TRUE,
                pay_subscribed = TRUE,
                subscription_end = $1
            WHERE user_id = $2
        ''', trial_end_date_only, user_id)
        
        # Обновляем сообщение
        first_name = callback.from_user.first_name or "Пользователь"
        subscription_status = await get_subscription_status(user_id)
        
        await callback.message.edit_text(
            await get_main_text(first_name, subscription_status, user_id),
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(user_id)
        )
        
        await safe_callback_answer(callback, f"✅ Пробный период активирован на {trial_days} дней!", show_alert=True)
        
        # Уведомление админам об активации пробного периода
        username = callback.from_user.username or "нет"
        first_name = callback.from_user.first_name or "Пользователь"
        await notify_admins(
            f"🆓 <b>Активация пробного периода</b>\n\n"
            f"Пользователь: {first_name} (@{username})\n"
            f"ID: <code>{user_id}</code>\n"
            f"Срок: {trial_days} дней\n"
            f"Активен до: {trial_end_date.strftime('%d.%m.%Y')}"
        )

# Управление пробным периодом
@dp.callback_query(F.data == "admin_trial")
async def handle_admin_trial(callback: CallbackQuery):
    """Управление пробным периодом"""
    if not is_admin(callback.from_user.id):
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
    if not is_admin(callback.from_user.id):
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
    if not is_admin(message.from_user.id):
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

# Управление серверами
@dp.callback_query(F.data == "admin_servers")
async def handle_admin_servers(callback: CallbackQuery):
    """Управление серверами"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    # Сначала отвечаем на callback, чтобы избежать таймаута
    await safe_callback_answer(callback)
    
    try:
        async with get_connection() as conn:
            servers = await conn.fetch('SELECT id, name, ip, is_active FROM servers ORDER BY id')
        
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
                status = "✅ Активен" if server['is_active'] else "⏸️ На паузе"
                button_text = f"{server['name']} ({server['ip']}) - {status}"
                builder.row(InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"admin_server_view:{server['id']}"
                ))
            
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
async def handle_admin_server_view(callback: CallbackQuery):
    """Просмотр информации о сервере"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    server_id = int(callback.data.split(":")[1])
    
    try:
        async with get_connection() as conn:
            server = await conn.fetchrow('''
                SELECT id, name, ip, port, protocol, username, password, inbound_id, base_url, is_active, created_at
                FROM servers WHERE id = $1
            ''', server_id)
            
            if not server:
                await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                return
            
            # Статистика по серверу
            keys_count = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE server_id = $1', server_id)
            active_keys = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE server_id = $1 AND is_active = TRUE', server_id)
        
        status = "✅ Активен" if server['is_active'] else "⏸️ На паузе"
        created_at = server['created_at'].strftime('%d.%m.%Y %H:%M') if server['created_at'] else "Неизвестно"
        
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
            f"<b>Статус:</b> {status}\n"
            f"<b>Создан:</b> {created_at}\n\n"
            f"<b>Статистика:</b>\n"
            f"• Всего ключей: {keys_count}\n"
            f"• Активных ключей: {active_keys}\n"
        )
        
        # Проверяем длину сообщения (лимит Telegram - 4096 символов)
        if len(text) > 4096:
            text = text[:4050] + "\n\n⚠️ <i>Сообщение обрезано из-за ограничения длины</i>"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="⏸️ Пауза" if server['is_active'] else "▶️ Активировать",
            callback_data=f"admin_server_toggle:{server_id}"
        ))
        builder.row(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"admin_server_edit:{server_id}"))
        builder.row(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"admin_server_delete:{server_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад к серверам", callback_data="admin_servers"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await safe_callback_answer(callback)
    except Exception as e:
        logger.error(f"Error in handle_admin_server_view: {e}", exc_info=True)
        await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("admin_server_toggle:"))
async def handle_admin_server_toggle(callback: CallbackQuery):
    """Переключение статуса сервера (пауза/активация)"""
    if not is_admin(callback.from_user.id):
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
        
        status_text = "активирован" if new_status else "приостановлен"
        await safe_callback_answer(callback, f"✅ Сервер {status_text}")
        
        # Обновляем интерфейс
        new_callback = callback.model_copy(update={'data': f"admin_server_view:{server_id}"})
        await handle_admin_server_view(new_callback)
    except Exception as e:
        logger.error(f"Error in handle_admin_server_toggle: {e}", exc_info=True)
        await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("admin_server_delete:"))
async def handle_admin_server_delete(callback: CallbackQuery):
    """Удаление сервера"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    server_id = int(callback.data.split(":")[1])
    
    try:
        async with get_connection() as conn:
            server = await conn.fetchrow('SELECT name FROM servers WHERE id = $1', server_id)
            if not server:
                await safe_callback_answer(callback, "❌ Сервер не найден", show_alert=True)
                return
            
            # Проверяем, есть ли ключи на этом сервере
            keys_count = await conn.fetchval('SELECT COUNT(*) FROM vpn_keys WHERE server_id = $1', server_id)
            
            if keys_count > 0:
                await safe_callback_answer(
                    callback,
                    f"❌ Нельзя удалить сервер: на нем {keys_count} ключей. Сначала удалите или переместите ключи.",
                    show_alert=True
                )
                return
            
            await conn.execute('DELETE FROM servers WHERE id = $1', server_id)
        
        await safe_callback_answer(callback, f"✅ Сервер '{server['name']}' удален")
        await handle_admin_servers(callback)
    except Exception as e:
        logger.error(f"Error in handle_admin_server_delete: {e}", exc_info=True)
        await safe_callback_answer(callback, f"❌ Ошибка: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "admin_server_add")
async def handle_admin_server_add(callback: CallbackQuery, state: FSMContext):
    """Начало добавления сервера"""
    if not is_admin(callback.from_user.id):
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
async def handle_admin_server_edit(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования сервера"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    server_id = int(callback.data.split(":")[1])
    
    async with get_connection() as conn:
        server = await conn.fetchrow('''
            SELECT id, name, ip, port, protocol, username, password, inbound_id, base_url
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
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_server_view:{server_id}"))
    
    await callback.message.edit_text(
        f"✏️ <b>Редактирование сервера</b>\n\n"
        f"<b>Текущие данные:</b>\n"
        f"Название: <i>{server['name']}</i>\n"
        f"IP: <i>{server['ip']}</i>\n"
        f"Порт: <i>{server['port']}</i>\n"
        f"Протокол: <i>{server['protocol'].upper()}</i>\n"
        f"Username: <i>{server['username']}</i>\n"
        f"Inbound ID: <i>{server['inbound_id']}</i>\n\n"
        f"Выберите поле для редактирования:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await safe_callback_answer(callback)

@dp.callback_query(F.data.startswith("admin_server_edit_field:"))
async def handle_admin_server_edit_field(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора поля для редактирования"""
    if not is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    
    parts = callback.data.split(":")
    field = parts[1]
    server_id = int(parts[2])
    
    await state.update_data(server_id=server_id, edit_field=field)
    
    field_names = {
        'name': 'название',
        'ip': 'IP адрес',
        'port': 'порт',
        'protocol': 'протокол (http/https)',
        'username': 'username',
        'password': 'password',
        'inbound_id': 'Inbound ID'
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
async def process_server_edit(message: Message, state: FSMContext):
    """Обработка редактирования сервера"""
    if not is_admin(message.from_user.id):
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
    elif field == 'inbound_id':
        try:
            new_value = int(new_value)
        except ValueError:
            await message.answer("❌ Inbound ID должен быть числом")
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
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад к серверу", callback_data=f"admin_server_view:{server_id}"))
    
    await message.answer(
        f"✅ Поле <b>{field}</b> обновлено!",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(AdminStates.SERVER_NAME)
async def process_server_name_admin(message: Message, state: FSMContext):
    """Обработка названия сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    await state.update_data(name=message.text)
    await message.answer("Введите IP адрес сервера:")
    await state.set_state(AdminStates.SERVER_IP)

@dp.message(AdminStates.SERVER_IP)
async def process_server_ip_admin(message: Message, state: FSMContext):
    """Обработка IP сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    await state.update_data(ip=message.text)
    await message.answer("Введите порт (по умолчанию 54321, можно просто нажать Enter):")
    await state.set_state(AdminStates.SERVER_PORT)

@dp.message(AdminStates.SERVER_PORT)
async def process_server_port_admin(message: Message, state: FSMContext):
    """Обработка порта сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    port_text = message.text.strip()
    port = int(port_text) if port_text else 54321
    await state.update_data(port=port)
    await message.answer("Введите протокол (http/https, по умолчанию https):")
    await state.set_state(AdminStates.SERVER_PROTOCOL)

@dp.message(AdminStates.SERVER_PROTOCOL)
async def process_server_protocol_admin(message: Message, state: FSMContext):
    """Обработка протокола сервера"""
    if not is_admin(message.from_user.id):
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
async def process_server_username_admin(message: Message, state: FSMContext):
    """Обработка username сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    await state.update_data(username=message.text)
    await message.answer("Введите password для панели 3x-ui:")
    await state.set_state(AdminStates.SERVER_PASSWORD)

@dp.message(AdminStates.SERVER_PASSWORD)
async def process_server_password_admin(message: Message, state: FSMContext):
    """Обработка password сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        await state.clear()
        return
    
    await state.update_data(password=message.text)
    await message.answer("Введите Inbound ID (число):")
    await state.set_state(AdminStates.SERVER_INBOUND_ID)

@dp.message(AdminStates.SERVER_INBOUND_ID)
async def process_server_inbound_id_admin(message: Message, state: FSMContext):
    """Обработка Inbound ID и сохранение сервера"""
    if not is_admin(message.from_user.id):
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
        test_client.login()
    except Exception as e:
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
        # Синхронизируем последовательность с максимальным id в таблице
        await conn.execute('''
            SELECT setval('servers_id_seq', COALESCE((SELECT MAX(id) FROM servers), 0) + 1, false)
        ''')
        server_id = await conn.fetchval('''
            INSERT INTO servers (name, ip, port, protocol, username, password, inbound_id, base_url, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
            RETURNING id
        ''', name, ip, port, protocol, username, password, inbound_id, base_url)
        
        # Автоматически создаём ключи для нового сервера для всех активных пользователей
        try:
            active_users = await conn.fetch('''
                SELECT user_id
                FROM users
                WHERE pay_subscribed = TRUE 
                  AND subscription_end IS NOT NULL
                  AND DATE(subscription_end) >= CURRENT_DATE
            ''')
            
            if active_users:
                logger.info(f"Creating keys for {len(active_users)} active users on new server {name} (ID: {server_id})")
                # Создаём ключи в фоне, чтобы не блокировать ответ админу
                for user_row in active_users:
                    user_id = user_row['user_id']
                    try:
                        asyncio.create_task(create_keys_for_all_servers(user_id))
                    except Exception as e:
                        logger.error(f"Failed to create keys for user {user_id} on new server: {e}")
        except Exception as e:
            logger.error(f"Error creating keys for active users on new server: {e}")
    
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

def setup_scheduler():
    """Инициализация и запуск планировщика задач"""
    global scheduler
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # Проверка истекших подписок с удалением ключей и уведомлениями
    # Запускаем при старте бота и затем каждый день в 00:00
    scheduler.add_job(
        handle_expired_subscriptions,
        'cron',
        hour=0,
        minute=0
    )
    # Также запускаем при старте бота для обработки накопившихся истекших подписок
    scheduler.add_job(
        handle_expired_subscriptions,
        'date',
        run_date=datetime.now() + timedelta(seconds=30)  # Через 30 секунд после старта
    )
    
    # Синхронизация подписок и ключей
    scheduler.add_job(
        sync_subscriptions_and_keys,
        'cron',
        hour=11,
        minute=53
    )
    
    # Отправка опросов через 3 дня после покупки
    scheduler.add_job(
        send_feedback_request,
        'cron',
        hour=12,
        minute=10
    )
    
    # Напоминания о подписке за 3 дня до окончания
    scheduler.add_job(
        send_subscription_reminder,
        'cron',
        hour=12,
        minute=15
    )
    
    # Отмена просроченных платежей через ЮKassa (каждые 15 минут)
    scheduler.add_job(
        cancel_expired_payments,
        'interval',
        minutes=15
    )
    
    scheduler.start()
    logger.info("APScheduler started with 5 scheduled jobs")

async def shutdown():
    """Корректное завершение всех компонентов бота"""
    global _shutdown_in_progress
    
    if _shutdown_in_progress:
        return
    
    _shutdown_in_progress = True
    logger.info("Shutting down gracefully...")
    
    # Останавливаем polling (это завершит dp.start_polling)
    try:
        await dp.stop_polling()
        logger.info("Polling stopped")
    except Exception as e:
        logger.error(f"Error stopping polling: {e}")
    
    # Останавливаем scheduler
    global scheduler
    if scheduler and scheduler.running:
        try:
            scheduler.shutdown(wait=True)
            logger.info("Scheduler stopped")
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")
    
    # Закрываем Flyer клиент
    global flyer_client
    if flyer_client:
        try:
            await flyer_client.close()
            logger.info("Flyer client closed")
        except Exception as e:
            logger.error(f"Error closing Flyer client: {e}")

    # Закрываем Uni Jump клиент
    global unijump_client
    if unijump_client:
        try:
            await unijump_client.close()
            logger.info("Uni Jump client closed")
        except Exception as e:
            logger.error(f"Error closing Uni Jump client: {e}")
    
    # Останавливаем вебхук сервер
    global flyer_webhook_server
    if flyer_webhook_server:
        try:
            await flyer_webhook_server.stop()
            logger.info("Flyer webhook server stopped")
        except Exception as e:
            logger.error(f"Error stopping Flyer webhook server: {e}")
    
    # Закрываем сессию бота
    try:
        await bot.session.close()
        logger.info("Bot session closed")
    except Exception as e:
        logger.error(f"Error closing bot session: {e}")

async def main():
    """Основная функция запуска бота"""
    # Устанавливаем обработчики сигналов
    loop = asyncio.get_running_loop()
    shutdown_task = None
    
    def handle_signal(sig):
        """Синхронный обработчик сигнала"""
        nonlocal shutdown_task
        logger.info(f"Received signal {sig}, initiating shutdown...")
        # Запускаем shutdown асинхронно (если еще не запущен)
        if shutdown_task is None or shutdown_task.done():
            shutdown_task = asyncio.create_task(shutdown())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except (ValueError, RuntimeError) as e:
            # На Windows могут быть проблемы с сигналами
            logger.warning(f"Could not add signal handler for {sig}: {e}")
    
    # Инициализируем БД
    await init_db()
    
    # Запускаем APScheduler (работает в том же event loop)
    setup_scheduler()
    
    # Запускаем вебхук сервер для Flyer Service (если включен)
    webhook_task = None
    global flyer_webhook_server
    if flyer_webhook_server:
        try:
            webhook_host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
            webhook_port = int(os.getenv("WEBHOOK_PORT", "8080"))
            webhook_task = asyncio.create_task(
                flyer_webhook_server.run(host=webhook_host, port=webhook_port)
            )
            logger.info(f"Flyer webhook server starting on {webhook_host}:{webhook_port}")
        except Exception as e:
            logger.error(f"Error starting Flyer webhook server: {e}")
    
    try:
        logger.info("Bot is starting...")
        # Запускаем polling (блокирует выполнение до остановки)
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except asyncio.CancelledError:
        logger.info("Polling was cancelled")
    except Exception as e:
        logger.error(f"Error in polling: {e}", exc_info=True)
    finally:
        # Выполняем финальное завершение (если еще не выполнено)
        if shutdown_task is None or shutdown_task.done():
            await shutdown()
        else:
            await shutdown_task

if __name__ == "__main__":
    print("Бот запущен!")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        print("\nБот остановлен!")
