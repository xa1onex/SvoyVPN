"""
Обработчики подписки и получения VPN ссылки
"""
import html
import logging
import re
import time
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
from ..device_fingerprint import (
    SUBSCRIPTION_DEVICE_COUNTABLE_SQL,
    format_device_display_name,
)
from ..custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)

_SUB_DEVICE_FILTER_SQL = f"({SUBSCRIPTION_DEVICE_COUNTABLE_SQL.strip()})"


async def _build_my_devices_view(conn, user_id: int) -> tuple[str, InlineKeyboardBuilder]:
    device_limit = await conn.fetchval(
        "SELECT COALESCE(device_limit, 5) FROM users WHERE user_id = $1", user_id
    ) or 5
    q = f"""
        SELECT device_fingerprint AS fp,
               (array_agg(device_model ORDER BY timestamp DESC NULLS LAST))[1] AS device_model,
               (array_agg(user_agent ORDER BY timestamp DESC))[1] AS user_agent
        FROM subscription_usage_logs
        WHERE user_id = $1
          AND timestamp >= NOW() - INTERVAL '6 hours'
          AND device_fingerprint IS NOT NULL
          AND {_SUB_DEVICE_FILTER_SQL}
        GROUP BY device_fingerprint
        ORDER BY MAX(timestamp) DESC
    """
    devices = await conn.fetch(q, user_id)
    count = len(devices)
    text = f"{E.devices} <b>Подключённые устройства</b> ({count}/{device_limit})\n\n"
    builder = InlineKeyboardBuilder()
    if devices:
        for i, d in enumerate(devices, 1):
            label = (d["device_model"] or "").strip() or format_device_display_name(
                d["user_agent"] or ""
            )
            text += f"{i}. {html.escape(label)}\n"
            fp = d["fp"]
            if fp:
                builder.row(
                    btn("{i}", "trash",
                        callback_data=f"rm_dev:{fp}",
                    )
                )
    else:
        text += "Нет подключённых устройств.\n"

    if count > device_limit:
        text += f"\n{E.warning} Лимит превышен ({count}/{device_limit})."

    if count > 0:
        builder.row(btn("Сбросить все сессии", "refresh", callback_data="reset_devices"))
    builder.row(btn("Назад", "back", callback_data="go_back_subscription"))
    return text, builder


_DEVICE_NAMES = {
    "apple": "iPhone / iPad",
    "android": "Android",
    "windows": "Windows",
    "mac": "macOS",
}


async def send_traffic_packs_menu(bot: Bot, event: Message | CallbackQuery, config: AppConfig, *, edit: bool = False) -> None:
    """Список пакетов доп. ГБ (из главного меню, /start traffic или кнопки «Лимит»)."""
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
    else:
        message = event
        user_id = event.from_user.id

    async with get_connection() as conn:
        ok_sub = await conn.fetchval(
            """
            SELECT CASE
                WHEN pay_subscribed = TRUE AND subscription_end IS NOT NULL
                     AND DATE(subscription_end) >= CURRENT_DATE
                THEN TRUE ELSE FALSE END
            FROM users WHERE user_id = $1
            """,
            user_id,
        )
        packs = await conn.fetch(
            """
            SELECT id, title, gb_amount, price_rub, price_stars
            FROM gb_pack_products
            WHERE is_active = TRUE
            ORDER BY gb_amount ASC, display_order ASC, id ASC
            """,
        )

    if not ok_sub:
        text = (
            f"{E.signal} <b>Дополнительный трафик</b>\n\n"
            "Доступно только при <b>активной подписке</b>.\n"
            "Сначала оформи или продли VPN в разделе «Подписка»."
        )
    elif not packs:
        text = (
            f"{E.signal} <b>Дополнительный трафик</b>\n\n"
            "Нехватило трафика? Купи дополнительный объём пакетов гб\n"
            "Пакеты сейчас недоступны. Загляни позже или напиши в поддержку."
        )
    else:
        parts = [
            f"{E.signal} <b>Увеличить лимит трафика</b>\n",
            "Дополнительный объём суммируется с месячным лимитом.\n",
        ]
        for p in packs:
            title_esc = html.escape(str(p["title"]).strip())
            rub_line = format_price_rub(int(p["price_rub"] or 0)) if int(p["price_rub"] or 0) > 0 else "—"
            parts.append(
                f"• <b>+{int(p['gb_amount'])} ГБ</b> · {title_esc} · {rub_line}"
            )
        text = "\n".join(parts)

    builder = InlineKeyboardBuilder()
    if ok_sub and packs:
        for p in packs:
            builder.row(
                InlineKeyboardButton(
                    text=f"+{p['gb_amount']} ГБ — {p['title'][:24]}",
                    callback_data=f"traffic_pack_choose:{int(p['id'])}",
                )
            )
    builder.row(btn("Назад", "back", callback_data="open_premium"))
    markup = builder.as_markup()

    if edit and isinstance(event, CallbackQuery):
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


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

    def _device_select_markup() -> InlineKeyboardBuilder:
        builder = InlineKeyboardBuilder()
        builder.row(btn("iPhone / iPad", "devices", callback_data="ob_dev_apple"))
        builder.row(btn("Android", "android", callback_data="ob_dev_android"))
        builder.row(btn("Windows", "laptop", callback_data="ob_dev_windows"))
        builder.row(btn("macOS", "desktop", callback_data="ob_dev_mac"))
        builder.row(btn("Назад", "back", callback_data="go_back_subscription"))
        return builder

    def _device_select_text() -> str:
        return (
            f"{E.laptop} <b>Выберите ваше устройство</b>\n\n"
            "На чем вы будете использовать VPN? Мы подготовили пошаговую инструкцию для каждой платформы."
        )

    async def _show_device_select(callback: CallbackQuery, *, delete_current: bool = False) -> None:
        text = _device_select_text()
        markup = _device_select_markup().as_markup()
        chat_id = callback.message.chat.id

        if delete_current:
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
            return

        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest:
            if callback.message.photo:
                try:
                    await callback.message.delete()
                except TelegramBadRequest:
                    pass
                await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
            else:
                await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)

    async def _send_happ_instruction(callback: CallbackQuery, device: str) -> None:
        from ..vpn_onboarding import build_happ_instruction_async, device_instruction_photo
        from ..database import get_device_instruction_photos

        if device not in _DEVICE_NAMES:
            await callback.answer(f"{E.error} Неизвестное устройство")
            return

        user_id = callback.from_user.id
        token = await ensure_subscription_token(user_id)
        text, builder = await build_happ_instruction_async(
            device, user_id, token=token, config=config
        )
        markup = builder.as_markup()
        chat_id = callback.message.chat.id
        message_kw = dict(parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        photo_kw = dict(parse_mode="HTML", reply_markup=markup)

        db_photos = await get_device_instruction_photos(device)
        local_photo = device_instruction_photo(device)

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

        if db_photos:
            for file_id in db_photos:
                try:
                    await bot.send_photo(chat_id, file_id)
                except Exception:
                    continue
            await bot.send_message(chat_id, text, **message_kw)
        elif local_photo:
            if len(text) <= 1020:
                await bot.send_photo(chat_id, local_photo, caption=text, **photo_kw)
            else:
                await bot.send_photo(chat_id, local_photo)
                await bot.send_message(chat_id, text, **message_kw)
        else:
            await bot.send_message(chat_id, text, **message_kw)
        await callback.answer()

    @dp.callback_query(F.data == "get_vpn_link")
    async def handle_get_vpn_link(callback: CallbackQuery):
        """Отправляет пользователю выбор устройства"""
        await _show_device_select(callback)
        await callback.answer()

    @dp.callback_query(F.data == "ob_back_devices")
    async def handle_ob_back_devices(callback: CallbackQuery):
        """Назад из инструкции — снова выбор устройства"""
        await _show_device_select(callback, delete_current=True)
        await callback.answer()

    @dp.callback_query(F.data.startswith("ob_dev_"))
    async def handle_ob_device(callback: CallbackQuery):
        """Инструкция Happ для выбранного устройства"""
        device = callback.data.replace("ob_dev_", "")
        await _send_happ_instruction(callback, device)

    @dp.callback_query(F.data.startswith("ob_app_"))
    async def handle_ob_app_legacy(callback: CallbackQuery):
        """Старые кнопки выбора приложения → инструкция Happ"""
        parts = callback.data.split("_")
        if len(parts) < 4:
            await callback.answer()
            return
        await _send_happ_instruction(callback, parts[2])

    @dp.callback_query(F.data.startswith("ob_photos_"))
    async def handle_ob_photos_legacy(callback: CallbackQuery):
        """Старые кнопки «фото-инструкция» → полная инструкция Happ"""
        device = callback.data.replace("ob_photos_", "")
        await _send_happ_instruction(callback, device)

    @dp.callback_query(F.data == "open_premium")
    async def handle_open_premium(callback: CallbackQuery, state: FSMContext):
        """Обработчик кнопки Premium — перенаправляем на новые тарифы"""
        from .tiers import build_tiers_message
        user_id = callback.from_user.id
        text, markup = await build_tiers_message(user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()
    
    @dp.message(Command("prem"))
    async def handle_prem_command(message: Message, state: FSMContext):
        """Обработчик команды /prem — перенаправляем на новые тарифы"""
        from .tiers import build_tiers_message
        text, markup = await build_tiers_message(message.from_user.id)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    
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
                'end_date_obj': None,
                'user_id': user_id
            }
        
        subscription_end = user_data['subscription_end']
        pay_subscribed = user_data['pay_subscribed']
        is_active = False
        days_remaining = 0
        end_date_str = None
        end_date_obj = None
        
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
                    end_date_obj = end_date
            except Exception as e:
                logger.error(f"Error parsing subscription date: {e}")
        
        return {
            'is_active': is_active,
            'days_remaining': days_remaining,
            'end_date_str': end_date_str,
            'end_date_obj': end_date_obj,
            'user_id': user_id
        }


async def build_subscription_message(info: dict, state: FSMContext, config: AppConfig) -> tuple[str, InlineKeyboardBuilder]:
    """Формирует сообщение и клавиатуру для подписки/продления (Plus 1 мес / 12 мес)."""
    user_id = info['user_id']
    from bot.plans import get_user_tariffs, format_price_rub
    
    current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
    
    builder = InlineKeyboardBuilder()

    from ..trial_usage import user_show_referral_trial_offer, get_trial_days

    show_referral_trial = False
    async with get_connection() as conn:
        show_referral_trial = await user_show_referral_trial_offer(conn, user_id)
        trial_days = await get_trial_days(conn) if show_referral_trial else 0
    
    if is_renew:
        from ..subscriptions import get_subscription_status_display
        status_line = await get_subscription_status_display(user_id)
        text = f"{E.success} {status_line}\n\n"
        text += f"{E.bulb} Продлите Plus в любое время:\n\n"
        for plan_id, plan_data in current_tariffs.items():
            text += f"• <b>{plan_data['title']}</b> — {format_price_rub(plan_data['price_rub'])}\n"
        text += "\n"
    else:
        if show_referral_trial and trial_days > 0:
            text = (
                f"{E.gift} <b>Plus за 1₽</b>\n\n"
                f"Специальное предложение для вас — <b>{trial_days} дней</b> Plus "
                f"с автопродлением по актуальной цене.\n\n"
                "• 50 ГБ bypass в месяц\n"
                "• YouTube / TikTok / AI\n"
                "• Безлимит устройств\n\n"
            )
            builder.row(
                btn("Plus за 1₽ — попробовать", "gift",
                    callback_data="activate_trial",
                ),
            )
        else:
            text = (
                f"{E.error} <b>VPN неактивен</b>\n\n"
                "Оформите <b>Plus</b> — быстрый VPN с обходом блокировок:\n"
                "• 50 ГБ bypass в месяц\n"
                "• YouTube / TikTok / AI\n"
                "• Безлимит устройств\n\n"
            )
            for plan_id, plan_data in current_tariffs.items():
                text += f"• <b>{plan_data['title']}</b> — {format_price_rub(plan_data['price_rub'])}\n"
            text += "\n"
            for plan_id, plan_data in current_tariffs.items():
                builder.button(
                    text=f"{plan_data['title']} — {format_price_rub(plan_data['price_rub'])}",
                    callback_data=f"tier_pay:{plan_id}",
                )
            builder.adjust(1)

    if is_renew:
        for plan_id, plan_data in current_tariffs.items():
            builder.button(
                text=f"{plan_data['title']} — {format_price_rub(plan_data['price_rub'])}",
                callback_data=f"tier_pay:{plan_id}",
            )
        builder.adjust(1)

    builder.row(btn("Тарифы Plus", "subscription", callback_data="open_tiers"))
    builder.row(btn("Увеличить лимит трафика", "signal", callback_data="open_traffic_packs"))
    if not show_referral_trial and not is_renew:
        builder.row(
            btn("Пригласи друга — получи бонус", "gift",
                callback_data="open_invite",
            ),
        )
    builder.row(btn("Назад", "back", callback_data="go_back_subscription"))
    
    return text, builder


async def setup_subscription_plan_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики для показа планов подписки"""
    
    @dp.callback_query(F.data == "show_subscription_plans")
    async def handle_show_subscription_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы подписки"""
        user_id = callback.from_user.id
        from bot.plans import get_user_tariffs, format_price_rub
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        text = f"{E.card} <b>Выберите план подписки:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in current_tariffs.items():
            price_text = format_price_rub(plan_data['price_rub'])
            text += f"• <b>{plan_data['title']}</b> - {price_text}\n"
            text += f"  Срок: {plan_data['duration']} месяцев\n"
            text += f"  Трафик: {plan_data.get('traffic_gb', 'Безлимитный')} ГБ\n\n"
        
        action = "buy_renewal" if is_renew else "buy_subscription"
        
        for plan_id, plan_data in list(current_tariffs.items())[:2]:
            if config.yookassa.enabled:
                builder.row(
                    btn("{plan_data['title']} ({format_price_rub(plan_data['price_rub'])})", "card",
                        callback_data=f"{action}:{plan_id}:yookassa"
                    )
                )
            if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
                builder.row(
                    btn("{plan_data['title']} ({format_price_rub(plan_data['price_rub'])})", "plus",
                        callback_data=f"{action}:{plan_id}:cryptopay"
                    )
                )
        
        builder.row(btn("Назад", "back", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data == "show_renewal_plans")
    async def handle_show_renewal_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы продления"""
        user_id = callback.from_user.id
        from bot.plans import get_user_tariffs, format_price_rub
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        text = f"{E.card} <b>Продлить подписку:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in current_tariffs.items():
            text += f"• <b>{plan_data['title']}</b> - {format_price_rub(plan_data['price_rub'])}\n"
        
        action = "buy_renewal" if is_renew else "buy_subscription"
        for plan_id, plan_data in list(current_tariffs.items())[:2]:
            if config.yookassa.enabled:
                builder.row(
                    btn("{plan_data['title']} ({format_price_rub(plan_data['price_rub'])})", "card",
                        callback_data=f"{action}:{plan_id}:yookassa"
                    )
                )
            if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
                builder.row(
                    btn("{plan_data['title']} ({format_price_rub(plan_data['price_rub'])})", "plus",
                        callback_data=f"{action}:{plan_id}:cryptopay"
                    )
                )
        
        builder.row(btn("Назад", "back", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data.startswith("plan:"))
    async def handle_select_plan(callback: CallbackQuery, state: FSMContext):
        """Обработчик выбора плана (показывает методы оплаты)"""
        plan_id = callback.data.split(":")[1]
        user_id = callback.from_user.id
        
        from bot.plans import get_user_tariffs, is_active_tier_plan, is_legacy_subscription_plan

        if is_legacy_subscription_plan(plan_id):
            await callback.answer(f"{E.error} Этот тариф больше не доступен. Выберите Plus.", show_alert=True)
            return

        if is_active_tier_plan(plan_id):
            b = InlineKeyboardBuilder()
            b.row(btn("Оплатить Plus", "card",
                callback_data=f"tier_pay:{plan_id}",
            ))
            b.row(btn("Все тарифы", "plus", callback_data="open_tiers"))
            await callback.message.edit_text(
                "Тарифы обновились. Доступны только <b>Plus на месяц</b> и <b>Plus на год</b>.",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
            return
        
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        if plan_id not in current_tariffs:
            await callback.answer(f"{E.error} План недоступен или не найден", show_alert=True)
            return
            
        plan_data = current_tariffs[plan_id]
        
        # Определяем действие
        action = "buy_renewal" if is_renew else "buy_subscription"
        
        # Показываем методы оплаты
        text = f"{E.card} <b>{plan_data['title']}</b>\n\n"
        text += f"Срок: {plan_data['duration']} месяцев\n"
        text += "Лимит трафика — месячный, по дню покупки подписки (см. приложение).\n\n"
        text += "Выберите способ оплаты:"
        
        builder = InlineKeyboardBuilder()
        
        if config.yookassa.enabled:
            builder.row(
                btn("Банковская карта ({format_price_rub(plan_data['price_rub'])})", "card",
                    callback_data=f"{action}:{plan_id}:yookassa"
                )
            )
            
        # Кнопка для оплаты Crypto Pay (если включена)
        if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
            builder.row(
                btn("Crypto Pay ({format_price_rub(plan_data['price_rub'])})", "plus",
                    callback_data=f"{action}:{plan_id}:cryptopay"
                )
            )
        
        builder.row(btn("Назад", "back", callback_data="open_premium"))
        
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
        # Парсим callback_data: buy_subscription:plan_id:method_id
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer(f"{E.error} Неверный формат данных", show_alert=True)
            return
        
        plan_id = parts[1]
        method_id = parts[2]
        user_id = callback.from_user.id

        from bot.plans import get_user_tariffs, is_active_tier_plan, is_legacy_subscription_plan

        if is_legacy_subscription_plan(plan_id):
            await callback.answer(f"{E.error} Тариф устарел. Используйте Plus.", show_alert=True)
            return
        if is_active_tier_plan(plan_id):
            await callback.answer("Используйте кнопку «Тарифы Plus» для оплаты", show_alert=True)
            return
        
        # Получаем актуальные планы
        current_tariffs, user_is_renew, _ = await get_user_tariffs(user_id)
        
        # Убедимся, что тариф доступен
        if plan_id not in current_tariffs:
            await callback.answer(f"{E.error} План недоступен или устарел", show_alert=True)
            return
            
        plan_data = current_tariffs[plan_id]
        
        # Валидация метода оплаты
        if method_id == "stars":
            await callback.answer(
                "Оплата подписки Stars недоступна. Используйте карту.",
                show_alert=True,
            )
            return
        if method_id not in PAYMENT_METHODS:
            await callback.answer(f"{E.error} Неизвестный метод оплаты", show_alert=True)
            return
        
        method_data = PAYMENT_METHODS[method_id]
        
        # Определяем цену
        currency_type = 'stars' if method_data['currency'] == 'XTR' else 'rub'
        price_key = f"price_{currency_type}"
        price = plan_data.get(price_key)
        
        if price is None:
            await callback.answer(f"{E.error} Цена не найдена для плана. Ключ: {price_key}", show_alert=True)
            logger.error(f"Price key '{price_key}' not found in plan_data for plan {plan_id}")
            return
        
        # Преобразуем цену в int
        try:
            price = int(float(price))
        except (ValueError, TypeError) as e:
            await callback.answer(f"{E.error} Ошибка: неверный формат цены", show_alert=True)
            logger.error(f"Invalid price format for plan {plan_id}: {price}, error: {e}")
            return
        
        if price <= 0:
            await callback.answer(f"{E.error} Ошибка: цена должна быть больше нуля", show_alert=True)
            logger.error(f"Invalid price value for plan {plan_id}: {price}")
            return
        
        # Обработка разных методов оплаты
        if method_id == "yookassa":
            # Оплата через ЮKassa
            if not config.yookassa.enabled:
                await callback.answer(f"{E.error} ЮKassa не настроена", show_alert=True)
                return
            
            # Проверяем минимальную сумму для ЮKassa (минимум 1 рубль = 100 копеек)
            if price < 100:
                await callback.answer(f"{E.error} Минимальная сумма оплаты - 1 рубль", show_alert=True)
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
                # The original metadata was a dictionary. The instruction implies changing to a colon-separated string.
                # Assuming the intent is to store a colon-separated string within the metadata.
                # The provided edit was malformed, so we'll add a 'payload' key to the metadata dictionary.
                payload_str = f"{callback.from_user.id}:{plan_id}:{method_id}"
                payment_data = yookassa_client.create_payment(
                    amount=amount_rub,
                    description=f"VPN подписка - {plan_data['title']}",
                    return_url=f"https://t.me/{bot_username}?start=payment_success",
                    metadata={
                        "user_id": callback.from_user.id,
                        "plan_id": plan_id,
                        "method_id": method_id,
                        "payload": payload_str
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
                builder.row(btn("Перейти к оплате", "card", url=confirmation_url))
                builder.row(btn("Назад", "back", callback_data="open_premium"))
                
                await callback.message.edit_text(
                    f"{E.card} <b>Оплата через ЮKassa</b>\n\n"
                    f"План: <i>{plan_data['title']}</i>\n"
                    f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                    f"Нажмите кнопку ниже, чтобы перейти к оплате.\n"
                    f"После успешной оплаты подписка будет активирована автоматически.\n\n"
                    f"{E.clock} <i>Платеж не должен задерживаться больше 1 часа.</i>",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
                
                await callback.answer()
                
            except Exception as e:
                logger.error(f"Error creating YooKassa payment: {e}", exc_info=True)
                await callback.answer(f"{E.error} Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
                return
        
        elif method_id == "cryptopay":
            if not hasattr(config, 'cryptopay') or not config.cryptopay.enabled:
                await callback.answer(f"{E.error} Crypto Pay не настроен", show_alert=True)
                return
            
            amount_rub = price / 100.0
            api_url = "https://testnet-pay.crypt.bot/api/createInvoice" if config.cryptopay.testnet else "https://pay.crypt.bot/api/createInvoice"
            # Using simple string payload to avoid JSON stripping issues
            payload_str = f"{callback.from_user.id}:{plan_id}:cryptopay:miniapp"
            
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    headers = {"Crypto-Pay-API-Token": config.cryptopay.api_token}
                    data_pay = {
                        "currency_type": "fiat",
                        "fiat": "RUB",
                        "amount": f"{amount_rub:.2f}",
                        "description": f"VPN подписка - {plan_data['title']}",
                        "payload": payload_str
                    }
                    async with session.post(api_url, headers=headers, json=data_pay) as resp:
                        res = await resp.json()
                        if res.get("ok"):
                            invoice_url = res["result"].get("mini_app_invoice_url", res["result"]["bot_invoice_url"])
                            invoice_id = res["result"]["invoice_id"]
                            logger.info(f"CryptoPay payment created: {invoice_id}")
                            
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
                                    "subscription" if not is_renewal else "renewal",
                                    "pending",
                                    str(invoice_id)
                                )
                            
                            builder = InlineKeyboardBuilder()
                            builder.row(btn("Перейти к оплате", "plus", url=invoice_url))
                            builder.row(btn("Проверить оплату", "refresh", callback_data=f"check_crypto:{invoice_id}"))
                            builder.row(btn("Назад", "back", callback_data="open_premium"))
                            
                            await callback.message.edit_text(
                                f"{E.plus} <b>Оплата через Crypto Pay</b>\n\n"
                                f"План: <i>{plan_data['title']}</i>\n"
                                f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                                f"Нажмите кнопку ниже, чтобы перейти к оплате.\n"
                                f"После успешной оплаты подписка будет активирована автоматически.\n\n",
                                reply_markup=builder.as_markup(),
                                parse_mode="HTML"
                            )
                            await callback.answer()
                        else:
                            logger.error(f"Crypto Pay API Error: {res}")
                            await callback.answer(f"{E.error} Ошибка при создании платежа.", show_alert=True)
            except Exception as e:
                logger.error(f"Error creating Crypto Pay payment: {e}", exc_info=True)
                await callback.answer(f"{E.error} Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
        else:
            # Оплата через Telegram (Stars)
            # Проверяем минимальную сумму
            if price < 1:
                await callback.answer(f"{E.error} Ошибка: сумма слишком мала", show_alert=True)
                return
            
            # Создаем payload
            payload = f"{plan_id}|{method_id}"
            
            # Отправляем инвойс
            try:
                await bot.send_invoice(
                    chat_id=callback.message.chat.id,
                    title=f"VPN подписка - {plan_data['title']}",
                    description=f"VPN подписка. Нажимая кнопку «Заплатить» Вы соглашаетесь с правилами VPN бота (/help)",
                    provider_token=method_data.get('provider_token', ''),
                    currency=method_data['currency'],
                    prices=[LabeledPrice(label="VPN подписка", amount=price)],
                    payload=payload,
                    start_parameter='subscription'
                )
                await callback.answer()
            except Exception as e:
                logger.error(f"Error sending invoice: {e}", exc_info=True)
                await callback.answer(f"{E.error} Ошибка при создании инвойса. Попробуйте позже.", show_alert=True)
    
    @dp.callback_query(F.data.startswith("check_crypto:"))
    async def handle_check_crypto_payment(callback: CallbackQuery):
        """Ручная проверка оплаты Crypto Pay"""
        invoice_id = callback.data.split(":")[1]
        
        # Получаем данные планов и методов
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        
        api_url = "https://testnet-pay.crypt.bot/api/getInvoices" if config.cryptopay.testnet else "https://pay.crypt.bot/api/getInvoices"
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {"Crypto-Pay-API-Token": config.cryptopay.api_token}
                params = {"invoice_ids": invoice_id}
                async with session.get(api_url, headers=headers, params=params) as resp:
                    res = await resp.json()
                    if res.get("ok") and res.get("result", {}).get("items"):
                        invoice = res["result"]["items"][0]
                        status = invoice.get("status")
                        
                        if status == "paid":
                            # Оплачено! Процессим.
                            from ..payments import process_webhook_payment
                            
                            # Парсим метаданные из payload
                            meta_payload = invoice.get("payload", "")
                            metadata = {}
                            if meta_payload and ":" in meta_payload:
                                parts = meta_payload.split(":")
                                if len(parts) >= 4 and parts[1] == "gb_pack":
                                    metadata = {
                                        "user_id": int(parts[0]),
                                        "product_type": "gb_pack",
                                        "pack_id": int(parts[2]),
                                        "method_id": parts[3] if len(parts) > 3 else "cryptopay",
                                        "payment_source": "miniapp"
                                        if parts[-1] == "miniapp"
                                        else "bot",
                                    }
                                else:
                                    device_count = 1
                                    if (
                                        len(parts) >= 5
                                        and str(parts[3]).isdigit()
                                        and int(parts[3]) < 1_000_000_000
                                    ):
                                        device_count = int(parts[3])
                                    metadata = {
                                        "user_id": int(parts[0]),
                                        "plan_id": parts[1],
                                        "method_id": parts[2] if len(parts) > 2 else "cryptopay",
                                        "device_count": device_count,
                                        "payment_source": "miniapp"
                                        if parts[-1] == "miniapp"
                                        else "bot",
                                    }
                            
                            success = await process_webhook_payment(
                                payment_id=str(invoice_id),
                                payment_obj=invoice,
                                metadata=metadata,
                                bot=bot,
                                config=config,
                                subscription_plans=subscription_plans,
                                renewal_plans=renewal_plans,
                                payment_methods=PAYMENT_METHODS
                            )
                            
                            if success:
                                # Сообщение об успехе уже отправлено в process_webhook_payment
                                # Но мы можем убрать кнопки на текущем сообщении
                                await callback.message.edit_reply_markup(reply_markup=None)
                                await callback.answer(f"{E.success} Оплата подтверждена!", show_alert=True)
                            else:
                                await callback.answer(f"{E.success} Оплата уже была обработана.", show_alert=True)
                        else:
                            await callback.answer(f"{E.wait} Оплата еще не поступила. Попробуйте через минуту.", show_alert=True)
                    else:
                        logger.error(f"Crypto Pay API Error in check: {res}")
                        await callback.answer(f"{E.error} Ошибка при проверке статуса.", show_alert=True)
        except Exception as e:
            logger.error(f"Error checking Crypto Pay payment: {e}", exc_info=True)
            await callback.answer(f"{E.error} Ошибка при проверке. Попробуйте позже.", show_alert=True)

    @dp.callback_query(F.data == "open_traffic_packs")
    async def handle_open_traffic_packs(callback: CallbackQuery):
        await send_traffic_packs_menu(bot, callback, config, edit=True)
        await callback.answer()

    @dp.callback_query(F.data.startswith("traffic_pack_choose:"))
    async def handle_traffic_pack_choose(callback: CallbackQuery):
        user_id = callback.from_user.id
        try:
            pack_id = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer(f"{E.error} Неверный пакет", show_alert=True)
            return

        async with get_connection() as conn:
            ok_sub = await conn.fetchval(
                """
                SELECT CASE
                    WHEN pay_subscribed = TRUE AND subscription_end IS NOT NULL
                         AND DATE(subscription_end) >= CURRENT_DATE
                    THEN TRUE ELSE FALSE END
                FROM users WHERE user_id = $1
                """,
                user_id,
            )
            pack = await conn.fetchrow(
                """
                SELECT id, title, gb_amount, price_rub, price_stars
                FROM gb_pack_products
                WHERE id = $1 AND is_active = TRUE
                """,
                pack_id,
            )

        if not ok_sub:
            await callback.answer("Нужна активная подписка", show_alert=True)
            return
        if not pack:
            await callback.answer("Пакет недоступен", show_alert=True)
            return

        text = (
            f"{E.signal} <b>Оплата пакета</b>\n\n"
            f"{pack['title']} — <b>+{pack['gb_amount']} ГБ</b> к лимиту\n\n"
            "Выберите способ оплаты:"
        )
        b = InlineKeyboardBuilder()
        if int(pack["price_stars"] or 0) >= 1:
            b.row(
                btn("Telegram Stars ({format_price_stars(pack['price_stars'])})", "star",
                    callback_data=f"traffic_pack_pay:{pack_id}:stars",
                )
            )
        if config.yookassa.enabled and int(pack["price_rub"] or 0) >= 100:
            b.row(
                btn("Карта ({format_price_rub(pack['price_rub'])})", "card",
                    callback_data=f"traffic_pack_pay:{pack_id}:yookassa",
                )
            )
        b.row(btn("К списку пакетов", "back", callback_data="open_traffic_packs"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("traffic_pack_pay:"))
    async def handle_traffic_pack_pay(callback: CallbackQuery):
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer(f"{E.error} Ошибка данных", show_alert=True)
            return
        try:
            pack_id = int(parts[1])
        except ValueError:
            await callback.answer(f"{E.error} Ошибка данных", show_alert=True)
            return
        method_id = parts[2]
        user_id = callback.from_user.id

        async with get_connection() as conn:
            ok_sub = await conn.fetchval(
                """
                SELECT CASE
                    WHEN pay_subscribed = TRUE AND subscription_end IS NOT NULL
                         AND DATE(subscription_end) >= CURRENT_DATE
                    THEN TRUE ELSE FALSE END
                FROM users WHERE user_id = $1
                """,
                user_id,
            )
            pack = await conn.fetchrow(
                """
                SELECT id, title, gb_amount, price_rub, price_stars
                FROM gb_pack_products
                WHERE id = $1 AND is_active = TRUE
                """,
                pack_id,
            )

        if not ok_sub or not pack:
            await callback.answer("Пакет или подписка недоступны", show_alert=True)
            return

        if method_id not in PAYMENT_METHODS:
            await callback.answer(f"{E.error} Неизвестный способ оплаты", show_alert=True)
            return
        method_data = PAYMENT_METHODS[method_id]

        if method_id == "yookassa":
            if not config.yookassa.enabled:
                await callback.answer(f"{E.error} ЮKassa не настроена", show_alert=True)
                return
            price = int(pack["price_rub"])
            if price < 100:
                await callback.answer(f"{E.error} Минимальная сумма — 1 ₽", show_alert=True)
                return
            try:
                yk = YooKassaClient(config.yookassa)
                bot_info = await bot.get_me()
                bot_username = bot_info.username or "bot"
                amount_rub = price / 100.0
                payment_data = yk.create_payment(
                    amount=amount_rub,
                    description=f"Доп. трафик VPN — {pack['title']}",
                    return_url=f"https://t.me/{bot_username}?start=payment_success",
                    metadata={
                        "user_id": user_id,
                        "product_type": "gb_pack",
                        "pack_id": pack_id,
                        "payment_source": "bot",
                    },
                )
                payment_id = payment_data["id"]
                confirmation_url = payment_data["confirmation_url"]
                async with get_connection() as conn:
                    await conn.execute(
                        """
                        INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        user_id,
                        price,
                        "RUB",
                        f"gb_pack:{pack_id}",
                        "gb_pack",
                        "pending",
                        payment_id,
                    )
                pay_kb = InlineKeyboardBuilder()
                pay_kb.row(btn("Перейти к оплате", "card", url=confirmation_url))
                pay_kb.row(btn("Назад", "back", callback_data=f"traffic_pack_choose:{pack_id}"))
                await callback.message.edit_text(
                    f"{E.card} <b>Оплата пакета</b>\n\n"
                    f"{pack['title']} — +{pack['gb_amount']} ГБ\n"
                    f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                    "После оплаты ГБ начислятся автоматически.",
                    parse_mode="HTML",
                    reply_markup=pay_kb.as_markup(),
                )
                await callback.answer()
            except Exception as e:
                logger.error("traffic_pack yookassa: %s", e, exc_info=True)
                await callback.answer(f"{E.error} Ошибка создания платежа", show_alert=True)
            return

        if method_id == "stars":
            price = int(pack["price_stars"] or 0)
            if price < 1:
                await callback.answer(f"{E.error} Неверная цена", show_alert=True)
                return
            ts = int(time.time())
            payload = f"stars_gbpack_{user_id}_{pack_id}_{ts}"
            try:
                await bot.send_invoice(
                    chat_id=callback.message.chat.id,
                    title=f"Доп. трафик: {pack['title']}",
                    description=f"+{pack['gb_amount']} ГБ к месячному лимиту",
                    provider_token=method_data.get("provider_token", ""),
                    currency=method_data["currency"],
                    prices=[LabeledPrice(label=str(pack["title"])[:32], amount=price)],
                    payload=payload,
                    start_parameter=f"gbp{pack_id}",
                )
                await callback.answer()
            except Exception as e:
                logger.error("traffic_pack stars invoice: %s", e, exc_info=True)
                await callback.answer(f"{E.error} Не удалось выставить счёт", show_alert=True)
            return

        await callback.answer(f"{E.error} Способ оплаты не поддерживается", show_alert=True)

    @dp.callback_query(F.data == "my_devices")
    async def handle_my_devices(callback: CallbackQuery, state: FSMContext):
        """Показать активные устройства пользователя (по отпечатку клиента, не по IP)."""
        user_id = callback.from_user.id
        from .start import should_show_devices_menu

        if not await should_show_devices_menu(user_id):
            await callback.answer(
                "На тарифе Plus безлимит устройств — сброс не нужен.",
                show_alert=True,
            )
            return
        async with get_connection() as conn:
            text, builder = await _build_my_devices_view(conn, user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("rm_dev:"))
    async def handle_rm_one_device(callback: CallbackQuery, state: FSMContext):
        """Удалить одно устройство (все логи с этим отпечатком за 6 ч)."""
        user_id = callback.from_user.id
        fp = (callback.data or "").split(":", 1)[1] if ":" in (callback.data or "") else ""
        fp = fp.strip().lower()
        if len(fp) != 32 or not re.fullmatch(r"[0-9a-f]{32}", fp):
            await callback.answer("Некорректные данные", show_alert=True)
            return
        async with get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM subscription_usage_logs
                WHERE user_id = $1
                  AND device_fingerprint = $2
                  AND timestamp >= NOW() - INTERVAL '6 hours'
                """,
                user_id,
                fp,
            )
            text, builder = await _build_my_devices_view(conn, user_id)
        await callback.answer(f"{E.success} Устройство удалено из списка.", show_alert=True)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())

    @dp.callback_query(F.data == "reset_devices")
    async def handle_reset_devices(callback: CallbackQuery, state: FSMContext):
        """Сбросить все активные сессии (удалить логи за 6 часов)"""
        user_id = callback.from_user.id
        from .start import should_show_devices_menu

        if not await should_show_devices_menu(user_id):
            await callback.answer(
                "На тарифе Plus безлимит устройств — сброс не нужен.",
                show_alert=True,
            )
            return

        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM subscription_usage_logs WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '6 hours'",
                user_id,
            )

        # Track device reset for upsell notifications
        try:
            from ..engagement_notifications import check_device_reset_upsell
            await check_device_reset_upsell(bot, user_id)
        except Exception as e:
            logger.debug("device_reset_upsell error: %s", e)

        await callback.answer(f"{E.success} Сессии сброшены! Подключите нужные устройства заново.", show_alert=True)

        builder = InlineKeyboardBuilder()
        builder.row(btn("Устройства", "devices", callback_data="my_devices"))
        builder.row(btn("Назад", "back", callback_data="go_back_subscription"))
        await callback.message.edit_text(
            f"{E.success} <b>Все сессии сброшены</b>\n\n"
            "Теперь подключите только нужные устройства — они будут учтены заново.",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )

    @dp.callback_query(F.data == "go_back_subscription")
    async def handle_go_back_subscription(callback: CallbackQuery, state: FSMContext):
        """Возврат на главное меню"""
        user_id = callback.from_user.id
        first_name = callback.from_user.first_name or "Пользователь"
        from ..subscriptions import get_subscription_status_display
        from ..handlers.start import get_main_text, get_main_keyboard
        
        subscription_status = await get_subscription_status_display(user_id)
        
        try:
            await callback.message.edit_text(
                text=await get_main_text(first_name, subscription_status, user_id),
                parse_mode='HTML',
                reply_markup=await get_main_keyboard(user_id, config),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            # Игнорируем ошибку, если сообщение не изменилось
            if "message is not modified" in str(e):
                logger.debug(f"Message not modified for user {user_id}, ignoring")
            else:
                raise
        await callback.answer()

    @dp.callback_query(F.data == "activate_trial")
    async def handle_activate_trial(callback: CallbackQuery, state: FSMContext):
        """Пробный период: Plus за 1₽ с привязкой карты для автосписания."""
        user_id = callback.from_user.id

        async with get_connection() as conn:
            from ..trial_usage import (
                get_trial_days,
                has_completed_trial_payment,
                user_has_referral_trial_source,
                user_show_referral_trial_offer,
                referral_trial_offer_text,
            )

            if not await user_has_referral_trial_source(conn, user_id):
                await callback.answer(
                    "Пробный Plus за 1₽ доступен по реферальной или партнёрской ссылке.",
                    show_alert=True,
                )
                from .tiers import build_tiers_message
                text, markup = await build_tiers_message(user_id, view="plus_plans")
                await callback.message.edit_text(
                    text, parse_mode="HTML", reply_markup=markup
                )
                return

            if not await user_show_referral_trial_offer(conn, user_id):
                if await has_completed_trial_payment(conn, user_id):
                    await callback.answer(
                        f"{E.error} Вы уже использовали пробный период!",
                        show_alert=True,
                    )
                else:
                    await callback.answer(
                        f"{E.error} Пробный период недоступен (активная Plus или отключён в настройках).",
                        show_alert=True,
                    )
                return

            trial_days = await get_trial_days(conn)
            if trial_days <= 0:
                await callback.answer(f"{E.error} Пробный период сейчас недоступен.", show_alert=True)
                return

        if not config.yookassa.enabled:
            await callback.answer(f"{E.error} Оплата недоступна", show_alert=True)
            return

        try:
            from ..trial_usage import referral_trial_offer_text
            from ..yookassa_client import YooKassaClient
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            payment_data = yk.create_payment(
                amount=1.00,
                description=f"VPN Plus — пробный период ({trial_days} дн.)",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": "plus_1m",
                    "method_id": "yookassa",
                    "product_type": "tier",
                    "is_trial": "true",
                    "trial_days": str(trial_days),
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, 100, "RUB", "plus_1m", "tier", "pending",
                    payment_data["id"],
                )

            from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB
            b = _IKB()
            b.row(btn("Перейти к оплате (1₽)", "card", url=payment_data["confirmation_url"]))
            b.row(btn("Назад", "back", callback_data="go_back"))
            await callback.message.edit_text(
                referral_trial_offer_text(trial_days),
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            ) 
            await callback.answer()
        except Exception as e:
            logger.error("activate_trial error: %s", e, exc_info=True)
            await callback.answer(f"{E.error} Ошибка создания платежа", show_alert=True)
