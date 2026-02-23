"""
Обработчик команды /start
"""
import secrets
import logging
from datetime import datetime, timedelta
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command

from ..database import get_connection, generate_subscription_token, ensure_subscription_token
from ..subscriptions import get_subscription_status, get_user_subscription_url

logger = logging.getLogger(__name__)


async def setup_start_handler(dp, bot: Bot, config):
    """Настраивает обработчик /start"""
    
    @dp.message(CommandStart())
    async def handle_start(message: Message):
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name or "Пользователь"
        args = message.text.split()
        
        # Парсим реферальный код
        referral_code = args[1][4:] if len(args) > 1 and args[1].startswith('ref_') else None
        
        async with get_connection() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            
            if not user:
                # Создаём нового пользователя
                new_referral_code = secrets.token_hex(4)
                sub_token = generate_subscription_token()
                
                await conn.execute('''
                    INSERT INTO users (
                        user_id, username, first_name, registration_date, last_activity,
                        referral_code, invited_by, pay_subscribed, subscription_end, subscription_token
                    ) VALUES ($1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $4, NULL, FALSE, NULL, $5)
                ''', user_id, username, first_name, new_referral_code, sub_token)
                
                # Обработка реферального кода
                has_referral = False
                if referral_code:
                    inviter = await conn.fetchrow('SELECT user_id FROM users WHERE referral_code = $1', referral_code)
                    
                    if inviter:
                        # Получаем настройки реферальной системы
                        referral_settings = await conn.fetchrow(
                            'SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1'
                        )
                        if not referral_settings:
                            inviter_bonus_days = 5
                            invited_bonus_days = 3
                        else:
                            inviter_bonus_days = referral_settings['inviter_bonus_days']
                            invited_bonus_days = referral_settings['invited_bonus_days']
                        
                        inviter_id = inviter['user_id']
                        
                        # ✅ ИСПРАВЛЕНО: Используем параметризованные запросы
                        await conn.execute('''
                            UPDATE users SET
                                referral_count = referral_count + 1,
                                subscription_end = CASE 
                                    WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                                    THEN CURRENT_DATE + INTERVAL $2 || ' days'
                                    ELSE subscription_end + INTERVAL $2 || ' days'
                                END,
                                pay_subscribed = TRUE
                            WHERE user_id = $1
                        ''', inviter_id, str(inviter_bonus_days))
                        
                        await conn.execute('''
                            UPDATE users SET
                                invited_by = $1,
                                subscription_end = CURRENT_DATE + INTERVAL $3 || ' days',
                                pay_subscribed = TRUE
                            WHERE user_id = $2
                        ''', inviter_id, user_id, str(invited_bonus_days))
                        
                        # Уведомление пригласившему
                        try:
                            end_date = datetime.now() + timedelta(days=inviter_bonus_days)
                            await bot.send_message(
                                inviter_id,
                                f"🎉 Вы получили +{inviter_bonus_days} дней VPN за приглашение друга!\n"
                                f"Теперь ваш VPN активен до: {end_date.strftime('%d.%m.%Y')}"
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
                        
                        has_referral = True
                
                # Уведомление админам
                referral_info = "по реферальной ссылке" if has_referral else "без рефералки"
                for admin_id in config.bot.admin_ids:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"👤 <b>Новая регистрация</b>\n\n"
                            f"ID: <code>{user_id}</code>\n"
                            f"Имя: {first_name}\n"
                            f"Username: @{username if username else 'нет'}\n"
                            f"Реферальный код: <code>{new_referral_code}</code>\n"
                            f"Регистрация: {referral_info}",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin {admin_id}: {e}")
                
                # Приветствие
                subscription_status = await get_subscription_status(user_id)
                await message.answer(
                    await get_main_text(first_name, subscription_status, user_id),
                    parse_mode="HTML",
                    reply_markup=await get_main_keyboard(user_id, config)
                )
            else:
                # Обновляем активность
                await conn.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)
                
                subscription_status = await get_subscription_status(user_id)
                await message.answer(
                    await get_main_text(first_name, subscription_status, user_id),
                    parse_mode="HTML",
                    reply_markup=await get_main_keyboard(user_id, config)
                )


async def get_main_text(first_name: str, subscription_status: str, user_id: int = None) -> str:
    """Возвращает основной текст с объявлением"""
    from ..database import get_connection
    
    ann = ""
    async with get_connection() as conn:
        ann_row = await conn.fetchrow('SELECT text FROM announcements ORDER BY id DESC LIMIT 1')
        if ann_row:
            ann = ann_row['text']
    
    msg = (
        f"👋 Рады видеть тебя снова, <b>{first_name}</b>!\n\n"
        f"<b>VPN</b>: <i>{subscription_status}</i>\n\n"
        f"📌 <b>Команды:</b>\n"
        "<i>/start</i> - Перезагрузить бота\n"
        "<i>/prem</i> - Покупка VPN\n"
        "<i>/invite</i> - Пригласи друга\n\n"
        f"{ann}"
    )
    return msg


async def get_main_keyboard(user_id: int, config):
    """Получает главную клавиатуру"""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    
    # Проверяем админа
    if user_id in config.bot.admin_ids:
        builder.row(InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel"))
    
    builder.row(
        InlineKeyboardButton(text="💳 Подписка", callback_data="open_premium"),
        InlineKeyboardButton(text="🎁 Подарок", callback_data="open_invite")
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
        """Обработчик кнопки Подарок и команды /invite (реферальная система)"""
        if isinstance(message_or_callback, CallbackQuery):
            callback = message_or_callback
            message = callback.message
            await callback.answer()
        else:
            message = message_or_callback
            callback = None
        
        user_id = message.from_user.id
        from urllib.parse import quote
        
        async with get_connection() as conn:
            user_data = await conn.fetchrow(
                "SELECT referral_code, referral_count FROM users WHERE user_id = $1",
                user_id
            )
            
            if not user_data:
                await message.answer("❌ Пожалуйста, сначала запустите бота с помощью команды /start")
                return
            
            referral_code = user_data.get("referral_code", "")
            referral_count = user_data.get("referral_count", 0)
            
            # Если реферальный код отсутствует, генерируем новый
            if not referral_code:
                import secrets
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
            f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>\n\n"
            f"👥 Приглашено друзей: <i>{referral_count or 0}</i>\n"
            f"За каждого друга вы получаете +{inviter_days} {'день' if inviter_days == 1 else 'дня' if inviter_days < 5 else 'дней'} VPN, а друг получает +{invited_days} {'день' if invited_days == 1 else 'дня' if invited_days < 5 else 'дней'}!"
        )
        
        # Клавиатура с кнопкой поделиться
        from aiogram.types import InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📤 Поделиться",
                url=f"https://t.me/share/url?url={ref_link}&text={quote('Присоединяйся к VPN боту с моей подпиской!')}"
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
        ])
        
        await message.answer(text, parse_mode='HTML', reply_markup=keyboard, disable_web_page_preview=True)
    
    @dp.callback_query(F.data == "go_back")
    async def go_back_handler(callback: CallbackQuery):
        """Обработчик кнопки Назад"""
        user_id = callback.from_user.id
        first_name = callback.from_user.first_name or "Пользователь"
        from ..subscriptions import get_subscription_status
        subscription_status = await get_subscription_status(user_id)
        
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
        help_text = (
            "🆘 <b>Помощь</b>\n\n"
            "📱 <b>Как использовать VPN:</b>\n"
            "1. Купите подписку через кнопку <b>💳 Подписка</b>\n"
            "2. Нажмите <b>🔗 Получить VPN</b> для получения ссылки подписки\n"
            "3. Добавьте ссылку в приложение (v2rayNG, v2rayN, sing-box и т.п.)\n"
            "4. Обновите подписку в приложении\n\n"
            "💳 <b>Способы оплаты:</b>\n"
            "• Telegram Stars\n"
            "• ЮKassa (банковская карта)\n\n"
            "❓ <b>Вопросы?</b>\n"
            "Обратитесь в поддержку: @xdouble_support"
        )
        if isinstance(message_or_callback, CallbackQuery):
            await callback.message.answer(help_text, parse_mode="HTML")
            await callback.answer()
        else:
            await message.answer(help_text, parse_mode="HTML")
    
    # Админ-панель обрабатывается в admin.py
