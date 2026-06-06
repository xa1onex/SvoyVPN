"""User-facing support ticket handlers."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from support_bot.ai_service import SupportAIService
from support_bot.config import SupportBotConfig, is_staff
from support_bot.db import (
    MessageRole,
    TicketStatus,
    add_message,
    create_ticket,
    format_ticket_status,
    get_ticket,
    get_ticket_messages,
    get_user_open_ticket,
    list_user_tickets,
    save_rating,
    update_ticket_status,
)
from support_bot.keyboards import (
    BTN_HELP,
    BTN_MY_TICKETS,
    BTN_NEW_TICKET,
    BTN_STAFF_AI,
    BTN_STAFF_MENU,
    BTN_STAFF_TICKETS,
    main_menu_kb,
    rating_kb,
    staff_menu_kb,
    ticket_actions_kb,
)
from support_bot.states import UserSupportStates

logger = logging.getLogger(__name__)


def _history_for_ai(messages: list[dict]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in messages:
        role = m["sender_role"]
        if role == MessageRole.USER.value:
            out.append({"role": "user", "content": m["text"]})
        elif role in (MessageRole.AI.value, MessageRole.STAFF.value):
            out.append({"role": "assistant", "content": m["text"]})
    return out


async def _notify_staff_human(bot: Bot, config: SupportBotConfig, ticket_id: int, user_id: int) -> None:
    staff_ids = set(config.staff_ids) | set(config.main_bot_admin_ids)
    text = (
        f"🙋 <b>Запрос оператора</b>\n"
        f"Тикет #{ticket_id}\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Откройте бота поддержки → Очередь"
    )
    for sid in staff_ids:
        try:
            await bot.send_message(sid, text, parse_mode="HTML")
        except Exception:
            logger.debug("Cannot notify staff %s", sid)


def setup_user_handlers(
    dp: Dispatcher,
    bot: Bot,
    config: SupportBotConfig,
    ai: SupportAIService,
) -> None:

    @dp.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        if message.from_user and is_staff(message.from_user.id, config):
            from support_bot.handlers.staff import STAFF_MENU_TEXT

            await message.answer(
                f"👋 Режим оператора.\n\n{STAFF_MENU_TEXT}",
                reply_markup=staff_menu_kb(),
                parse_mode="HTML",
            )
            return
        name = message.from_user.first_name if message.from_user else "друг"
        await message.answer(
            f"👋 Здравствуйте, {name}!\n\n"
            f"Это техподдержка <b>{config.service_name}</b>.\n"
            "Опишите проблему — AI-ассистент поможет с подпиской, VPN и оплатой.\n"
            "В любой момент можно позвать живого оператора.\n\n"
            "📝 <b>Новое обращение</b> — создать тикет\n"
            "📂 <b>Мои тикеты</b> — история обращений",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )

    @dp.message(Command("help"))
    @dp.message(F.text == BTN_HELP)
    async def cmd_help(message: Message):
        if message.from_user and is_staff(message.from_user.id, config):
            return
        await message.answer(
            "Как пользоваться:\n"
            "1. Нажмите «Новое обращение» и опишите вопрос одним сообщением.\n"
            "2. Продолжайте диалог в тикете — ассистент видит историю.\n"
            "3. «Позвать человека» — подключим оператора.\n"
            "4. «Закрыть обращение» — завершение и оценка 1–5.\n\n"
            "Команды: /start /help /new",
        )

    @dp.message(Command("new"))
    @dp.message(F.text == BTN_NEW_TICKET)
    async def new_ticket(message: Message, state: FSMContext):
        if not message.from_user:
            return
        if is_staff(message.from_user.id, config):
            return
        uid = message.from_user.id
        existing = await get_user_open_ticket(uid)
        if existing:
            await state.set_state(UserSupportStates.in_ticket)
            await state.update_data(ticket_id=existing["id"])
            await message.answer(
                f"У вас уже есть открытый тикет #{existing['id']} ({format_ticket_status(existing['status'])}).\n"
                "Напишите сообщение или закройте его кнопкой ниже.",
                reply_markup=ticket_actions_kb(existing["id"]),
            )
            return

        ticket_id = await create_ticket(uid)
        await add_message(
            ticket_id,
            MessageRole.SYSTEM,
            "Тикет создан",
        )
        await state.set_state(UserSupportStates.in_ticket)
        await state.update_data(ticket_id=ticket_id)
        await message.answer(
            f"✅ Обращение #{ticket_id} создано.\n"
            "Опишите проблему или вопрос — ассистент ответит с учётом вашего аккаунта.",
            reply_markup=ticket_actions_kb(ticket_id),
        )

    @dp.message(F.text == BTN_MY_TICKETS)
    async def my_tickets(message: Message):
        if not message.from_user:
            return
        if is_staff(message.from_user.id, config):
            return
        tickets = await list_user_tickets(message.from_user.id)
        if not tickets:
            await message.answer("У вас пока нет обращений. Создайте «Новое обращение».")
            return
        lines = []
        for t in tickets:
            created = t["created_at"].strftime("%d.%m.%Y %H:%M") if t["created_at"] else "—"
            rating = f" · оценка {t['rating']}/5" if t.get("rating") else ""
            lines.append(
                f"#{t['id']} — {format_ticket_status(t['status'])}{rating}\n   {created}"
            )
        await message.answer("📂 Ваши обращения:\n\n" + "\n\n".join(lines))

    @dp.callback_query(F.data.startswith("ticket_human:"))
    async def call_human(callback: CallbackQuery, state: FSMContext):
        ticket_id = int(callback.data.split(":")[1])
        ticket = await get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != callback.from_user.id:
            await callback.answer("Тикет не найден", show_alert=True)
            return
        await update_ticket_status(
            ticket_id,
            TicketStatus.AWAITING_HUMAN,
            human_requested=True,
        )
        await add_message(
            ticket_id,
            MessageRole.SYSTEM,
            "Пользователь запросил живого оператора",
        )
        await _notify_staff_human(bot, config, ticket_id, callback.from_user.id)
        await callback.message.answer(
            "🙋 Запрос передан оператору. Ожидайте ответа в этом чате — обычно в течение рабочего дня.",
            reply_markup=ticket_actions_kb(ticket_id),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("ticket_close:"))
    async def close_ticket(callback: CallbackQuery, state: FSMContext):
        ticket_id = int(callback.data.split(":")[1])
        ticket = await get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != callback.from_user.id:
            await callback.answer("Тикет не найден", show_alert=True)
            return
        await update_ticket_status(ticket_id, TicketStatus.CLOSED)
        await add_message(ticket_id, MessageRole.SYSTEM, "Тикет закрыт пользователем")
        await state.set_state(UserSupportStates.awaiting_rating_comment)
        await state.update_data(rating_ticket_id=ticket_id)
        closing = await ai.closing_message()
        await callback.message.answer(closing, reply_markup=rating_kb(ticket_id))
        await callback.answer("Обращение закрыто")

    @dp.callback_query(F.data.startswith("ticket_rate:"))
    async def rate_ticket(callback: CallbackQuery, state: FSMContext):
        parts = callback.data.split(":")
        ticket_id = int(parts[1])
        if parts[0] == "ticket_rate_skip":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Спасибо!")
            await state.clear()
            return
        rating = int(parts[2])
        await save_rating(ticket_id, rating)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"Спасибо за оценку {rating}/5! "
            "Можете добавить комментарий одним сообщением или нажмите /start."
        )
        await state.set_state(UserSupportStates.awaiting_rating_comment)
        await state.update_data(rating_ticket_id=ticket_id)
        await callback.answer()

    @dp.message(UserSupportStates.awaiting_rating_comment)
    async def rating_comment(message: Message, state: FSMContext):
        data = await state.get_data()
        ticket_id = data.get("rating_ticket_id")
        if ticket_id and message.text:
            await save_rating(ticket_id, await _get_rating_or_5(ticket_id), message.text.strip())
        await state.clear()
        await message.answer("Спасибо за отзыв! 🙏", reply_markup=main_menu_kb())

    @dp.message(UserSupportStates.in_ticket, F.text)
    async def ticket_message(message: Message, state: FSMContext):
        if not message.from_user or not message.text:
            return

        data = await state.get_data()
        ticket_id = data.get("ticket_id")
        if not ticket_id:
            open_t = await get_user_open_ticket(message.from_user.id)
            if not open_t:
                await message.answer("Сначала создайте обращение: «Новое обращение».")
                return
            ticket_id = open_t["id"]
            await state.update_data(ticket_id=ticket_id)

        ticket = await get_ticket(ticket_id)
        if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
            await state.clear()
            await message.answer("Это обращение закрыто. Создайте новое.", reply_markup=main_menu_kb())
            return

        user_text = message.text.strip()
        await add_message(ticket_id, MessageRole.USER, user_text, message.from_user.id)

        if ticket["status"] == TicketStatus.AWAITING_HUMAN.value:
            await message.answer(
                "Ваше сообщение сохранено. Оператор скоро ответит. "
                "Можете продолжать уточнять детали.",
                reply_markup=ticket_actions_kb(ticket_id),
            )
            return

        wait_msg = await message.answer("⏳ Думаю…")
        history = _history_for_ai(await get_ticket_messages(ticket_id))
        try:
            reply = await ai.reply_in_ticket(
                user_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                history=history[:-1],
                user_message=user_text,
            )
        except Exception:
            logger.exception("AI reply failed")
            reply = "Произошла ошибка. Нажмите «Позвать человека» — подключим оператора."

        await add_message(ticket_id, MessageRole.AI, reply)
        await wait_msg.delete()
        await message.answer(reply, reply_markup=ticket_actions_kb(ticket_id))

    @dp.message(F.text)
    async def fallback_text(message: Message, state: FSMContext):
        if not message.from_user:
            return
        if is_staff(message.from_user.id, config):
            return
        text = (message.text or "").strip()
        if text in (BTN_STAFF_TICKETS, BTN_STAFF_AI, BTN_STAFF_MENU):
            return
        current = await state.get_state()
        if current and current.startswith("StaffStates"):
            return
        open_t = await get_user_open_ticket(message.from_user.id)
        if open_t:
            await state.set_state(UserSupportStates.in_ticket)
            await state.update_data(ticket_id=open_t["id"])
            await ticket_message(message, state)
        else:
            await message.answer(
                "Чтобы начать, нажмите «📝 Новое обращение».",
                reply_markup=main_menu_kb(),
            )


async def _get_rating_or_5(ticket_id: int) -> int:
    t = await get_ticket(ticket_id)
    return int(t["rating"]) if t and t.get("rating") else 5
