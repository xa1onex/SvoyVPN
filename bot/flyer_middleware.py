"""
Middleware: обязательная подписка через Flyer (api.flyerhubs.com).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import AppConfig
from .flyer_client import FlyerClient

logger = logging.getLogger(__name__)

FLYER_RECHECK_CALLBACK = "flyer_recheck"
_TASK_LABELS = {
    "start bot": "Запустить бота",
    "subscribe channel": "Подписаться на канал",
    "give boost": "Выдать буст",
    "follow link": "Перейти по ссылке",
    "perform action": "Выполнить действие",
    "view posts": "Посмотреть посты",
}


def _user_id_from_event(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user.id
    return None


def _language_code_from_event(event: TelegramObject) -> str | None:
    if isinstance(event, Message) and event.from_user:
        return event.from_user.language_code
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user.language_code
    return None


def _should_skip_flyer_check(event: TelegramObject, config: AppConfig) -> bool:
    user_id = _user_id_from_event(event)
    if user_id is None:
        return True
    if user_id in config.bot.admin_ids:
        return True

    if isinstance(event, Message):
        if not event.from_user or event.from_user.is_bot:
            return True
        if event.successful_payment:
            return True
        return False

    if isinstance(event, CallbackQuery):
        if not event.from_user or event.from_user.is_bot:
            return True
        data = event.data or ""
        if data.startswith(("admin_", "wd_approve:", "wd_reject:")):
            return True
        return False

    return True


def _task_button_text(task: dict[str, Any]) -> str:
    name = (task.get("name") or "").strip()
    if name:
        return name[:60]
    task_type = task.get("task") or ""
    return _TASK_LABELS.get(task_type, "Выполнить задание")


def build_tasks_keyboard(tasks: list[dict[str, Any]]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for task in tasks:
        links = task.get("links") or []
        if not links and task.get("link"):
            links = [task["link"]]
        label = _task_button_text(task)
        for link in links:
            if link:
                builder.row(InlineKeyboardButton(text=f"📢 {label}", url=str(link)))
    builder.row(
        InlineKeyboardButton(text="✅ Проверить подписку", callback_data=FLYER_RECHECK_CALLBACK)
    )
    return builder


async def send_flyer_subscription_prompt(
    bot: Bot,
    chat_id: int,
    flyer: FlyerClient,
    user_id: int,
    language_code: str | None,
    *,
    edit_message_id: int | None = None,
) -> None:
    tasks = await flyer.get_tasks(user_id, language_code=language_code, limit=5)
    text = (
        "📢 <b>Для продолжения подпишитесь на указанные ресурсы</b>\n\n"
        "Выполните задания по кнопкам ниже, затем нажмите «Проверить подписку»."
    )
    if not tasks:
        text = (
            "📢 <b>Для продолжения нужна подписка на партнёрские каналы</b>\n\n"
            "Подпишитесь на каналы из сообщения Flyer (если оно уже пришло), "
            "затем нажмите «Проверить подписку»."
        )

    markup = build_tasks_keyboard(tasks).as_markup()

    if edit_message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=edit_message_id,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


class FlyerSubscriptionMiddleware(BaseMiddleware):
    """Блокирует обработчики, пока пользователь не прошёл проверку Flyer."""

    def __init__(self, flyer: FlyerClient | None, config: AppConfig, bot: Bot) -> None:
        self._flyer = flyer
        self._config = config
        self._bot = bot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._flyer or not self._flyer.enabled:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == FLYER_RECHECK_CALLBACK:
            return await self._handle_recheck(event)

        if _should_skip_flyer_check(event, self._config):
            return await handler(event, data)

        user_id = _user_id_from_event(event)
        if user_id is None:
            return await handler(event, data)

        language_code = _language_code_from_event(event)
        try:
            subscribed = await self._flyer.check(
                user_id,
                language_code=language_code,
                message={
                    "text": (
                        "📢 <b>Подпишитесь на каналы</b>, чтобы пользоваться ботом.\n\n"
                        "После подписки нажмите «Проверить»."
                    ),
                    "button_channel": "Подписаться",
                    "button_bot": "Запустить бота",
                    "button_url": "Перейти",
                    "button_boost": "Выдать буст",
                },
            )
        except Exception as e:
            logger.error("Flyer check failed for user %s: %s", user_id, e)
            return await handler(event, data)

        if subscribed:
            return await handler(event, data)

        chat_id = None
        if isinstance(event, Message):
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id

        if chat_id is not None:
            await send_flyer_subscription_prompt(
                self._bot,
                chat_id,
                self._flyer,
                user_id,
                language_code,
            )

        if isinstance(event, CallbackQuery):
            await event.answer("Сначала выполните подписку", show_alert=True)
        return None

    async def _handle_recheck(self, callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer()
            return

        user_id = callback.from_user.id
        language_code = callback.from_user.language_code

        try:
            subscribed = await self._flyer.check(user_id, language_code=language_code)
        except Exception as e:
            logger.error("Flyer recheck failed for user %s: %s", user_id, e)
            await callback.answer("Сервис проверки временно недоступен", show_alert=True)
            return

        if subscribed:
            await callback.message.edit_text(
                "✅ <b>Подписка подтверждена!</b>\n\nМожете пользоваться ботом.",
                parse_mode="HTML",
            )
            await callback.answer("Готово!")
            return

        await send_flyer_subscription_prompt(
            self._bot,
            callback.message.chat.id,
            self._flyer,
            user_id,
            language_code,
            edit_message_id=callback.message.message_id,
        )
        await callback.answer("Подписка ещё не подтверждена", show_alert=True)
