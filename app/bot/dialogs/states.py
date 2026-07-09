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
    → duration → venue (текст одной строкой заводит новую) → room → teachers
    """

    style = State()
    style_typing = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    duration = State()
    venue = State()
    room = State()
    teachers = State()


class CreateSchoolSG(StatesGroup):
    """Свежий admin даёт имя своей школе и (опционально) место занятий —
    дефолтную площадку для будущих групп. Открывается из cmd_start."""

    name = State()
    place = State()


class RenameSchoolSG(StatesGroup):
    """Owner переименовывает школу. school_id в start_data."""

    name = State()


class AddVenueSG(StatesGroup):
    """Член школы добавляет площадку из меню школы. school_id в start_data."""

    name = State()


class AddTeacherPresetSG(StatesGroup):
    """Член школы сохраняет связку «кто ведёт». school_id в start_data."""

    note = State()


class EditNameSG(StatesGroup):
    """Юзер меняет своё display-имя."""

    name = State()


class BroadcastSG(StatesGroup):
    """Препод шлёт сообщение группе: ввод текста → preview-подтверждение →
    прогресс доставки (outbox асинхронный, Phase 5). group_id в start_data,
    text/broadcast_id копятся в dialog_data."""

    text = State()
    confirm = State()
    progress = State()


class DevBroadcastSG(StatesGroup):
    """Phase 11: dev-only глобальная рассылка через все/часть школ. Тот же
    Broadcast+outbox механизм, что и BroadcastSG (Phase 5) — просто другой
    источник получателей. Открывается ТОЛЬКО из dev-меню, гейт по
    settings.developer_ids (см. cb_dev_broadcast_start в admin.py)."""

    scope = State()
    scope_pick = State()
    audience = State()
    text = State()
    confirm = State()
    progress = State()


class EditGroupSG(StatesGroup):
    """Редактирование группы: menu + sub-окна по полям. group_id хранится
    в dialog_data, передаётся через start(..., data={'group_id': gid})."""

    menu = State()
    style = State()
    style_typing = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    duration = State()
    venue = State()
    room = State()
    teachers = State()
