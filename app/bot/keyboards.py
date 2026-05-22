from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Group, Person, SupportScope

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
            [InlineKeyboardButton(text="📷 QR-код", callback_data=f"grpqr:{group_id}")],
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


def join_name_keyboard(suggested: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Записать как «{suggested}»", callback_data="join:name_ok"
                )
            ]
        ]
    )


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Парень", callback_data="join:gender:male"),
                InlineKeyboardButton(text="Девушка", callback_data="join:gender:female"),
            ]
        ]
    )


def optin_keyboard(person_id: int, in_pool: bool) -> InlineKeyboardMarkup:
    text = (
        "✅ Ты в пуле саппортов — выйти"
        if in_pool
        else "🆘 Иногда выручать другие группы"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=f"optin:{person_id}")]
        ]
    )


def pool_keyboard(persons: list[Person], scope: SupportScope) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in persons:
        mark = "✅" if p.in_teacher_pool else "☐"
        helped = f" · помог {p.helped_count}" if p.helped_count else ""
        kb.button(text=f"{mark} {p.full_name}{helped}", callback_data=f"tpool:{p.id}")
    scope_text = (
        "🔁 Зову: мой пул + общий"
        if scope == SupportScope.own_and_open
        else "🔁 Зову: только мой пул"
    )
    kb.button(text=scope_text, callback_data="sscope:toggle")
    kb.adjust(1)
    return kb.as_markup()


def support_confirm_keyboard(call_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добрать", callback_data=f"sup:yes:{call_id}"),
                InlineKeyboardButton(text="✗ Не надо", callback_data=f"sup:no:{call_id}"),
            ]
        ]
    )


def support_ask_keyboard(call_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Приду помочь", callback_data=f"sup:join:{call_id}"
                ),
                InlineKeyboardButton(
                    text="✗ Не смогу", callback_data=f"sup:skip:{call_id}"
                ),
            ]
        ]
    )


MENU_GROUPS = "📋 Мои группы"
MENU_TODAY = "📅 Сегодня"
MENU_STATS = "📊 Статистика"
MENU_POOL = "🆘 Саппорты"
MENU_NEWGROUP = "➕ Новая группа"
MENU_TEXTS = frozenset({MENU_GROUPS, MENU_TODAY, MENU_STATS, MENU_POOL, MENU_NEWGROUP})


def teacher_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu for the teacher — instead of typing slash-commands."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_GROUPS), KeyboardButton(text=MENU_TODAY)],
            [KeyboardButton(text=MENU_STATS), KeyboardButton(text=MENU_POOL)],
            [KeyboardButton(text=MENU_NEWGROUP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
