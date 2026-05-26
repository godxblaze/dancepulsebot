from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Admin, Gender, Group, Person, Venue

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
        label = f"{g.name} · {g.style}" if g.style else g.name
        if len(label) > 64:
            label = label[:61] + "…"
        kb.button(text=label, callback_data=f"grp:{g.id}")
    kb.adjust(1)
    return kb.as_markup()


def group_actions_keyboard(
    group_id: int, is_owner: bool, is_active: bool = True
) -> InlineKeyboardMarkup:
    """Кнопки в карточке группы. Owner видит больше (изменить, co-админы, удалить);
    co-admin — только то, что ему доступно."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="👥 Ученики", callback_data=f"grpstu:{group_id}")],
    ]
    if is_owner:
        rows.append(
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"grpedit:{group_id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="📨 Пригласить ученика", callback_data=f"grpinv:{group_id}")]
    )
    rows.append(
        [InlineKeyboardButton(text="📢 Сообщение группе", callback_data=f"grpbcast:{group_id}")]
    )
    rows.append([InlineKeyboardButton(
        text="🚫 Отменить ближайшее занятие",
        callback_data=f"grpcancel:{group_id}",
    )])
    rows.append([InlineKeyboardButton(
        text="✏️ Отметить посещаемость",
        callback_data=f"grpattnmark:{group_id}",
    )])
    if is_owner:
        rows.append(
            [InlineKeyboardButton(text="🛠 Со-админы", callback_data=f"grpcoa:{group_id}")]
        )
        if is_active:
            rows.append([InlineKeyboardButton(
                text="⏸ Пауза группы", callback_data=f"grppause:{group_id}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="▶️ Возобновить группу", callback_data=f"grpresume:{group_id}",
            )])
        rows.append(
            [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"grpdel:{group_id}")]
        )
    rows.append([InlineKeyboardButton(text="◀ К списку", callback_data="grplist")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def attendance_mark_keyboard(
    survey_id: int, group_id: int, items: list[tuple[Person, bool | None, str | None]]
) -> InlineKeyboardMarkup:
    """Ростер для пост-leson отметки. `items` — (person, attended, promised_answer).

    Отображает рядом с именем:
      ✅ — реально был (attended=True);
      ❌ — не пришёл (attended=False);
      ☐  — не отмечено (attended=None).
    В скобках promise: «✓ обещал» / «✗ отказался» / «— молчал»."""
    kb = InlineKeyboardBuilder()
    for person, attended, promised in items:
        if attended is True:
            mark = "✅"
        elif attended is False:
            mark = "❌"
        else:
            mark = "☐"
        if promised == "yes":
            hint = "✓"
        elif promised == "no":
            hint = "✗"
        else:
            hint = "—"
        kb.button(
            text=f"{mark} {person.full_name} ({hint})",
            callback_data=f"atntog:{survey_id}:{person.id}",
        )
    kb.button(text="◀ К группе", callback_data=f"grp:{group_id}")
    kb.adjust(1)
    return kb.as_markup()


def closed_surveys_keyboard(
    group_id: int, surveys_with_dates: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """Список последних прошедших занятий — выбрать, какое отмечать."""
    kb = InlineKeyboardBuilder()
    for survey_id, label in surveys_with_dates:
        kb.button(text=label, callback_data=f"atnlesson:{survey_id}:{group_id}")
    kb.button(text="◀ К группе", callback_data=f"grp:{group_id}")
    kb.adjust(1)
    return kb.as_markup()


def pick_lesson_to_cancel_keyboard(
    group_id: int, lessons: list[tuple[int, str, bool]]
) -> InlineKeyboardMarkup:
    """Список ближайших дат занятий с пометкой «уже отменено». Каждая дата —
    кнопка `grpcanceldt:<gid>:<ts>` или disabled-callback для уже отменённых."""
    kb = InlineKeyboardBuilder()
    for ts, label, already_cancelled in lessons:
        if already_cancelled:
            kb.button(text=f"🚫 {label} (уже отменено)", callback_data="noop")
        else:
            kb.button(text=label, callback_data=f"grpcanceldt:{group_id}:{ts}")
    kb.button(text="◀ К группе", callback_data=f"grp:{group_id}")
    kb.adjust(1)
    return kb.as_markup()


def confirm_cancel_lesson_keyboard(group_id: int, ts: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"grpcancel:{group_id}")],
            [InlineKeyboardButton(
                text="🚫 Да, отменить занятие",
                callback_data=f"grpcancelok:{group_id}:{ts}",
            )],
        ]
    )


def coadmins_menu_keyboard(
    group_id: int, coadmins: list[Admin]
) -> InlineKeyboardMarkup:
    """Подменю «Со-админы»: получить ссылку-приглашение + список existing с кнопкой убрать."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="➕ Получить ссылку-приглашение",
            callback_data=f"coalink:{group_id}",
        )]
    ]
    for a in coadmins:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Убрать {a.full_name}",
            callback_data=f"coarem:{group_id}:{a.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀ К группе", callback_data=f"grp:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def coadmin_accept_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"coaacc:{code}")],
            [InlineKeyboardButton(text="✗ Не сейчас", callback_data="coadecl")],
        ]
    )


def group_edit_menu_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """Menu of fields you can change on an existing group."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💃 Направление", callback_data=f"ge:style:{group_id}")],
            [InlineKeyboardButton(text="🎯 Уровень", callback_data=f"ge:level:{group_id}")],
            [InlineKeyboardButton(text="📅 Дни недели", callback_data=f"ge:days:{group_id}")],
            [InlineKeyboardButton(text="🕐 Время", callback_data=f"ge:time:{group_id}")],
            [InlineKeyboardButton(text="📍 Место", callback_data=f"ge:venue:{group_id}")],
            [InlineKeyboardButton(text="🚪 Зал", callback_data=f"ge:room:{group_id}")],
            [InlineKeyboardButton(
                text="👤 Преподаватели", callback_data=f"ge:teachers:{group_id}"
            )],
            [InlineKeyboardButton(text="◀ К группе", callback_data=f"grp:{group_id}")],
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


def flip_answer_keyboard(survey_id: int, current_answer: str) -> InlineKeyboardMarkup:
    """Shown after a student answers — one tap to flip to the opposite answer.

    `current_answer` is "yes" or "no"; the button offers the other one.
    """
    if current_answer == "yes":
        text, data = "❌ Передумал, не приду", f"att:{survey_id}:no"
    else:
        text, data = "✅ Передумал, приду", f"att:{survey_id}:yes"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]]
    )


def lesson_card_keyboard(
    group_id: int, survey_id: int | None, pending_count: int
) -> InlineKeyboardMarkup:
    """Actions on the scheduler's per-lesson auto-summary message."""
    kb = InlineKeyboardBuilder()
    if survey_id is not None and pending_count > 0:
        kb.button(
            text=f"📨 Пинг молчащим ({pending_count})",
            callback_data=f"ping:{survey_id}",
        )
    kb.button(text="📋 Открыть группу", callback_data=f"grp:{group_id}")
    kb.adjust(1)
    return kb.as_markup()


def today_ping_keyboard(
    pending_by_group: list[tuple[str, int, int]]
) -> InlineKeyboardMarkup | None:
    """For /today summary — one ping button per group that still has silent students."""
    if not pending_by_group:
        return None
    kb = InlineKeyboardBuilder()
    for name, survey_id, pending in pending_by_group:
        kb.button(text=f"📨 Пинг «{name}» ({pending})", callback_data=f"ping:{survey_id}")
    kb.adjust(1)
    return kb.as_markup()


def _gender_glyph(gender: Gender | None) -> str:
    """Small icon to put next to a person's name in a roster."""
    if gender == Gender.male:
        return "🕺"
    if gender == Gender.female:
        return "💃"
    return "·"


def students_keyboard(group_id: int, members: list[Person]) -> InlineKeyboardMarkup:
    """Group roster — each name is a button opening the person card."""
    kb = InlineKeyboardBuilder()
    for p in members:
        glyph = _gender_glyph(p.gender)
        lvl = f" · ур.{p.support_level}" if p.support_level else ""
        kb.button(text=f"{glyph} {p.full_name}{lvl}", callback_data=f"pcard:{group_id}:{p.id}")
    kb.button(text="📨 Пригласить ещё", callback_data=f"grpinv:{group_id}")
    kb.button(text="◀ К группе", callback_data=f"grp:{group_id}")
    kb.adjust(1)
    return kb.as_markup()


def person_card_keyboard(
    group_id: int, person_id: int, support_level: int | None, gender: Gender | None
) -> InlineKeyboardMarkup:
    """Actions on a person inside a group: pool level, gender, remove, back.

    The pool-level button shows what tap will do next (cycles 1→2→3→null).
    Gender-toggle button лечит ошибки онбординга (выбрал не тот пол).
    """
    if support_level is None:
        level_text = "🆘 В пул саппортов (тап: → ур.1)"
    elif support_level >= 3:
        level_text = f"🆘 В пуле, ур.{support_level} (тап: убрать)"
    else:
        level_text = f"🆘 В пуле, ур.{support_level} (тап: → ур.{support_level + 1})"
    if gender == Gender.male:
        gender_text = "🕺 Парень (тап: переключить на 💃)"
    elif gender == Gender.female:
        gender_text = "💃 Девушка (тап: переключить на 🕺)"
    else:
        gender_text = "❓ Пол не задан (тап: задать)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=level_text,
                callback_data=f"pcardlvl:{group_id}:{person_id}",
            )],
            [InlineKeyboardButton(
                text=gender_text,
                callback_data=f"pcardgnd:{group_id}:{person_id}",
            )],
            [InlineKeyboardButton(
                text="🚪 Убрать из группы",
                callback_data=f"pcardrem:{group_id}:{person_id}",
            )],
            [InlineKeyboardButton(text="◀ К списку учеников", callback_data=f"grpstu:{group_id}")],
        ]
    )


def confirm_remove_from_group_keyboard(group_id: int, person_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="◀ Отмена", callback_data=f"pcard:{group_id}:{person_id}"
            )],
            [InlineKeyboardButton(
                text="🚪 Да, убрать из группы",
                callback_data=f"pcardremok:{group_id}:{person_id}",
            )],
        ]
    )


def confirm_delete_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """Destructive action and cancel sit on separate rows — harder to mis-tap."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"grp:{group_id}")],
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"grpdelok:{group_id}")],
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


def pool_keyboard(persons: list[Person]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in persons:
        if p.support_level is None:
            label = f"☐ {p.full_name}"
        else:
            helped = f" · помог {p.helped_count}" if p.helped_count else ""
            label = f"✅ {p.full_name} · ур.{p.support_level}{helped}"
        kb.button(text=label, callback_data=f"tpool:{p.id}")
    kb.adjust(1)
    return kb.as_markup()


def support_pick_keyboard(
    call_id: int, candidates: list[Person], selected: set[int]
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in candidates:
        mark = "✅" if p.id in selected else "☐"
        lvl = f" · ур.{p.support_level}" if p.support_level else ""
        kb.button(
            text=f"{mark} {p.full_name}{lvl}", callback_data=f"pick:{call_id}:{p.id}"
        )
    kb.button(text=f"📨 Позвать ({len(selected)})", callback_data=f"picksend:{call_id}")
    kb.adjust(1)
    return kb.as_markup()


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


def support_flip_keyboard(call_id: int, current: str) -> InlineKeyboardMarkup:
    """One-tap «передумал» — flips the saved answer to the opposite one."""
    if current == "join":
        text, data = "✗ Передумал, не смогу", f"sup:skip:{call_id}"
    else:
        text, data = "✅ Передумал, приду помочь", f"sup:join:{call_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]]
    )


MENU_GROUPS = "📋 Мои группы"
MENU_TODAY = "📅 Сегодня"
MENU_STATS = "📊 Статистика"
MENU_POOL = "🆘 Саппорты"
MENU_NEWGROUP = "➕ Новая группа"
MENU_NAME = "✏️ Моё имя"
MENU_TEXTS = frozenset(
    {MENU_GROUPS, MENU_TODAY, MENU_STATS, MENU_POOL, MENU_NEWGROUP, MENU_NAME}
)


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu for the admin — instead of typing slash-commands."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_GROUPS), KeyboardButton(text=MENU_TODAY)],
            [KeyboardButton(text=MENU_STATS), KeyboardButton(text=MENU_POOL)],
            [KeyboardButton(text=MENU_NEWGROUP), KeyboardButton(text=MENU_NAME)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def student_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu for a student — only the two relevant entries."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_GROUPS), KeyboardButton(text=MENU_TODAY)],
            [KeyboardButton(text=MENU_NAME)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ── new-group wizard pickers ────────────────────────────────────────


def style_picker_keyboard(suggestions: list[str]) -> InlineKeyboardMarkup:
    """Pick a dance style: previously-used ones as quick picks + free type + skip."""
    kb = InlineKeyboardBuilder()
    for i, s in enumerate(suggestions[:6]):
        kb.button(text=s, callback_data=f"ngst:pick:{i}")
    kb.button(text="✏️ Ввести своё", callback_data="ngst:type")
    kb.button(text="⏭ Пропустить", callback_data="ngst:skip")
    kb.adjust(1)
    return kb.as_markup()


def style_typing_keyboard() -> InlineKeyboardMarkup:
    """Shown while the admin types a custom style — only a skip button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="ngst:skip")]
        ]
    )


def level_picker_keyboard(prefix: str = "nglv") -> InlineKeyboardMarkup:
    """0–4 + skip. `prefix` lets the same keyboard work for new-group and edit flows."""
    kb = InlineKeyboardBuilder()
    for n in range(5):
        kb.button(text=f"ур.{n}", callback_data=f"{prefix}:pick:{n}")
    kb.button(text="⏭ Пропустить", callback_data=f"{prefix}:skip")
    kb.adjust(5, 1)
    return kb.as_markup()


def venue_picker_keyboard(venues: list[Venue]) -> InlineKeyboardMarkup:
    """Pick a venue: previously-used + add-new + skip."""
    kb = InlineKeyboardBuilder()
    for v in venues:
        label = f"📍 {v.name}"
        if v.address:
            label += f" — {v.address}"
        if len(label) > 64:  # Telegram button text limit
            label = label[:61] + "…"
        kb.button(text=label, callback_data=f"ngvn:pick:{v.id}")
    kb.button(text="➕ Добавить новую", callback_data="ngvn:new")
    kb.button(text="⏭ Пропустить", callback_data="ngvn:skip")
    kb.adjust(1)
    return kb.as_markup()


def skip_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    """One-button keyboard for skipping an optional wizard step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data=callback_data)]
        ]
    )
