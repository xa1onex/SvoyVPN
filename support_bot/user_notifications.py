"""Отправка уведомлений пользователям через основной VPN-бот (кнопки callback работают)."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)

# Кнопки главного меню (как в admin broadcast) — текст можно переименовать
MENU_BUTTON_MAP = {
    "get_vpn": ("get_vpn_link", "🔗 Получить VPN"),
    "referral": ("open_balance", "🎁 Подарок"),
    "premium": ("open_premium", "💎 Подписка"),
    "help": ("open_help", "🆘 Помощь"),
    "trial": ("activate_trial", "🎁 Standard за 1₽ — попробовать"),
    "tiers": ("open_tiers", "🚀 Тарифы Lite/Standard/Pro"),
    "traffic": ("open_traffic_packs", "📶 Увеличить лимит трафика"),
    "balance": ("open_balance", "🎁 Подарок"),
    "devices": ("my_devices", "📱 Мои устройства"),
}

# Готовые промо из engagement (фиксированные callback в основном боте)
BUILTIN_PROMO_CALLBACKS = {
    "promo_lite_30": "promo_lite_30",
    "promo_referral_10_lite": "promo_referral_10:lite",
    "promo_referral_10_standard": "promo_referral_10:standard",
    "promo_referral_10_pro": "promo_referral_10:pro",
}


def list_button_catalog() -> dict[str, Any]:
    return {
        "menu_buttons": [
            {
                "type": "menu",
                "menu_key": k,
                "default_text": v[1],
                "maps_to_callback": v[0],
                "custom_text": "любой текст на кнопке",
            }
            for k, v in MENU_BUTTON_MAP.items()
        ],
        "builtin_callbacks": [
            {"type": "callback", "callback_data": cd, "description": key}
            for key, cd in BUILTIN_PROMO_CALLBACKS.items()
        ],
        "special_types": [
            {
                "type": "tier_pay",
                "plan_id": "standard_1m",
                "text": "Подпись",
                "note": "Обычная цена тарифа",
            },
            {
                "type": "personal_promo",
                "offer_id": 123,
                "text": "из create_personal_discount_offer",
                "note": "Скидка N% с учётом рекуррента — одноразовая оплата",
            },
            {"type": "url", "url": "https://...", "text": "Ссылка"},
            {
                "type": "callback",
                "callback_data": "open_tiers",
                "text": "любой callback основного бота",
            },
        ],
        "important": (
            "Уведомления отправляются через ОСНОВНОЙ бот SvoyVPN, иначе callback-кнопки не сработают. "
            "При has_recurring_card предупреди: автосписание по полной цене; скидка только по кнопке оффера."
        ),
    }


def build_inline_keyboard(buttons: list[dict[str, Any]], *, user_trial_used: bool = False) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    for btn in buttons:
        btype = (btn.get("type") or "menu").lower()
        text = (btn.get("text") or "").strip()

        if btype == "menu":
            key = (btn.get("menu_key") or btn.get("key") or "").strip()
            if key not in MENU_BUTTON_MAP:
                continue
            cb, default_text = MENU_BUTTON_MAP[key]
            if key == "trial" and user_trial_used:
                continue
            builder.row(
                InlineKeyboardButton(text=text or default_text, callback_data=cb)
            )
        elif btype == "url":
            url = (btn.get("url") or "").strip()
            if text and url.startswith("http"):
                builder.row(InlineKeyboardButton(text=text, url=url))
        elif btype == "tier_pay":
            plan_id = btn.get("plan_id") or "standard_1m"
            builder.row(
                InlineKeyboardButton(
                    text=text or f"💳 Оплатить {plan_id}",
                    callback_data=f"tier_pay:{plan_id}",
                )
            )
        elif btype == "personal_promo":
            oid = btn.get("offer_id")
            if oid is not None:
                builder.row(
                    InlineKeyboardButton(
                        text=text or "🔥 Спецпредложение",
                        callback_data=f"personal_promo:{int(oid)}",
                    )
                )
        elif btype == "builtin_promo":
            promo_key = btn.get("promo_key") or ""
            cd = BUILTIN_PROMO_CALLBACKS.get(promo_key)
            if cd and text:
                builder.row(InlineKeyboardButton(text=text, callback_data=cd))
        elif btype == "callback":
            cd = (btn.get("callback_data") or "").strip()
            if text and cd:
                builder.row(InlineKeyboardButton(text=text, callback_data=cd))

    if not builder.buttons:
        return None
    return builder.as_markup()


async def send_user_notification(
    main_bot: Bot,
    user_id: int,
    text: str,
    *,
    buttons: list[dict[str, Any]] | None = None,
    parse_mode: str = "HTML",
) -> dict[str, Any]:
    from bot.database import get_connection

    async with get_connection() as conn:
        trial_row = await conn.fetchrow(
            "SELECT trial_used, blacklisted FROM users WHERE user_id=$1", user_id
        )
    if not trial_row:
        return {"error": "Пользователь не найден"}
    if trial_row["blacklisted"]:
        return {"error": "Пользователь в чёрном списке"}

    markup = build_inline_keyboard(
        buttons or [],
        user_trial_used=bool(trial_row["trial_used"]),
    )
    try:
        msg = await main_bot.send_message(
            user_id,
            text,
            reply_markup=markup,
            parse_mode=parse_mode,
        )
        return {
            "ok": True,
            "message_id": msg.message_id,
            "chat_id": user_id,
            "buttons_count": len(buttons or []),
        }
    except Exception as e:
        logger.exception("send_user_notification %s", user_id)
        return {"error": str(e)}
