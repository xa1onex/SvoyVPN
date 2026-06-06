"""Панель оператора: два режима — тикеты и ИИ."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from support_bot.ai_service import SupportAIService
from support_bot.config import SupportBotConfig, is_staff
from support_bot.db import (
    MessageRole,
    TicketStatus,
    add_message,
    format_ticket_status,
    get_ticket,
    get_ticket_messages,
    list_open_tickets_for_staff,
    update_ticket_status,
)
from support_bot.keyboards import (
    BTN_STAFF_AI,
    BTN_STAFF_MENU,
    BTN_STAFF_TICKETS,
    staff_menu_kb,
    staff_mode_kb,
    staff_ticket_pick_kb,
)
from support_bot.states import StaffStates
from bot.database import get_connection

logger = logging.getLogger(__name__)

STAFF_MENU_TEXT = (
    "🛠 <b>Меню оператора</b>\n\n"
    f"<b>{BTN_STAFF_TICKETS}</b> — очередь тикетов, ответы пользователям\n"
    f"<b>{BTN_STAFF_AI}</b> — чат с ИИ (БД, подписки, логи)\n\n"
    "Команда: /admin"
)


async def _show_ticket_queue(message: Message, state: FSMContext) -> None:
    await state.set_state(StaffStates.tickets_mode)
    await state.update_data(staff_ticket_id=None)
    tickets = await list_open_tickets_for_staff()
    if not tickets:
        await message.answer(
            "📋 Очередь пуста — нет открытых тикетов.",
            reply_markup=staff_mode_kb(),
        )
        return
    lines = ["📋 <b>Открытые тикеты</b> — выберите кнопкой ниже:\n"]
    for t in tickets[:12]:
        uname = f"@{t['username']}" if t.get("username") else "—"
        human = " 🙋" if t.get("human_requested") else ""
        lines.append(
            f"#{t['id']}{human} · {format_ticket_status(t['status'])}\n"
            f"   {t['first_name'] or '—'} {uname} · <code>{t['user_id']}</code>"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=staff_ticket_pick_kb(tickets),
        parse_mode="HTML",
    )
    await message.answer(
        "После выбора тикета пишите текст — он уйдёт пользователю.",
        reply_markup=staff_mode_kb(),
    )


async def _open_ticket_for_reply(
    message: Message,
    state: FSMContext,
    ticket_id: int,
) -> None:
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        await message.answer("Тикет закрыт или не найден.")
        return
    msgs = await get_ticket_messages(ticket_id, limit=12)
    hist = "\n".join(
        f"[{m['sender_role']}] {(m['text'] or '')[:180]}"
        for m in msgs[-8:]
    ) or "(пусто)"
    await state.set_state(StaffStates.tickets_mode)
    await state.update_data(staff_ticket_id=ticket_id)
    if ticket["status"] != TicketStatus.AWAITING_HUMAN.value:
        await update_ticket_status(
            ticket_id,
            TicketStatus.AWAITING_HUMAN,
            assigned_staff_id=message.from_user.id if message.from_user else None,
        )
    uname = "—"
    async with get_connection() as conn:
        u = await conn.fetchrow(
            "SELECT username, first_name FROM users WHERE user_id = $1",
            ticket["user_id"],
        )
        if u:
            uname = f"@{u['username']}" if u["username"] else (u["first_name"] or "—")
    await message.answer(
        f"💬 <b>Тикет #{ticket_id}</b> · {uname} · <code>{ticket['user_id']}</code>\n\n"
        f"<b>История:</b>\n{hist}\n\n"
        "Пишите сообщение — ответ уйдёт пользователю.",
        reply_markup=staff_mode_kb(),
        parse_mode="HTML",
    )


def setup_staff_handlers(
    dp: Dispatcher,
    bot: Bot,
    config: SupportBotConfig,
    ai: SupportAIService,
) -> None:

    @dp.message(Command("admin"))
    async def cmd_admin(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        await state.clear()
        await message.answer(STAFF_MENU_TEXT, reply_markup=staff_menu_kb(), parse_mode="HTML")

    @dp.message(F.text == BTN_STAFF_MENU)
    async def staff_back_menu(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        await state.clear()
        await message.answer(STAFF_MENU_TEXT, reply_markup=staff_menu_kb(), parse_mode="HTML")

    @dp.message(F.text == BTN_STAFF_TICKETS)
    async def enter_tickets_mode(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        await _show_ticket_queue(message, state)

    @dp.message(F.text == BTN_STAFF_AI)
    async def enter_ai_mode(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        await state.set_state(StaffStates.ai_mode)
        await state.update_data(staff_ticket_id=None)
        await message.answer(
            "🤖 <b>Режим ИИ</b>\n"
            "Пишите запросы: проверка подписки, продление, поиск юзера, логи…\n"
            f"«{BTN_STAFF_TICKETS}» — ответы на тикеты · «{BTN_STAFF_MENU}» — меню",
            reply_markup=staff_mode_kb(),
            parse_mode="HTML",
        )

    @dp.callback_query(F.data.startswith("staff_pick:"))
    async def pick_ticket(callback: CallbackQuery, state: FSMContext):
        if not is_staff(callback.from_user.id, config):
            await callback.answer("Нет доступа", show_alert=True)
            return
        ticket_id = int(callback.data.split(":")[1])
        await _open_ticket_for_reply(callback.message, state, ticket_id)
        await callback.answer()

    @dp.message(StaffStates.tickets_mode, F.text)
    async def tickets_mode_text(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        text = (message.text or "").strip()
        if text in (BTN_STAFF_MENU, BTN_STAFF_TICKETS, BTN_STAFF_AI):
            return
        if text.startswith("/"):
            return

        data = await state.get_data()
        ticket_id = data.get("staff_ticket_id")
        if not ticket_id:
            await message.answer("Сначала выберите тикет из очереди (кнопка «Ответы на тикеты»).")
            return

        ticket = await get_ticket(ticket_id)
        if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
            await message.answer("Тикет закрыт. Выберите другой из очереди.")
            await state.update_data(staff_ticket_id=None)
            return

        await add_message(ticket_id, MessageRole.STAFF, text, message.from_user.id)
        try:
            await bot.send_message(
                ticket["user_id"],
                f"👨‍💼 <b>Оператор</b>\n\n{text}",
                parse_mode="HTML",
            )
            await message.answer(f"✅ Отправлено в тикет #{ticket_id}", reply_markup=staff_mode_kb())
        except Exception as e:
            await message.answer(f"❌ Не доставлено: {e}")

    @dp.message(StaffStates.ai_mode, F.text)
    async def ai_mode_text(message: Message, state: FSMContext):
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        text = (message.text or "").strip()
        if text in (BTN_STAFF_MENU, BTN_STAFF_TICKETS, BTN_STAFF_AI):
            return
        if text.startswith("/"):
            return

        wait = await message.answer("⏳ Думаю…")
        try:
            reply = await ai.staff_command(text, message.from_user.id)
        except Exception:
            logger.exception("Staff AI failed")
            reply = "Ошибка AI. Повторите запрос."
        await wait.delete()
        await message.answer(reply, reply_markup=staff_mode_kb())

    @dp.message(F.text)
    async def staff_need_admin(message: Message, state: FSMContext):
        """Сообщения оператора без режима — только подсказка /admin."""
        if not message.from_user or not is_staff(message.from_user.id, config):
            return
        await message.answer(
            "Вы оператор. Нажмите /admin и выберите режим:\n"
            f"· {BTN_STAFF_TICKETS}\n"
            f"· {BTN_STAFF_AI}",
            reply_markup=staff_menu_kb(),
        )
