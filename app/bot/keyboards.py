from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Group

_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def weekdays_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, name in enumerate(_WD_NAMES):
        mark = "✅ " if idx in selected else ""
        kb.button(text=f"{mark}{name}", callback_data=f"wd:{idx}")
    kb.button(text="Готово ▶", callback_data="wd:done")
    kb.adjust(4, 3, 1)
    return kb.as_markup()


def groups_keyboard(groups: list[Group]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for g in groups:
        kb.button(text=g.name, callback_data=f"grp:{g.id}")
    kb.adjust(1)
    return kb.as_markup()


def group_actions_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Ученики", callback_data=f"grpstu:{group_id}")],
            [
                InlineKeyboardButton(
                    text="🔗 Ссылка-приглашение", callback_data=f"grplink:{group_id}"
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"grpdel:{group_id}")],
            [InlineKeyboardButton(text="◀ К списку", callback_data="grplist")],
        ]
    )


def attendance_keyboard(survey_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Приду", callback_data=f"att:{survey_id}:yes"),
                InlineKeyboardButton(text="❌ Не приду", callback_data=f"att:{survey_id}:no"),
            ]
        ]
    )


def confirm_delete_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✗ Отмена", callback_data=f"grp:{group_id}"),
                InlineKeyboardButton(text="✓ Удалить", callback_data=f"grpdelok:{group_id}"),
            ]
        ]
    )
