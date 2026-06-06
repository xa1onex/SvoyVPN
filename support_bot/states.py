from aiogram.fsm.state import State, StatesGroup


class UserSupportStates(StatesGroup):
    in_ticket = State()
    awaiting_rating_comment = State()


class StaffStates(StatesGroup):
    ai_mode = State()
    tickets_mode = State()
