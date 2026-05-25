from aiogram.fsm.state import State, StatesGroup


class NewGroup(StatesGroup):
    style = State()
    style_typing = State()  # free-text style entry after «Ввести своё»
    level = State()
    weekdays = State()
    lesson_time = State()
    venue = State()  # pick existing / new / skip
    venue_new_name = State()
    venue_new_address = State()
    room = State()
    teachers_note = State()


class EditGroup(StatesGroup):
    """Per-field edits on an already-created group. `edit_gid` lives in FSM data."""

    style = State()
    style_typing = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    venue = State()
    venue_new_name = State()
    venue_new_address = State()
    room = State()
    teachers_note = State()


class JoinFlow(StatesGroup):
    name = State()
    gender = State()


class EditName(StatesGroup):
    waiting = State()
