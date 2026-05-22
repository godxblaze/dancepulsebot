from aiogram.fsm.state import State, StatesGroup


class NewGroup(StatesGroup):
    name = State()
    weekdays = State()
    lesson_time = State()
