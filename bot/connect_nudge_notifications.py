"""
Напоминания новым пользователям подключить VPN (запрос /sub в Happ).

Цепочка:
  1) через 1 ч после регистрации — если не было запроса подписки;
  2) через 1 сутки после первого напоминания — если всё ещё не подключился;
  3) за 4 дня до окончания подписки — только если срок подарка ≥ 6 дней.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .activity_log import record_bot_activity
from .database import get_connection
from .plans import (
    SENTINEL_SUBSCRIPTION_END_THRESHOLD,
    format_subscription_end_for_display,
    is_sentinel_subscription_end,
)
from .custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)

NOTIFY_1H = "connect_nudge_1h"
NOTIFY_1D = "connect_nudge_1d"
NOTIFY_4D = "connect_nudge_4d_before_end"

# Минимальная длина подарочной подписки (дней), чтобы успеть 1ч + 1д + «за 4 дня»
MIN_GIFT_DAYS_FOR_4D_NUDGE = 6

_BATCH = 80


def build_connect_nudge_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        btn("Подключить VPN", "vpn_connect", callback_data="get_vpn_link"),
    )
    builder.row(
        btn("Подарок", "gift", callback_data="open_invite"),
        btn("Помощь", "help", callback_data="open_help"),
    )
    return builder.as_markup()


def _message_text(variant: str, *, end_str: str | None = None) -> str:
    if variant == NOTIFY_1H:
        return (
            f"{E.think} <b>Не получается подключиться?</b> Мы поможем!\n\n"
            "Попробуйте подключить VPN — это пара минут через кнопку ниже.\n"
            "Спешите: <b>подарочная подписка</b> может закончиться, "
            "если вы не воспользуетесь сервисом."
        )
    if variant == NOTIFY_1D:
        return (
            f"{E.clock} <b>VPN всё ещё не подключён</b>\n\n"
            "Вы зарегистрировались, но подписка в Happ ещё не открывалась.\n"
            "Нажмите <b>«Подключить VPN»</b> — подскажем по шагам.\n\n"
            "Подарочный период не бесконечный — успейте попробовать!"
        )
    if variant == NOTIFY_4D:
        date_part = f" до <b>{end_str}</b>" if end_str else ""
        return (
            f"{E.warning} <b>Через 4 дня заканчивается подарочный Plus</b>{date_part}\n\n"
            "Вы ещё не подключали VPN. Успейте воспользоваться — "
            "осталось мало времени.\n\n"
            "Нужна помощь — кнопка <b>«Помощь»</b> ниже."
        )
    return f"{E.vpn_connect} Подключите VPN через кнопку ниже."


async def _mark_sent(conn, user_id: int, notification_type: str) -> None:
    await conn.execute(
        "INSERT INTO user_notifications (user_id, notification_type) VALUES ($1, $2)",
        user_id,
        notification_type,
    )


async def _send_nudge(bot: Bot, user_id: int, variant: str, *, end_str: str | None = None) -> bool:
    try:
        await bot.send_message(
            user_id,
            _message_text(variant, end_str=end_str),
            parse_mode="HTML",
            reply_markup=build_connect_nudge_keyboard(),
        )
        await record_bot_activity(user_id, "notification", f"sent:{variant}")
        return True
    except Exception as e:
        logger.debug("connect_nudge send failed user=%s type=%s: %s", user_id, variant, e)
        return False


async def run_connect_nudge_reminders(bot: Bot) -> None:
    """Периодическая проверка (каждые ~15 мин)."""
    await _process_1h_nudges(bot)
    await _process_1d_nudges(bot)
    await _process_4d_nudges(bot)


async def run_connect_nudge_backfill(bot: Bot) -> None:
    """Один раз после деплоя — догнать накопившихся без подключения."""
    logger.info("connect_nudge: starting backfill for users without VPN connection")
    sent = 0
    sent += await _process_4d_nudges(bot)
    sent += await _process_1d_backfill(bot)
    sent += await _process_1h_nudges(bot, immediate=True, registration_window_days=None)
    logger.info("connect_nudge: backfill done, sent=%s", sent)


async def _process_1d_backfill(bot: Bot) -> int:
    """Старые регистрации (>1 дня): сразу второе напоминание, без ожидания 1ч."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.blacklisted = FALSE
              AND u.registration_date <= NOW() - INTERVAL '1 day'
              AND NOT EXISTS (
                  SELECT 1 FROM subscription_usage_logs s WHERE s.user_id = u.user_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id
                    AND n.notification_type IN ($1, $2, $3)
              )
            ORDER BY u.registration_date ASC
            LIMIT $4
            """,
            NOTIFY_1H,
            NOTIFY_1D,
            NOTIFY_4D,
            _BATCH,
        )

    sent = 0
    for row in rows:
        uid = row["user_id"]
        if await _send_nudge(bot, uid, NOTIFY_1D):
            async with get_connection() as conn:
                await _mark_sent(conn, uid, NOTIFY_1H)
                await _mark_sent(conn, uid, NOTIFY_1D)
            sent += 1
    if sent:
        logger.info("connect_nudge 1d backfill: sent=%s", sent)
    return sent


async def _process_1h_nudges(
    bot: Bot,
    *,
    immediate: bool = False,
    registration_window_days: int | None = 30,
) -> int:
    reg_cutoff = (
        f"AND u.registration_date >= NOW() - INTERVAL '{registration_window_days} days'"
        if registration_window_days is not None
        else ""
    )
    time_clause = (
        "TRUE" if immediate else "u.registration_date <= NOW() - INTERVAL '1 hour'"
    )
    async with get_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT u.user_id
            FROM users u
            WHERE u.blacklisted = FALSE
              AND ({time_clause})
              {reg_cutoff}
              AND NOT EXISTS (
                  SELECT 1 FROM subscription_usage_logs s WHERE s.user_id = u.user_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = $1
              )
            ORDER BY u.registration_date ASC
            LIMIT $2
            """,
            NOTIFY_1H,
            _BATCH,
        )

    sent = 0
    for row in rows:
        uid = row["user_id"]
        if await _send_nudge(bot, uid, NOTIFY_1H):
            async with get_connection() as conn:
                await _mark_sent(conn, uid, NOTIFY_1H)
            sent += 1
    if sent:
        logger.info("connect_nudge 1h: sent=%s immediate=%s", sent, immediate)
    return sent


async def _process_1d_nudges(bot: Bot) -> int:
    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.blacklisted = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM subscription_usage_logs s WHERE s.user_id = u.user_id
              )
              AND EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = $1
                    AND n.created_at <= NOW() - INTERVAL '1 day'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = $2
              )
            ORDER BY u.registration_date ASC
            LIMIT $3
            """,
            NOTIFY_1H,
            NOTIFY_1D,
            _BATCH,
        )

    sent = 0
    for row in rows:
        uid = row["user_id"]
        if await _send_nudge(bot, uid, NOTIFY_1D):
            async with get_connection() as conn:
                await _mark_sent(conn, uid, NOTIFY_1D)
            sent += 1
    if sent:
        logger.info("connect_nudge 1d: sent=%s", sent)
    return sent


async def _process_4d_nudges(bot: Bot, *, backfill: bool = False) -> int:
    sentinel = SENTINEL_SUBSCRIPTION_END_THRESHOLD
    min_days = MIN_GIFT_DAYS_FOR_4D_NUDGE

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.subscription_end
            FROM users u
            WHERE u.blacklisted = FALSE
              AND u.pay_subscribed = TRUE
              AND u.subscription_end IS NOT NULL
              AND DATE(u.subscription_end) >= CURRENT_DATE
              AND DATE(u.subscription_end) < $1
              AND DATE(u.subscription_end) = CURRENT_DATE + INTERVAL '4 days'
              AND (DATE(u.subscription_end) - DATE(u.registration_date)) >= $2
              AND NOT EXISTS (
                  SELECT 1 FROM subscription_usage_logs s WHERE s.user_id = u.user_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_notifications n
                  WHERE n.user_id = u.user_id AND n.notification_type = $3
              )
            ORDER BY u.subscription_end ASC
            LIMIT $4
            """,
            sentinel,
            min_days,
            NOTIFY_4D,
            _BATCH,
        )

    sent = 0
    for row in rows:
        uid = row["user_id"]
        sub_end = row["subscription_end"]
        if is_sentinel_subscription_end(sub_end):
            continue
        end_str = format_subscription_end_for_display(sub_end)
        if await _send_nudge(bot, uid, NOTIFY_4D, end_str=end_str):
            async with get_connection() as conn:
                await _mark_sent(conn, uid, NOTIFY_4D)
            sent += 1
    if sent:
        logger.info("connect_nudge 4d: sent=%s", sent)
    return sent
