"""
Обработчики подписки и получения VPN ссылки
"""
import logging
from datetime import datetime
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..database import get_connection, ensure_subscription_token
from ..subscriptions import get_subscription_status, get_user_subscription_url
from ..plans import get_subscription_plans, get_renewal_plans, format_price_rub, format_price_stars, format_price_both, PAYMENT_METHODS
from ..config import AppConfig

logger = logging.getLogger(__name__)


async def setup_subscription_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики подписки"""
    
    @dp.callback_query(F.data == "get_vpn_link")
    async def handle_get_vpn_link(callback: CallbackQuery):
        """Отправляет пользователю ссылку подписки"""
        user_id = callback.from_user.id
        link = await get_user_subscription_url(user_id)
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
        text, builder = await build_subscription_message(info, state)
        
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
        text, builder = await build_subscription_message(info, state)
        
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


async def build_subscription_message(info: dict, state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    """Строит сообщение и клавиатуру для подписки"""
    builder = InlineKeyboardBuilder()
    is_active = info['is_active']
    days_remaining = info['days_remaining']
    end_date_str = info['end_date_str']
    user_id = info['user_id']
    
    subscription_url = await get_user_subscription_url(user_id)
    
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
        
        # Показываем планы продления
        renewal_plans = await get_renewal_plans()
        if renewal_plans:
            text += "💳 <b>Продлить подписку:</b>\n"
            for plan_id, plan_data in list(renewal_plans.items())[:3]:  # Показываем первые 3
                price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
                text += f"• {plan_data['title']} - {price_text}\n"
            text += "\n"
        
        builder.row(InlineKeyboardButton(text="💳 Продлить подписку", callback_data="show_renewal_plans"))
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
    
    return text, builder.as_markup()


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
    
    @dp.callback_query(F.data.startswith("buy_subscription:"))
    async def handle_buy_subscription(callback: CallbackQuery):
        """Обработка покупки подписки"""
        # TODO: Реализовать создание инвойса
        await callback.answer("Функция в разработке", show_alert=True)
    
    @dp.callback_query(F.data.startswith("buy_renewal:"))
    async def handle_buy_renewal(callback: CallbackQuery):
        """Обработка продления подписки"""
        # TODO: Реализовать создание инвойса
        await callback.answer("Функция в разработке", show_alert=True)
    
    @dp.callback_query(F.data == "go_back")
    async def handle_go_back(callback: CallbackQuery, state: FSMContext):
        """Возврат в главное меню"""
        user_id = callback.from_user.id
        info = await get_subscription_info(user_id)
        text, builder = await build_subscription_message(info, state)
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
