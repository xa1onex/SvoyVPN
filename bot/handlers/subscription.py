"""
Обработчики подписки и получения VPN ссылки
"""
import html
import logging
import time
from datetime import datetime
from aiogram import Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from ..database import get_connection, ensure_subscription_token
from ..subscriptions import get_subscription_status, get_user_subscription_url, format_subscription_status_label
from ..plans import get_subscription_plans, get_renewal_plans, format_price_rub, format_price_stars, format_price_both, PAYMENT_METHODS
from ..config import AppConfig
from ..yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


ONBOARDING_APPS = {
    "apple": [
        {"id": "happ", "name": "Happ", "url": "https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215"},
        {"id": "hiddify", "name": "Hiddify", "url": "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532"},
        {"id": "v2raytun", "name": "V2RayTun", "url": "https://apps.apple.com/app/v2raytun/id6476628951"}
    ],
    "android": [
        {"id": "happ", "name": "Happ", "url": "https://play.google.com/store/apps/details?id=com.happproxy"},
        {"id": "hiddify", "name": "Hiddify", "url": "https://play.google.com/store/apps/details?id=app.hiddify.com"},
        {"id": "v2raytun", "name": "V2RayTun", "url": "https://play.google.com/store/apps/details?id=com.v2raytun.android"}
    ],
    "windows": [
        {"id": "happ", "name": "Happ", "url": "https://github.com/Happ-proxy/happ-desktop/releases/download/2.4.0/setup-Happ.x64.exe"},
        {"id": "hiddify", "name": "Hiddify", "url": "https://github.com/hiddify/hiddify-app/releases"},
        {"id": "v2rayn", "name": "V2RayN", "url": "https://github.com/2dust/v2rayN/releases"}
    ],
    "mac": [
        {"id": "happ", "name": "Happ", "url": "https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215"},
        {"id": "hiddify", "name": "Hiddify", "url": "https://github.com/hiddify/hiddify-app/releases"},
        {"id": "v2raytun", "name": "V2RayTun", "url": "https://apps.apple.com/app/v2raytun/id6476628951"}
    ]
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
            "📶 <b>Дополнительный трафик</b>\n\n"
            "Доступно только при <b>активной подписке</b>.\n"
            "Сначала оформи или продли VPN в разделе «Подписка»."
        )
    elif not packs:
        text = (
            "📶 <b>Дополнительный трафик</b>\n\n"
            "Нехватило трафика? Купи дополнительный объём пакетов гб\n"
            "Пакеты сейчас недоступны. Загляни позже или напиши в поддержку."
        )
    else:
        parts = [
            "📶 <b>Увеличить лимит трафика</b>\n",
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
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
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
    
    @dp.callback_query(F.data == "get_vpn_link")
    async def handle_get_vpn_link(callback: CallbackQuery):
        """Отправляет пользователю выбор устройства"""
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📱 iPhone", callback_data="ob_dev_apple"))
        builder.row(InlineKeyboardButton(text="🤖 Android", callback_data="ob_dev_android"))
        builder.row(InlineKeyboardButton(text="💻 Windows", callback_data="ob_dev_windows"))
        builder.row(InlineKeyboardButton(text="🖥 macOS", callback_data="ob_dev_mac"))
        builder.row(InlineKeyboardButton(text="⚙️ Настроить вручную", callback_data="ob_dev_manual"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back_subscription"))
        
        text = (
            "💻 <b>Выберите ваше устройство</b>\n\n"
            "На чем вы будете использовать VPN? Мы подготовили пошаговую инструкцию для каждой платформы."
        )
        
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
        except TelegramBadRequest:
            await callback.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
        await callback.answer()

    @dp.callback_query(F.data.startswith("ob_dev_"))
    async def handle_ob_device(callback: CallbackQuery):
        """Выбор приложения для конкретного устройства или ручная настройка"""
        device = callback.data.replace("ob_dev_", "")
        
        if device == "manual":
            user_id = callback.from_user.id
            link = await get_user_subscription_url(user_id, config)
            
            builder = InlineKeyboardBuilder()
            # Нативная кнопка copy_text (Telegram Bot API, aiogram 2.x)
            builder.row(InlineKeyboardButton(
                text="📋 Скопировать конфигурацию",
                copy_text={"text": link}
            ))
            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="get_vpn_link"))
            
            await callback.message.edit_text(
                "⚙️ <b>Ручная настройка</b>\n\n"
                "Ваша универсальная ссылка на подписку:\n"
                f"<code>{link}</code>\n\n"
                "Она подходит для любого приложения, работающего через протоколы VLESS/V2Ray (например, v2rayNG, V2RayN, Hiddify, sing-box и т.д.).",
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
            await callback.answer()
            return

        apps = ONBOARDING_APPS.get(device, [])
        
        builder = InlineKeyboardBuilder()
        for app in apps:
            builder.row(InlineKeyboardButton(text=f"🚀 {app['name']}", callback_data=f"ob_app_{device}_{app['id']}"))
            
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="get_vpn_link"))
        
        device_names = {"apple": "iPhone", "android": "Android", "windows": "Windows", "mac": "macOS"}
        dev_name = device_names.get(device, device)
        
        await callback.message.edit_text(
            f"📱 <b>{dev_name} — Выберите приложение</b>\n\n"
            "Выберите приложение, которое хотите использовать. Мы рекомендуем <b>Happ</b> для максимальной скорости.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("ob_app_"))
    async def handle_ob_app(callback: CallbackQuery):
        """Инструкция по установке и кнопка подключения"""
        # data format: ob_app_{device}_{app_id}
        parts = callback.data.split("_")
        device = parts[2]
        app_id = parts[3]
        
        apps = ONBOARDING_APPS.get(device, [])
        app = next((a for a in apps if a["id"] == app_id), None)
        if not app:
            await callback.answer("❌ Ошибка: приложение не сканируется")
            return
            
        user_id = callback.from_user.id
        token = await ensure_subscription_token(user_id)
        
        # Deep link logic (Mac uses apple path)
        device_path = "apple" if device == "mac" else device
        connect_url = f"https://xdoublegroup.online/{device_path}/{app_id}/{token}"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📥 1. Скачать приложение", url=app['url']))
        builder.row(InlineKeyboardButton(text="⚡️ 2. ПОДКЛЮЧИТЬ VPN", url=connect_url))
        
        # Кнопка для фото-инструкции, если она есть
        from ..database import get_device_instruction_photos
        photos = await get_device_instruction_photos(device)
        if photos:
            builder.row(InlineKeyboardButton(text="📸 Посмотреть инструкцию", callback_data=f"ob_photos_{device}"))
            
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"ob_dev_{device}"))
        
        await callback.message.edit_text(
            f"🚀 <b>Настройка {app['name']}</b>\n\n"
            f"1️⃣ <b>Скачайте</b> приложение (если еще не установлено).\n\n"
            f"2️⃣ <b>Нажмите</b> кнопку «ПОДКЛЮЧИТЬ VPN» — приложение откроется само и добавит все нужные сервера.\n\n"
            f"3️⃣ <b>Запустите</b> VPN в приложении!\n\n"
            f"<i>* Если ссылка не открывается, убедитесь, что приложение установлено.</i>",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("ob_photos_"))
    async def handle_ob_photos(callback: CallbackQuery):
        """Показ фото-инструкций для устройства"""
        device = callback.data.replace("ob_photos_", "")
        from ..database import get_device_instruction_photos
        photos = await get_device_instruction_photos(device)
        
        if not photos:
            await callback.answer("Инструкции пока нет")
            return
            
        await callback.answer()
        for photo_id in photos:
            try:
                await callback.message.answer_photo(photo_id)
            except Exception:
                continue
        
        await callback.message.answer(
            "Выше приведены фото-инструкции для вашего устройства. Если остались вопросы, напишите в поддержку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ob_dev_{device}")]
            ])
        )
    
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
    """Формирует сообщение и клавиатуру для подписки/продления"""
    user_id = info['user_id']
    from bot.plans import get_user_tariffs, get_subscription_plans, get_renewal_plans, format_price_rub, format_price_stars, format_price_both
    
    current_tariffs, is_renew, show_discount = await get_user_tariffs(user_id)
    regular_plans = await get_subscription_plans()
    renewal_plans = await get_renewal_plans()
    
    builder = InlineKeyboardBuilder()
    
    if is_renew:
        text = "💳 <b>Управление подпиской:</b>\n\n"
        end_date_obj = info.get('end_date_obj')
        if end_date_obj:
            days_remaining = info.get('days_remaining')
            if days_remaining == 0:
                text += f"✅ Ваш VPN: <b>активен СЕГОДНЯ</b> ({info.get('end_date_str')})!\n"
            elif days_remaining == 1:
                text += f"✅ Ваш VPN: <b>активен ЗАВТРА</b> ({info.get('end_date_str')})!\n"
            else:
                text += f"✅ Ваш VPN <b>{format_subscription_status_label(end_date_obj)}</b>!\n"
        else:
            text += "✅ Ваш VPN <b>активен</b>!\n"
        text += "Вы можете пользоваться приложением.\n\n"
        try:
            from ..traffic import user_traffic_snapshot

            async with get_connection() as conn:
                snap = await user_traffic_snapshot(conn, user_id, sync_from_panels=False)
            if snap.get("trafficEnforced"):
                u_g = float(snap.get("usedGb") or 0)
                l_g = float(snap.get("limitGb") or 0)
                b_g = int(snap.get("bonusGb") or 0)
                b_rem = float(snap.get("bonusRemainingGb") or 0)
                text += f"📊 <b>Трафик</b>: {u_g:.1f} / {l_g:.0f} ГБ"
                if b_g > 0:
                    text += f" (пакет: осталось {b_rem:.1f} из {b_g} ГБ)"
                text += "\n\n"
        except Exception as e:
            logger.warning("build_subscription_message traffic line: %s", e)

        if show_discount:
            text += "🎁 <b>Специальное предложение!</b>\n\n"
            text += "🔥 Успей продлить <b>VPN</b> по специальной цене:\n\n"
            
            for plan_id, plan_data in current_tariffs.items():
                base_id = plan_id.replace('_renew', '')
                base_plan = regular_plans.get(base_id, {})
                old_price = format_price_rub(base_plan.get('price_rub', plan_data.get('price_rub', 0)))
                new_price = format_price_rub(plan_data['price_rub'])
                text += f"{plan_data['title'].replace(' 🔥', '')} <s>{old_price}</s> - {new_price}\n"
            text += "\n"
        else:
            text += "💡 Вы можете продлить подписку в любое время:\n\n"

        builder.row(InlineKeyboardButton(text="🚀 Новые тарифы (Lite/Standard/Pro)", callback_data="open_tiers"))
        builder.row(InlineKeyboardButton(text="📶 Увеличить лимит трафика", callback_data="open_traffic_packs"))

        # Кнопки для продления (с использованием уже подготовленных current_tariffs)
        for plan_id, plan_data in current_tariffs.items():
            builder.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )
            
    else:
        text = "💳 <b>Информация о вашем VPN:</b>\n\n"
        text += (
            "❌ Ваш VPN <b>неактивен</b>!\n\n"
            "Что ты получишь с <b>VPN</b>?\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость подключения\n\n"
        )
        
        if show_discount:
            text += "🎁 <b>Специальное предложение!</b>\n\n"
            text += "🔥 Получи <b>VPN</b> по специальной цене:\n\n"
            
            for plan_id, plan_data in current_tariffs.items():
                # Т.к. get_user_tariffs уже подставил скидочные цены в current_tariffs для неактивного юзера если show_discount=True
                # Но мы хотим показать зачеркнутую старую цену
                base_id = plan_id.replace('_renew', '')
                base_plan = regular_plans.get(base_id, {})
                old_price = format_price_rub(base_plan.get('price_rub', plan_data.get('price_rub', 0)))
                new_price = format_price_rub(plan_data['price_rub'])
                text += f"{plan_data['title'].replace(' 🔥', '')} <s>{old_price}</s> - {new_price}\n"
            text += "\n"
        else:
            text += "Выберите план подписки:\n"
            
        # Кнопки со всеми доступными тарифами
        for plan_id, plan_data in current_tariffs.items():
            builder.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )

    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back_subscription"))
    
    return text, builder


async def setup_subscription_plan_handlers(dp, bot: Bot, config: AppConfig):
    """Настраивает обработчики для показа планов подписки"""
    
    @dp.callback_query(F.data == "show_subscription_plans")
    async def handle_show_subscription_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы подписки"""
        user_id = callback.from_user.id
        from bot.plans import get_user_tariffs
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        text = "💳 <b>Выберите план подписки:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in current_tariffs.items():
            price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
            text += f"• <b>{plan_data['title']}</b> - {price_text}\n"
            text += f"  Срок: {plan_data['duration']} месяцев\n"
            text += f"  Трафик: {plan_data.get('traffic_gb', 'Безлимитный')} ГБ\n\n"
        
        action = "buy_renewal" if is_renew else "buy_subscription"
        
        # Кнопки для оплаты (Stars и YooKassa)
        for plan_id, plan_data in list(current_tariffs.items())[:2]:  # Показываем первые 2 плана
            builder.row(
                InlineKeyboardButton(
                    text=f"⭐ {plan_data['title']} ({format_price_stars(plan_data['price_stars'])})",
                    callback_data=f"{action}:{plan_id}:stars"
                )
            )
            if config.yookassa.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💳 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"{action}:{plan_id}:yookassa"
                    )
                )
            if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💎 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"{action}:{plan_id}:cryptopay"
                    )
                )
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    
    @dp.callback_query(F.data == "show_renewal_plans")
    async def handle_show_renewal_plans(callback: CallbackQuery, state: FSMContext):
        """Показывает планы продления"""
        user_id = callback.from_user.id
        from bot.plans import get_user_tariffs
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        text = "💳 <b>Продлить подписку:</b>\n\n"
        builder = InlineKeyboardBuilder()
        
        for plan_id, plan_data in current_tariffs.items():
            price_text = format_price_both(plan_data['price_rub'], plan_data['price_stars'])
            text += f"• <b>{plan_data['title']}</b> - {price_text}\n"
        
        # Кнопки для оплаты
        action = "buy_renewal" if is_renew else "buy_subscription"
        for plan_id, plan_data in list(current_tariffs.items())[:2]:
            builder.row(
                InlineKeyboardButton(
                    text=f"⭐ {plan_data['title']} ({format_price_stars(plan_data['price_stars'])})",
                    callback_data=f"{action}:{plan_id}:stars"
                )
            )
            if config.yookassa.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💳 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"{action}:{plan_id}:yookassa"
                    )
                )
            if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=f"💎 {plan_data['title']} ({format_price_rub(plan_data['price_rub'])})",
                        callback_data=f"{action}:{plan_id}:cryptopay"
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
        
        from bot.plans import get_user_tariffs
        current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
        
        if plan_id not in current_tariffs:
            await callback.answer("❌ План недоступен или не найден", show_alert=True)
            return
            
        plan_data = current_tariffs[plan_id]
        
        # Определяем действие
        action = "buy_renewal" if is_renew else "buy_subscription"
        
        # Показываем методы оплаты
        text = f"💳 <b>{plan_data['title']}</b>\n\n"
        text += f"Срок: {plan_data['duration']} месяцев\n"
        text += "Лимит трафика — месячный, по дню покупки подписки (см. приложение).\n\n"
        text += "Выберите способ оплаты:"
        
        builder = InlineKeyboardBuilder()
        
        # Кнопка для оплаты Stars
        builder.row(
            InlineKeyboardButton(
                text=f"⭐ Telegram Stars ({format_price_stars(plan_data['price_stars'])})",
                callback_data=f"{action}:{plan_id}:stars"
            )
        )
        
        # Кнопка для оплаты YooKassa (если включена)
        if config.yookassa.enabled:
            builder.row(
                InlineKeyboardButton(
                    text=f"💳 Банковская карта ({format_price_rub(plan_data['price_rub'])})",
                    callback_data=f"{action}:{plan_id}:yookassa"
                )
            )
            
        # Кнопка для оплаты Crypto Pay (если включена)
        if hasattr(config, 'cryptopay') and config.cryptopay.enabled:
            builder.row(
                InlineKeyboardButton(
                    text=f"💎 Crypto Pay ({format_price_rub(plan_data['price_rub'])})",
                    callback_data=f"{action}:{plan_id}:cryptopay"
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
        # Парсим callback_data: buy_subscription:plan_id:method_id
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Неверный формат данных", show_alert=True)
            return
        
        plan_id = parts[1]
        method_id = parts[2]
        user_id = callback.from_user.id
        
        # Получаем актуальные планы
        from bot.plans import get_user_tariffs
        current_tariffs, user_is_renew, _ = await get_user_tariffs(user_id)
        
        # Убедимся, что тариф доступен
        if plan_id not in current_tariffs:
            await callback.answer("❌ План недоступен или устарел", show_alert=True)
            return
            
        plan_data = current_tariffs[plan_id]
        
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
        
        elif method_id == "cryptopay":
            if not hasattr(config, 'cryptopay') or not config.cryptopay.enabled:
                await callback.answer("❌ Crypto Pay не настроен", show_alert=True)
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
                            builder.row(InlineKeyboardButton(text="💎 Перейти к оплате", url=invoice_url))
                            builder.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto:{invoice_id}"))
                            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_premium"))
                            
                            await callback.message.edit_text(
                                f"💎 <b>Оплата через Crypto Pay</b>\n\n"
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
                            await callback.answer("❌ Ошибка при создании платежа.", show_alert=True)
            except Exception as e:
                logger.error(f"Error creating Crypto Pay payment: {e}", exc_info=True)
                await callback.answer("❌ Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
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
                await callback.answer("❌ Ошибка при создании инвойса. Попробуйте позже.", show_alert=True)
    
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
                                await callback.answer("✅ Оплата подтверждена!", show_alert=True)
                            else:
                                await callback.answer("✅ Оплата уже была обработана.", show_alert=True)
                        else:
                            await callback.answer("⏳ Оплата еще не поступила. Попробуйте через минуту.", show_alert=True)
                    else:
                        logger.error(f"Crypto Pay API Error in check: {res}")
                        await callback.answer("❌ Ошибка при проверке статуса.", show_alert=True)
        except Exception as e:
            logger.error(f"Error checking Crypto Pay payment: {e}", exc_info=True)
            await callback.answer("❌ Ошибка при проверке. Попробуйте позже.", show_alert=True)

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
            await callback.answer("❌ Неверный пакет", show_alert=True)
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
            f"📶 <b>Оплата пакета</b>\n\n"
            f"{pack['title']} — <b>+{pack['gb_amount']} ГБ</b> к лимиту\n\n"
            "Выберите способ оплаты:"
        )
        b = InlineKeyboardBuilder()
        if int(pack["price_stars"] or 0) >= 1:
            b.row(
                InlineKeyboardButton(
                    text=f"⭐ Telegram Stars ({format_price_stars(pack['price_stars'])})",
                    callback_data=f"traffic_pack_pay:{pack_id}:stars",
                )
            )
        if config.yookassa.enabled and int(pack["price_rub"] or 0) >= 100:
            b.row(
                InlineKeyboardButton(
                    text=f"💳 Карта ({format_price_rub(pack['price_rub'])})",
                    callback_data=f"traffic_pack_pay:{pack_id}:yookassa",
                )
            )
        b.row(InlineKeyboardButton(text="◀️ К списку пакетов", callback_data="open_traffic_packs"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("traffic_pack_pay:"))
    async def handle_traffic_pack_pay(callback: CallbackQuery):
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Ошибка данных", show_alert=True)
            return
        try:
            pack_id = int(parts[1])
        except ValueError:
            await callback.answer("❌ Ошибка данных", show_alert=True)
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
            await callback.answer("❌ Неизвестный способ оплаты", show_alert=True)
            return
        method_data = PAYMENT_METHODS[method_id]

        if method_id == "yookassa":
            if not config.yookassa.enabled:
                await callback.answer("❌ ЮKassa не настроена", show_alert=True)
                return
            price = int(pack["price_rub"])
            if price < 100:
                await callback.answer("❌ Минимальная сумма — 1 ₽", show_alert=True)
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
                pay_kb.row(InlineKeyboardButton(text="💳 Перейти к оплате", url=confirmation_url))
                pay_kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"traffic_pack_choose:{pack_id}"))
                await callback.message.edit_text(
                    f"💳 <b>Оплата пакета</b>\n\n"
                    f"{pack['title']} — +{pack['gb_amount']} ГБ\n"
                    f"Сумма: <i>{format_price_rub(price)}</i>\n\n"
                    "После оплаты ГБ начислятся автоматически.",
                    parse_mode="HTML",
                    reply_markup=pay_kb.as_markup(),
                )
                await callback.answer()
            except Exception as e:
                logger.error("traffic_pack yookassa: %s", e, exc_info=True)
                await callback.answer("❌ Ошибка создания платежа", show_alert=True)
            return

        if method_id == "stars":
            price = int(pack["price_stars"] or 0)
            if price < 1:
                await callback.answer("❌ Неверная цена", show_alert=True)
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
                await callback.answer("❌ Не удалось выставить счёт", show_alert=True)
            return

        await callback.answer("❌ Способ оплаты не поддерживается", show_alert=True)

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
                reply_markup=await get_main_keyboard(user_id, config)
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
        """Активация пробного периода пользователем"""
        user_id = callback.from_user.id
        
        async with get_connection() as conn:
            user_trial_used = await conn.fetchval("SELECT trial_used FROM users WHERE user_id = $1", user_id)
            if user_trial_used:
                await callback.answer("❌ Вы уже использовали пробный период!", show_alert=True)
                return
            
            trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
            trial_days = trial_settings['days'] if trial_settings else 0
            
            if trial_days <= 0:
                await callback.answer("❌ Пробный период сейчас недоступен.", show_alert=True)
                return
            
            # Обновляем пользователя
            await conn.execute('''
                UPDATE users SET 
                    trial_used = TRUE,
                    pay_subscribed = TRUE,
                    subscription_end = CASE 
                        WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                        THEN CURRENT_DATE + ($1 || ' days')::INTERVAL
                        ELSE subscription_end + ($1 || ' days')::INTERVAL
                    END
                WHERE user_id = $2
            ''', str(trial_days), user_id)
            from ..traffic import apply_subscription_anchor_on_payment
            await apply_subscription_anchor_on_payment(conn, user_id)
            
        await callback.answer(f"✅ Пробный период на {trial_days} дней успешно активирован!", show_alert=True)
        # Перерисовываем главное меню
        await handle_go_back_subscription(callback, state)
