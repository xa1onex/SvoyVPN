from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# --- Пользователь ---

BTN_NEW_TICKET = "📝 Новое обращение"
BTN_MY_TICKETS = "📂 Мои тикеты"
BTN_HELP = "ℹ️ Помощь"

# --- Админ ---
BTN_STAFF_TICKETS = "📋 Ответы на тикеты"
BTN_STAFF_AI = "🤖 Режим ИИ"
BTN_STAFF_MENU = "🛠 Меню админа"


def main_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_NEW_TICKET))
    b.row(KeyboardButton(text=BTN_MY_TICKETS))
    b.row(KeyboardButton(text=BTN_HELP))
    return b.as_markup(resize_keyboard=True)


def staff_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню оператора — выбор режима."""
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_STAFF_TICKETS))
    b.row(KeyboardButton(text=BTN_STAFF_AI))
    return b.as_markup(resize_keyboard=True)


def staff_mode_kb() -> ReplyKeyboardMarkup:
    """Внутри режима — переключение и выход в меню."""
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=BTN_STAFF_TICKETS), KeyboardButton(text=BTN_STAFF_AI))
    b.row(KeyboardButton(text=BTN_STAFF_MENU))
    return b.as_markup(resize_keyboard=True)


def ticket_actions_kb(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🙋 Позвать человека",
            callback_data=f"ticket_human:{ticket_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Закрыть обращение",
            callback_data=f"ticket_close:{ticket_id}",
        )
    )
    return builder.as_markup()


def rating_kb(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in range(1, 6):
        builder.add(
            InlineKeyboardButton(
                text=str(n),
                callback_data=f"ticket_rate:{ticket_id}:{n}",
            )
        )
    builder.adjust(5)
    builder.row(
        InlineKeyboardButton(
            text="Пропустить",
            callback_data=f"ticket_rate_skip:{ticket_id}",
        )
    )
    return builder.as_markup()


def staff_ticket_pick_kb(tickets: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t in tickets[:12]:
        tid = t["id"]
        uname = f"@{t['username']}" if t.get("username") else str(t["user_id"])
        label = f"#{tid} · {uname}"[:60]
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"staff_pick:{tid}")
        )
    return builder.as_markup()
