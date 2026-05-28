"""Aiogram FSM states которые НЕ мигрировали на aiogram-dialog.

Все wizard'ы и пер-полевые edit-flow живут в `app/bot/dialogs/`; здесь —
короткие FSM которые ловят одно сообщение в контексте (JoinFlow, CancelLesson).
"""
from aiogram.fsm.state import State, StatesGroup


class JoinFlow(StatesGroup):
    name = State()
    gender = State()


class CancelLesson(StatesGroup):
    """Опциональный ввод причины отмены занятия. group_id и ts — в FSM data."""

    reason = State()


class EntitySearch(StatesGroup):
    """Поиск по имени в длинных списках людей (walk-in / pool / ростер).
    kind и контекст (survey_id/group_id) — в FSM data."""

    query = State()
