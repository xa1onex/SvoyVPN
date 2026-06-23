"""
Логирование действий пользователей в боте (кнопки, команды, сообщения).
"""
from __future__ import annotations

import asyncio
import logging

from .database import get_connection

logger = logging.getLogger(__name__)

_MAX_ACTION = 256
_MAX_DETAIL = 500

_SKIP_ACTIONS = frozenset({"flyer_recheck"})

ACTION_LABELS: dict[str, str] = {
    "/start": "▶️ Старт",
    "get_vpn_link": "🔗 Подключить VPN",
    "open_invite": "🎁 Подарок",
    "open_help": "🆘 Помощь",
    "open_balance": "💰 Баланс",
    "open_subscription": "💎 Подписка",
    "open_tiers": "💎 Тарифы",
    "activate_trial": "🆓 Пробный Plus",
    "successful_payment": "💳 Оплата",
    "text": "💬 Текст",
    "photo": "🖼 Фото",
    "document": "📎 Файл",
    "contact": "📇 Контакт",
    "sticker": "🙂 Стикер",
    "sent:connect_nudge_1h": "📨 Nudge 1ч",
    "sent:connect_nudge_1d": "📨 Nudge 1д",
    "sent:connect_nudge_4d_before_end": "📨 Nudge −4д",
}


def _truncate(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def normalize_action(action: str) -> str:
    """Группировка для аналитики: tier_pay:plus_1m → tier_pay."""
    if not action:
        return "unknown"
    base = action.split(":", 1)[0].split("|", 1)[0]
    return base[:64] or "unknown"


def format_activity_label(event_kind: str, action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.startswith("sent:"):
        return f"📨 {action[5:]}"
    base = normalize_action(action)
    if base in ACTION_LABELS:
        suffix = action[len(base) :]
        return ACTION_LABELS[base] + (suffix if suffix else "")
    icons = {
        "callback": "🔘",
        "command": "⌨️",
        "message": "💬",
        "payment": "💳",
        "notification": "📨",
    }
    icon = icons.get(event_kind, "•")
    return f"{icon} <code>{action[:48]}</code>"


def schedule_bot_activity(
    user_id: int,
    event_kind: str,
    action: str,
    *,
    detail: str | None = None,
) -> None:
    """Неблокирующая запись события."""
    if not user_id or user_id < 0:
        return
    action = _truncate(action, _MAX_ACTION) or "unknown"
    if action in _SKIP_ACTIONS:
        return
    detail = _truncate(detail, _MAX_DETAIL)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write_activity(user_id, event_kind, action, detail))
    except RuntimeError:
        pass


async def record_bot_activity(
    user_id: int,
    event_kind: str,
    action: str,
    *,
    detail: str | None = None,
) -> None:
    await _write_activity(
        user_id,
        event_kind,
        _truncate(action, _MAX_ACTION) or "unknown",
        _truncate(detail, _MAX_DETAIL),
    )


async def _write_activity(
    user_id: int,
    event_kind: str,
    action: str,
    detail: str | None,
) -> None:
    if action in _SKIP_ACTIONS:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO bot_activity_logs (user_id, event_kind, action, detail)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                event_kind[:32],
                action,
                detail,
            )
            await conn.execute(
                "UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = $1",
                user_id,
            )
    except Exception as e:
        logger.debug("bot_activity log failed user=%s action=%s: %s", user_id, action, e)


def describe_message_event(message) -> tuple[str, str, str | None]:
    """event_kind, action, detail из Message."""
    from aiogram.types import Message

    if not isinstance(message, Message):
        return "message", "unknown", None

    if message.successful_payment:
        sp = message.successful_payment
        payload = _truncate(sp.invoice_payload or "", 120)
        amount = f"{sp.total_amount / 100:.2f} {sp.currency}"
        return "payment", "successful_payment", f"{amount}; {payload}"

    if message.text and message.text.startswith("/"):
        parts = message.text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower()
        args = _truncate(parts[1] if len(parts) > 1 else None, 120)
        return "command", cmd, args

    content = message.content_type or "message"
    detail = None
    if message.text:
        detail = _truncate(message.text, 120)
    elif message.caption:
        detail = _truncate(message.caption, 120)
    return "message", content, detail


def describe_callback_event(callback) -> tuple[str, str, str | None]:
    from aiogram.types import CallbackQuery

    if not isinstance(callback, CallbackQuery):
        return "callback", "unknown", None
    data = callback.data or "empty"
    return "callback", data, None
