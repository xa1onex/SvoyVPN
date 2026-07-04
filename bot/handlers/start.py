"""
Обработчик команды /start
"""
import asyncio
import secrets
import random
import logging
from datetime import datetime, timedelta
from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..database import get_connection, generate_subscription_token, ensure_subscription_token
from ..plans import (
    FREE_SUBSCRIPTION_END,
    FREE_TIER_ID,
    get_tier_bypass_gb,
    get_tier_max_devices,
)
from ..subscriptions import (
    create_or_activate_keys_for_all_servers,
    ensure_user_has_subscription,
    get_subscription_status_display,
    get_user_subscription_url,
)
from ..traffic import ensure_bypass_period
from ..help_content import get_help_page_text, help_page_count
from ..custom_emojis import E, e, lbl, btn, emoji_button, raw

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
        "• Мы дарим до 2000₽ всем за использование VPN - /invite\n"
        "• Универсальные тарифы от <b>0₽/год</b>\n"
        "• Стабильный обход <b>Белых списков</b>\n"
        "• И многое другое"
    )
    footer = (
        f"\n\n{E.point_right} Начни прямо сейчас полностью бесплатно! <b>{E.vpn_connect} Подключить VPN</b> жми и все готово!\n\n"
        f"{E.alert_double} Используя сервис, вы соглашаетесь с {offer_link}."
    )
    return why_block + footer


def build_utm_bonus_welcome_message() -> str:
    """Приветствие при регистрации по UTM/рефералу с бонусом (без упоминания дней)."""
    offer_link = (
        f'<a href="{OFFER_PRIVACY_TELEGRAPH_URL}">'
        "офертой и политикой конфиденциальности</a>"
    )
    return (
        "<b>SvoyVPN</b> — стабильный VPN с обходом Белых списков "
        "и с <b>бесплатным</b> тарифом!\n\n"
        "Тебе уже доступны:\n"
        "· <b>Бесплатный</b> обход Белых списков\n"
        "· Стабильные <b>быстрые</b> страны\n"
        "· Работа <b>YouTube/TikTok/ChatGPT</b> и др.\n"
        f'Просто жми кнопку "{E.vpn_connect} Подключить VPN" и все <b>готово</b>!\n\n'
        "А также можешь забрать до <b>2000₽</b> подарками тг - /invite "
        f'(или раздел "{E.gift} Подарок")\n\n'
        f"{E.alert_double} Используя сервис, вы соглашаетесь с {offer_link}."
    )


def get_promo_welcome_keyboard(*, show_trial: bool = True):
    """Клавиатура только для первого приветствия (UTM/реферал с бонусом)."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    if show_trial:
        builder.row(
            btn("Plus за 1₽ — попробовать", "gift",
                callback_data="activate_trial",
            ),
        )
    builder.row(
        btn("Подключить VPN", "vpn_connect", callback_data="get_vpn_link"),
    )
    builder.row(
        btn("Подарок", "gift", callback_data="open_invite"),
        btn("Помощь", "help", callback_data="open_help"),
    )
    return builder.as_markup()


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
            f"{E.gift} Ваш друг подарил вам подписку на <b>{days} дн.</b> — "
            "просто подключайтесь и пользуйтесь свободным интернетом без ограничений"
        )
        return f"{header}{gift_line}\n\n{body}"

    return f"{header}{body}"


def _build_help_keyboard(
    page: int,
    *,
    support_link: str | None,
) -> InlineKeyboardBuilder:
    """Клавиатура справки: навигация по темам + поддержка."""
    builder = InlineKeyboardBuilder()
    total = help_page_count(offer_url=OFFER_PRIVACY_TELEGRAPH_URL)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            btn("Назад", "back", callback_data=f"help_page:{page - 1}")
        )
    if page < total - 1:
        nav.append(
            btn("Вперёд ", "forward", callback_data=f"help_page:{page + 1}")
        )
    if nav:
        builder.row(*nav)
    if support_link:
        builder.row(btn("Техподдержка", "support", url=support_link))
    builder.row(
        btn("Оферта и политика конфиденциальности", "doc",
            url=OFFER_PRIVACY_TELEGRAPH_URL,
        )
    )
    builder.row(btn("В главное меню", "main_menu", callback_data="go_back"))
    return builder


async def _send_help_page(
    message_or_callback: Message | CallbackQuery,
    *,
    page: int = 0,
) -> None:
    from ..database import get_support_link

    support_link = await get_support_link()
    text = get_help_page_text(page, offer_url=OFFER_PRIVACY_TELEGRAPH_URL)
    markup = _build_help_keyboard(page, support_link=support_link).as_markup()

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await message_or_callback.answer(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


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
                f"{E.success} <b>Личность подтверждена!</b>\n\n"
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
                
                await conn.execute(
                    """
                    INSERT INTO users (
                        user_id, username, first_name, registration_date, last_activity,
                        referral_code, invited_by, pay_subscribed, subscription_end, subscription_token,
                        utm_source, subscription_tier, bypass_traffic_limit_gb, device_limit
                    ) VALUES (
                        $1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        $4, NULL, TRUE, $5, $6, $7, $8, $9, $10
                    )
                    """,
                    user_id,
                    username,
                    first_name,
                    new_referral_code,
                    FREE_SUBSCRIPTION_END,
                    sub_token,
                    utm_tag,
                    FREE_TIER_ID,
                    get_tier_bypass_gb(FREE_TIER_ID),
                    get_tier_max_devices(FREE_TIER_ID),
                )
                
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
                            f"{E.user} <b>Новая регистрация</b>\n\n"
                            f"ID: <code>{user_id}</code>\n"
                            f"Имя: {first_name}\n"
                            f"Username: @{username if username else 'нет'}\n"
                            f"Реферальный код: <code>{new_referral_code}</code>\n"
                            f"Источник: {source_info}",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin {admin_id}: {e}")
                
                await ensure_bypass_period(conn, user_id)
                asyncio.create_task(create_or_activate_keys_for_all_servers(user_id))

                use_promo_welcome = (
                    (utm_bonus_applied and utm_bonus_days > 0 and not has_referral)
                    or has_referral
                )

                if use_promo_welcome:
                    from ..trial_usage import should_show_trial_in_main_menu

                    welcome_msg = build_utm_bonus_welcome_message()
                    show_trial_btn = await should_show_trial_in_main_menu(conn, user_id)
                    welcome_keyboard = get_promo_welcome_keyboard(show_trial=show_trial_btn)
                else:
                    welcome_msg = await build_new_user_welcome_message(
                        has_referral=False,
                        referral_bonus_days=0,
                    )
                    welcome_keyboard = await get_main_keyboard(user_id, config)

                await message.answer(
                    welcome_msg,
                    reply_markup=welcome_keyboard,
                    disable_web_page_preview=True,
                    parse_mode='HTML'
                )
            else:
                # Обновляем активность
                await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)

                await ensure_user_has_subscription(
                    user_id,
                    username=username,
                    first_name=first_name,
                    provision_keys=False,
                )

                # Логируем UTM / реферал для существующего пользователя
                if utm_tag:
                    try:
                        await conn.execute(
                            """
                            UPDATE users
                            SET utm_source = COALESCE(NULLIF(TRIM(utm_source), ''), $1)
                            WHERE user_id = $2
                            """,
                            utm_tag,
                            user_id,
                        )
                        await conn.execute(
                            """
                            INSERT INTO utm_visits (user_id, utm_tag, is_new_user)
                            VALUES ($1, $2, FALSE)
                            """,
                            user_id,
                            utm_tag,
                        )
                    except Exception as e:
                        logger.warning(f"Could not log UTM visit for existing user: {e}")

                if referral_code:
                    inviter = await conn.fetchrow(
                        "SELECT user_id FROM users WHERE referral_code = $1",
                        referral_code,
                    )
                    if inviter and inviter["user_id"] != user_id:
                        await conn.execute(
                            """
                            UPDATE users SET invited_by = COALESCE(invited_by, $1)
                            WHERE user_id = $2 AND invited_by IS NULL
                            """,
                            inviter["user_id"],
                            user_id,
                        )

                subscription_status = await get_subscription_status_display(user_id)
                from ..trial_usage import (
                    should_show_trial_in_main_menu,
                    get_trial_days,
                    referral_trial_offer_text,
                )

                show_referral_trial = await should_show_trial_in_main_menu(conn, user_id)
                main_text = await get_main_text(first_name, subscription_status, user_id)
                main_keyboard = await get_main_keyboard(user_id, config)

                await message.answer(
                    main_text,
                    parse_mode="HTML",
                    reply_markup=main_keyboard,
                    disable_web_page_preview=True,
                )

                if show_referral_trial:
                    already = await conn.fetchval(
                        """
                        SELECT 1 FROM user_notifications
                        WHERE user_id = $1 AND notification_type = 'referral_trial_on_return'
                          AND created_at > NOW() - INTERVAL '7 days'
                        """,
                        user_id,
                    )
                    if not already and (utm_tag or referral_code):
                        trial_days = await get_trial_days(conn)
                        if trial_days > 0:
                            from aiogram.utils.keyboard import InlineKeyboardBuilder
                            from aiogram.types import InlineKeyboardButton

                            b = InlineKeyboardBuilder()
                            b.row(
                                btn("Plus за 1₽ — попробовать", "gift",
                                    callback_data="activate_trial",
                                )
                            )
                            await message.answer(
                                referral_trial_offer_text(trial_days),
                                parse_mode="HTML",
                                reply_markup=b.as_markup(),
                            )
                            await conn.execute(
                                """
                                INSERT INTO user_notifications (user_id, notification_type)
                                VALUES ($1, $2)
                                """,
                                user_id,
                                "referral_trial_on_return",
                            )


async def get_main_text(first_name: str, subscription_status: str, user_id: int = None, is_new_user: bool = False, has_referral: bool = False) -> str:
    """Возвращает основной текст с объявлением"""
    from ..database import get_announcement_text

    ann = await get_announcement_text()

    if is_new_user:
        greeting = f"{E.wave} Добро пожаловать, <b>{first_name}</b>!"
    else:
        greeting = f"{E.wave} Рады видеть тебя снова, <b>{first_name}</b>!"

    parts = [greeting, "", subscription_status]
    if ann and ann.strip():
        parts.extend(["", ann.strip()])
    return "\n".join(parts)


async def should_show_devices_menu(user_id: int) -> bool:
    """Сброс устройств нужен только на Free (на Plus — безлимит)."""
    from ..plans import ALL_PAID_TIER_IDS, is_sentinel_subscription_end, is_subscription_active

    try:
        from ..database import get_connection
        async with get_connection() as conn:
            u = await conn.fetchrow(
                """
                SELECT subscription_tier, pay_subscribed, subscription_end
                FROM users WHERE user_id = $1
                """,
                user_id,
            )
        if u:
            tier = u["subscription_tier"] or "free"
            if (
                tier in ALL_PAID_TIER_IDS
                and is_subscription_active(u["pay_subscribed"], u["subscription_end"])
                and not is_sentinel_subscription_end(u["subscription_end"])
            ):
                return False
    except Exception as e:
        logger.error("should_show_devices_menu: %s", e)
    return True


async def get_main_keyboard(user_id: int, config):
    """Получает главную клавиатуру"""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    
    # Проверяем админа
    if user_id in config.bot.admin_ids:
        builder.row(btn("Админ панель", "admin", callback_data="admin_panel"))
    
    # Проверка на Пробный период
    show_trial = False
    try:
        from ..database import get_connection
        from ..trial_usage import should_show_trial_in_main_menu

        async with get_connection() as conn:
            show_trial = await should_show_trial_in_main_menu(conn, user_id)
    except Exception as e:
        logger.error(f"Error checking trial logic: {e}")
        
    if show_trial:
        builder.row(btn("Plus за 1₽ — попробовать", "gift", callback_data="activate_trial"))

    from ..menu_labels import GIFT_BUTTON

    builder.row(
        btn("Помощь", "help", callback_data="open_help"),
        btn("Лимиты", "limits", callback_data="open_bypass_packs"),
    )
    if await should_show_devices_menu(user_id):
        builder.row(
            btn("Устройства", "devices", callback_data="my_devices"),
        )
    builder.row(
        btn("Подписка", "subscription", callback_data="open_tiers"),
        InlineKeyboardButton(text=GIFT_BUTTON, callback_data="open_invite"),
    )
    builder.row(
        btn("Подключить VPN", "vpn_connect", callback_data="get_vpn_link"),
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
        
        try:
            await callback.message.edit_text(
                text=await get_main_text(first_name, subscription_status, user_id),
                parse_mode='HTML',
                reply_markup=await get_main_keyboard(user_id, config),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()
    
    @dp.message(Command("test_delete_user"))
    async def handle_test_delete_user(message: Message):
        """Временная команда: полное удаление себя из сервиса (для тестов)."""
        from ..test_user_purge import purge_user_completely

        user_id = message.from_user.id

        async with get_connection() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM users WHERE user_id = $1",
                user_id,
            )
        if not exists:
            await message.answer(
                "В базе вас нет — вы уже «новый пользователь». Нажмите /start.",
            )
            return

        await message.answer("Удаляю ваш аккаунт и все данные…")
        try:
            result = await purge_user_completely(user_id)
        except Exception as e:
            logger.error("test_delete_user failed user=%s: %s", user_id, e, exc_info=True)
            await message.answer(
                f"{E.error} Не удалось удалить аккаунт полностью. Попробуйте позже или напишите в поддержку.",
            )
            return

        if not result.get("found"):
            await message.answer("Запись не найдена (возможно, уже удалена).")
            return

        deleted = result.get("deleted") or {}
        lines = [
            f"• {name}: {cnt}" for name, cnt in sorted(deleted.items()) if cnt
        ]
        summary = "\n".join(lines) if lines else "• записей в связанных таблицах не было"
        xui_ok = result.get("xui_ok", 0)
        xui_err = result.get("xui_errors", 0)

        await message.answer(
            f"{E.success} <b>Аккаунт удалён</b>\n\n"
            f"<b>БД:</b>\n{summary}\n\n"
            f"<b>X-UI:</b> удалено {xui_ok}, ошибок {xui_err}\n\n"
            "Нажмите /start — зарегистрируетесь заново, как новый пользователь.",
            parse_mode="HTML",
        )

    @dp.callback_query(F.data == "open_help")
    @dp.message(Command("help"))
    async def handle_open_help(message_or_callback: Message | CallbackQuery):
        """Помощь — постраничная справка."""
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer()
        await _send_help_page(message_or_callback, page=0)

    @dp.callback_query(F.data.startswith("help_page:"))
    async def handle_help_page(callback: CallbackQuery):
        """Листание тем справки."""
        try:
            page = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            page = 0
        total = help_page_count(offer_url=OFFER_PRIVACY_TELEGRAPH_URL)
        page = max(0, min(page, total - 1))
        await callback.answer()
        await _send_help_page(callback, page=page)

    # Админ-панель обрабатывается в admin.py
