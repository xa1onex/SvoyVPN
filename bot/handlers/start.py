"""
Обработчик команды /start
"""
import secrets
import random
import logging
from datetime import datetime, timedelta
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from ..database import get_connection, generate_subscription_token, ensure_subscription_token
from ..subscriptions import get_subscription_status_display, get_user_subscription_url

logger = logging.getLogger(__name__)

# Публичная оферта и политика конфиденциальности (актуальная версия на Telegraph)
OFFER_PRIVACY_TELEGRAPH_URL = (
    "https://telegra.ph/PUBLICHNAYA-OFERTA-I-POLITIKA-KONFIDENCIALNOSTI-04-20"
)


def _svoyvpn_tagline_html() -> str:
    return (
        "<b>SvoyVPN</b> — стабильный VPN с обходом <b>Белых списков</b> "
        "и с <b>бесплатным</b> тарифом!\n\n"
    )


def _svoyvpn_why_and_footer_html() -> str:
    offer_link = (
        f'<a href="{OFFER_PRIVACY_TELEGRAPH_URL}">'
        "офертой и политикой конфиденциальности</a>"
    )
    why_block = (
        "<b>Зачем он вам:</b>\n"
        "• Мы дарим подарки всем за использование VPN - /invite\n"
        "• Универсальные тарифы от <b>0₽/год</b>\n"
        "• Стабильный обход <b>Белых списков</b>\n"
        "• И многое другое"
    )
    footer = (
        f"\n\n👉 Больше информации в разделе <b>помощь</b> — /help\n\n"
        f"‼️ Используя сервис, вы соглашаетесь с {offer_link}."
    )
    return why_block + footer


async def build_new_user_welcome_message(
    *,
    has_referral: bool = False,
    referral_bonus_days: int = 0,
) -> str:
    header = _svoyvpn_tagline_html()
    body = _svoyvpn_why_and_footer_html()

    if has_referral:
        days = max(int(referral_bonus_days or 0), 1)
        gift_line = (
            f"🎁 Ваш друг подарил вам подписку на <b>{days} дн.</b> — "
            "просто подключайтесь и пользуйтесь свободным интернетом без ограничений"
        )
        return f"{header}{gift_line}\n\n{body}"

    return f"{header}{body}"


async def setup_start_handler(dp, bot: Bot, config):
    """Настраивает обработчик /start"""
    
    @dp.message(CommandStart())
    async def handle_start(message: Message):
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name or "Пользователь"
        args = message.text.split()
        
        # Парсим deep link аргумент
        deep_link_arg = args[1] if len(args) > 1 else None

        # Докупка трафика по ссылке t.me/Bot?start=traffic
        if deep_link_arg in ("traffic", "boost"):
            async with get_connection() as conn:
                exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id)
            if exists:
                from .subscription import send_traffic_packs_menu
                await send_traffic_packs_menu(bot, message, config, edit=False)
            else:
                await message.answer(
                    "Сначала нажми <b>/start</b> без параметров, затем открой ссылку снова.",
                    parse_mode="HTML",
                )
            return
        
        # ── Android Auth Confirmation ──
        if deep_link_arg and deep_link_arg.startswith('auth_'):
            nonce = deep_link_arg[5:]
            from ..webhook_server import WebhookServer
            WebhookServer.confirm_telegram_auth(nonce, user_id)
            await message.answer(
                f"✅ <b>Личность подтверждена!</b>\n\n"
                "Вы успешно вошли в приложение SvoyVPN. Теперь можете вернуться в приложение.",
                parse_mode="HTML"
            )
            return

        # ── Привязка Telegram к аккаунту с почты (веб / мобильный вход по email) ──
        if deep_link_arg and deep_link_arg.startswith('linktg_'):
            nonce = deep_link_arg[7:]
            from ..webhook_server import WebhookServer
            msg = await WebhookServer.confirm_link_telegram(
                nonce, user_id, username, first_name
            )
            await message.answer(msg, parse_mode="HTML")
            return

        referral_code = deep_link_arg[4:] if deep_link_arg and deep_link_arg.startswith('ref_') else None
        utm_tag = (
            deep_link_arg
            if deep_link_arg
            and not deep_link_arg.startswith('ref_')
            and deep_link_arg not in ('traffic', 'boost')
            else None
        )
        
        async with get_connection() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            
            if not user:
                # ── Обычная регистрация нового пользователя ──
                # Создаём нового пользователя
                new_referral_code = secrets.token_hex(4)
                sub_token = generate_subscription_token()
                
                await conn.execute('''
                    INSERT INTO users (
                        user_id, username, first_name, registration_date, last_activity,
                        referral_code, invited_by, pay_subscribed, subscription_end, subscription_token,
                        utm_source
                    ) VALUES ($1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $4, NULL, FALSE, NULL, $5, $6)
                ''', user_id, username, first_name, new_referral_code, sub_token, utm_tag)
                
                # Обработка реферального кода
                has_referral = False
                invited_bonus_days = None
                invited_end_date = None
                
                # --- UTM tracking ---
                utm_bonus_applied = False
                utm_bonus_days = 0
                utm_campaign_desc = None
                
                if utm_tag:
                    # Записываем визит в utm_visits
                    try:
                        await conn.execute('''
                            INSERT INTO utm_visits (user_id, utm_tag, is_new_user)
                            VALUES ($1, $2, TRUE)
                        ''', user_id, utm_tag)
                    except Exception as e:
                        logger.warning(f"Could not log UTM visit: {e}")
                    
                    # Проверяем, есть ли настроенная кампания с привилегиями
                    try:
                        campaign = await conn.fetchrow(
                            'SELECT * FROM utm_campaigns WHERE tag = $1 AND is_active = TRUE', utm_tag
                        )
                        if campaign and campaign['bonus_days'] and campaign['bonus_days'] > 0:
                            utm_bonus_days = campaign['bonus_days']
                            utm_campaign_desc = campaign['description']
                            from ..referral_rewards import grant_plus_bonus_days
                            await grant_plus_bonus_days(conn, user_id, utm_bonus_days)
                            utm_bonus_applied = True
                            logger.info(
                                "UTM Plus bonus %s days applied to user %s (tag: %s)",
                                utm_bonus_days, user_id, utm_tag,
                            )
                    except Exception as e:
                        logger.warning(f"Could not process UTM campaign: {e}")
                
                if referral_code:
                    inviter = await conn.fetchrow(
                        'SELECT user_id FROM users WHERE referral_code = $1', referral_code
                    )
                    if inviter and inviter['user_id'] != user_id:
                        inviter_id = inviter['user_id']
                        await conn.execute(
                            'UPDATE users SET invited_by = $1 WHERE user_id = $2',
                            inviter_id, user_id,
                        )
                        await conn.execute(
                            'UPDATE users SET referral_count = referral_count + 1 WHERE user_id = $1',
                            inviter_id,
                        )
                        try:
                            from ..referral_rewards import grant_referral_bonuses
                            await grant_referral_bonuses(bot, user_id, inviter_id)
                        except Exception as e:
                            logger.error("referral bonus grant error: %s", e)
                        has_referral = True
                        logger.info("Referral: inviter=%s invited=%s", inviter_id, user_id)
                
                # Уведомление админам
                source_info = "по реферальной ссылке" if has_referral else "без рефералки"
                if utm_tag:
                    source_info = f"UTM: <code>{utm_tag}</code>"
                    if utm_bonus_applied:
                        source_info += f" (+{utm_bonus_days} дн.)"
                
                for admin_id in config.bot.admin_ids:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"👤 <b>Новая регистрация</b>\n\n"
                            f"ID: <code>{user_id}</code>\n"
                            f"Имя: {first_name}\n"
                            f"Username: @{username if username else 'нет'}\n"
                            f"Реферальный код: <code>{new_referral_code}</code>\n"
                            f"Источник: {source_info}",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin {admin_id}: {e}")
                
                from ..referral_rewards import get_referral_bonus_days
                referral_bonus_days = await get_referral_bonus_days() if has_referral else 0

                welcome_msg = await build_new_user_welcome_message(
                    has_referral=has_referral,
                    referral_bonus_days=referral_bonus_days,
                )

                if utm_bonus_applied and utm_bonus_days > 0 and not has_referral:
                    user_data = await conn.fetchrow(
                        'SELECT subscription_end FROM users WHERE user_id = $1', user_id
                    )
                    if user_data and user_data['subscription_end']:
                        expiration_date = user_data['subscription_end'].strftime("%d.%m.%Y")
                    else:
                        expiration_date = (
                            datetime.now() + timedelta(days=utm_bonus_days)
                        ).strftime("%d.%m.%Y")
                    day_word = (
                        "день" if utm_bonus_days == 1
                        else "дня" if utm_bonus_days < 5
                        else "дней"
                    )
                    utm_prefix = (
                        f"🎁 Вы получили +{utm_bonus_days} {day_word} "
                        f"<b>Plus</b> по акции!\n"
                        f"Ваш <b>VPN Plus</b> подключен до: {expiration_date}\n\n"
                    )
                    welcome_msg = utm_prefix + welcome_msg

                await message.answer(
                    welcome_msg,
                    reply_markup=await get_main_keyboard(user_id, config),
                    disable_web_page_preview=True,
                    parse_mode='HTML'
                )
            else:
                # Обновляем активность
                await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)
                
                # Логируем UTM визит для существующего пользователя (без привилегий)
                if utm_tag:
                    try:
                        await conn.execute('''
                            INSERT INTO utm_visits (user_id, utm_tag, is_new_user)
                            VALUES ($1, $2, FALSE)
                        ''', user_id, utm_tag)
                    except Exception as e:
                        logger.warning(f"Could not log UTM visit for existing user: {e}")
                
                subscription_status = await get_subscription_status_display(user_id)
                await message.answer(
                    await get_main_text(first_name, subscription_status, user_id),
                    parse_mode="HTML",
                    reply_markup=await get_main_keyboard(user_id, config)
                )


async def get_main_text(first_name: str, subscription_status: str, user_id: int = None, is_new_user: bool = False, has_referral: bool = False) -> str:
    """Возвращает основной текст с объявлением"""
    if is_new_user:
        greeting = f"👋 Добро пожаловать, <b>{first_name}</b>!"
    else:
        greeting = f"👋 Рады видеть тебя снова, <b>{first_name}</b>!"

    marketing = _svoyvpn_tagline_html() + _svoyvpn_why_and_footer_html()
    return f"{greeting}\n\n{subscription_status}\n\n{marketing}"


async def get_main_keyboard(user_id: int, config):
    """Получает главную клавиатуру"""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton, WebAppInfo
    
    builder = InlineKeyboardBuilder()
    
    # Проверяем админа
    if user_id in config.bot.admin_ids:
        builder.row(InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel"))
    
    # Получаем URL для miniapp из APP_URL
    miniapp_url = None
    if config.app_url:
        # v= — смена заставляет Telegram/WebView перезагрузить оболочку (иначе кэш по URL)
        miniapp_url = f"{config.app_url}/miniapp?v=131"
    else:
        # Fallback на localhost для разработки
        import os
        webhook_port = os.getenv("WEBHOOK_PORT", "8080")
        miniapp_url = f"http://localhost:{webhook_port}/miniapp"
    
    # Кнопка WebApp для miniapp
    if miniapp_url:
        builder.row(
            InlineKeyboardButton(
                text="📱 Открыть приложение",
                web_app=WebAppInfo(url=miniapp_url)
            )
        )
    
    # Проверка на Пробный период
    show_trial = False
    try:
        from ..database import get_connection
        async with get_connection() as conn:
            user = await conn.fetchrow("SELECT trial_used, pay_subscribed, subscription_end FROM users WHERE user_id = $1", user_id)
            if user and user['trial_used'] is False:
                # Проверяем, нет ли активной подписки
                is_active = False
                if user['pay_subscribed'] and user['subscription_end']:
                    end_date = user['subscription_end']
                    if isinstance(end_date, str):
                        end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
                    elif hasattr(end_date, 'date'):
                        end_date = end_date.date()
                    is_active = end_date >= datetime.now().date()
                
                if not is_active:
                    trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
                    if trial_settings and trial_settings['days'] and trial_settings['days'] > 0:
                        show_trial = True
    except Exception as e:
        logger.error(f"Error checking trial logic: {e}")
        
    if show_trial:
        builder.row(InlineKeyboardButton(text="🎁 Plus за 1₽ — попробовать", callback_data="activate_trial"))

    show_traffic_boost = False
    try:
        from ..database import get_connection as _gc
        async with _gc() as _conn:
            u2 = await _conn.fetchrow(
                "SELECT pay_subscribed, subscription_end FROM users WHERE user_id = $1",
                user_id,
            )
        if u2 and u2["pay_subscribed"] and u2["subscription_end"]:
            ed = u2["subscription_end"]
            if isinstance(ed, str):
                ed = datetime.strptime(ed.split()[0], "%Y-%m-%d").date()
            elif hasattr(ed, "date"):
                ed = ed.date()
            show_traffic_boost = ed >= datetime.now().date()
    except Exception as e:
        logger.error(f"Error checking subscription for traffic button: {e}")

    if show_traffic_boost:
        builder.row(
            InlineKeyboardButton(text="🚀 Подписка", callback_data="open_tiers"),
            InlineKeyboardButton(text="📶 Лимиты", callback_data="open_bypass_packs"),
        )
        builder.row(
            InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"),
            InlineKeyboardButton(text="📱 Устройства", callback_data="my_devices"),
        )
        builder.row(
            InlineKeyboardButton(text="🎁 Подарок", callback_data="open_invite"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🚀 Подписка", callback_data="open_tiers"),
            InlineKeyboardButton(text="🎁 Подарок", callback_data="open_invite"),
        )
        builder.row(
            InlineKeyboardButton(text="🔗 Получить VPN", callback_data="get_vpn_link"),
        )
    builder.row(
        InlineKeyboardButton(text="🆘 Помощь", callback_data="open_help")
    )
    
    return builder.as_markup()


async def setup_other_handlers(dp, bot: Bot, config):
    """Настраивает дополнительные обработчики (invite, help, admin)"""
    
    @dp.callback_query(F.data == "open_invite")
    @dp.message(Command("invite"))
    async def handle_open_invite(message_or_callback: Message | CallbackQuery):
        """/invite и кнопка «Подарок» → экран подарков."""
        from .balance import render_balance_screen

        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer()
        await render_balance_screen(
            message_or_callback, bot, config, track_referral=True
        )
    
    @dp.callback_query(F.data == "go_back")
    async def go_back_handler(callback: CallbackQuery):
        """Обработчик кнопки Назад"""
        user_id = callback.from_user.id
        first_name = callback.from_user.first_name or "Пользователь"
        from ..subscriptions import get_subscription_status_display
        subscription_status = await get_subscription_status_display(user_id)
        
        await callback.message.edit_text(
            text=await get_main_text(first_name, subscription_status, user_id),
            parse_mode='HTML',
            reply_markup=await get_main_keyboard(user_id, config)
        )
        await callback.answer()
    
    @dp.callback_query(F.data == "open_help")
    @dp.message(Command("help"))
    async def handle_open_help(message_or_callback: Message | CallbackQuery):
        """Обработчик кнопки Помощь и команды /help"""
        if isinstance(message_or_callback, CallbackQuery):
            callback = message_or_callback
            message = callback.message
            await callback.answer()
        else:
            message = message_or_callback
            callback = None
        
        # Получаем ссылку на техподдержку
        from ..database import get_support_link
        support_link = await get_support_link()
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        if support_link:
            builder.row(InlineKeyboardButton(text="🛟 Техподдержка", url=support_link))
        builder.row(
            InlineKeyboardButton(
                text="📄 Оферта и политика конфиденциальности",
                url=OFFER_PRIVACY_TELEGRAPH_URL,
            )
        )
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))

        help_text = (
            "🤖<b>VPN бот</b> — быстрый и надежный VPN сервис\n\n"
            f"📄 <a href=\"{OFFER_PRIVACY_TELEGRAPH_URL}\">Публичная оферта и политика конфиденциальности</a> "
            "— условия услуги и обработки персональных данных.\n\n"
            "<b>Бот предоставляет</b>:\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость подключения\n\n"
            "<b>Как пользоваться</b>?\n"
            "• Купите подписку через /prem\n"
            "• Получите VPN ссылку\n"
            "• Импортируйте ссылку в приложение Happ\n"
            "• Подключитесь!\n\n"
            "<b>Реферальная программа</b>:\n"
            "• Пригласите друга через /invite\n"
            "• За каждого друга — скидка 5% на следующее списание (до 25%)\n"
            "• +5% bypass ГБ от тарифа — вам и другу\n\n"
            "📌 <b>Команды</b>:\n"
            "/start - Перезагрузить бота\n"
            "/prem - Покупка VPN\n"
            "/invite - Пригласи друга\n"
        )
        
        # Если это callback, редактируем сообщение, иначе отправляем новое
        if isinstance(message_or_callback, CallbackQuery):
            await callback.message.edit_text(
                help_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await message.answer(
                help_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    
    # Админ-панель обрабатывается в admin.py
