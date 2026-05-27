"""FSM-стейты для aiogram-dialog flows. Держим отдельно от
[app/bot/states.py](../states.py) — там legacy aiogram-FSM, тут dialog-states.

Plus shared helper `user_from(manager)` — единственная точка где обрабатывается
union-тип `DialogManager.event` без mypy-шума.
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import User
from aiogram_dialog import DialogManager


def user_from(manager: DialogManager) -> User | None:
    """`manager.event` — Union[Message, CallbackQuery, ErrorEvent, …]; from_user
    есть только у части. Здесь — один корректный access-point с проверкой."""
    event = manager.event
    return getattr(event, "from_user", None)


class NewGroupSG(StatesGroup):
    """Мастер создания группы:
    style → (style_typing if «ввести своё») → level → weekdays → lesson_time
    → venue → (venue_new_name → venue_new_address if «новая») → room → teachers
    """

    style = State()
    style_typing = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    venue = State()
    venue_new_name = State()
    venue_new_address = State()
    room = State()
    teachers = State()


class CreateSchoolSG(StatesGroup):
    """Свежий admin даёт имя своей школе. Открывается из cmd_start."""

    name = State()


class RenameSchoolSG(StatesGroup):
    """Owner переименовывает школу. school_id в start_data."""

    name = State()


class EditNameSG(StatesGroup):
    """Юзер меняет своё display-имя."""

    name = State()


class BroadcastSG(StatesGroup):
    """Препод шлёт сообщение группе: ввод текста → preview-подтверждение.
    group_id в start_data, text копится в dialog_data."""

    text = State()
    confirm = State()


class EditGroupSG(StatesGroup):
    """Редактирование группы: menu + sub-окна по полям. group_id хранится
    в dialog_data, передаётся через start(..., data={'group_id': gid})."""

    menu = State()
    style = State()
    style_typing = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    venue = State()
    venue_new_name = State()
    venue_new_address = State()
    room = State()
    teachers = State()
