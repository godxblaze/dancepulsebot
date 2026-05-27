from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Admin, Gender, Group, Person, School, SchoolMemberRole, Venue

_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

PAGE_SIZE = 10


def _page_slice(items: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, int, int]:
    """(slice, page, total_pages). Кламп: если page вне границ — двигаем внутрь."""
    total = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total - 1))
    start = page * page_size
    return items[start:start + page_size], page, total


def _pagination_row(
    page: int, total_pages: int, cb_prefix: str
) -> list[InlineKeyboardButton]:
    """Один ряд `◀ N/M ▶`. cb_prefix получает суффикс ':<page>'.
    Возвращает пустой list если страница одна (пагинация не нужна)."""
    if total_pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"{cb_prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(
        text=f"{page + 1}/{total_pages}", callback_data="noop",
    ))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"{cb_prefix}:{page + 1}"))
    return row


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
        prefix = "⏸ " if not g.active else ""
        label = f"{prefix}{g.name} · {g.style}" if g.style else f"{prefix}{g.name}"
        if len(label) > 64:
            label = label[:61] + "…"
        kb.button(text=label, callback_data=f"grp:{g.id}")
    kb.adjust(1)
    return kb.as_markup()


def group_actions_keyboard(
    group_id: int,
    is_owner: bool,
    is_active: bool = True,
    *,
    has_upcoming: bool = True,
    has_closed_surveys: bool = True,
) -> InlineKeyboardMarkup:
    """Кнопки в карточке группы. Кнопки группируются в 2 колонки чтобы карточка
    влезала в один экран. Owner видит больше (изменить, пауза, удалить);
    co-admin — только что доступно. Мёртвые кнопки скрываются."""
    # Собираем плоский список, потом пакуем парами.
    flat: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="👥 Ученики", callback_data=f"grpstu:{group_id}"),
        InlineKeyboardButton(text="📨 Пригласить", callback_data=f"grpinv:{group_id}"),
        InlineKeyboardButton(text="📢 Сообщение", callback_data=f"grpbcast:{group_id}"),
    ]
    if is_owner:
        flat.append(
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"grpedit:{group_id}")
        )
    if has_upcoming:
        flat.append(InlineKeyboardButton(
            text="🚫 Отменить занятие", callback_data=f"grpcancel:{group_id}",
        ))
    if has_closed_surveys:
        flat.append(InlineKeyboardButton(
            text="✏️ Отметить посещаемость", callback_data=f"grpattnmark:{group_id}",
        ))
    if is_owner:
        if is_active:
            flat.append(InlineKeyboardButton(
                text="⏸ Пауза", callback_data=f"grppause:{group_id}",
            ))
        else:
            flat.append(InlineKeyboardButton(
                text="▶️ Возобновить", callback_data=f"grpresume:{group_id}",
            ))
        flat.append(InlineKeyboardButton(
            text="🗑 Удалить", callback_data=f"grpdel:{group_id}",
        ))
    # Пакуем по 2 в ряд.
    rows: list[list[InlineKeyboardButton]] = [
        flat[i:i + 2] for i in range(0, len(flat), 2)
    ]
    rows.append([InlineKeyboardButton(text="◀ К списку", callback_data="grplist")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def walk_in_picker_keyboard(
    survey_id: int, group_id: int, candidates: list[Person], page: int = 0,
) -> InlineKeyboardMarkup:
    """Список персон, которых ещё нет в ростере опроса — выбрать «walk-in».
    Постранично по PAGE_SIZE."""
    page_items, page, total = _page_slice(candidates, page)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=p.full_name, callback_data=f"atnwkpick:{survey_id}:{p.id}")]
        for p in page_items
    ]
    nav = _pagination_row(page, total, f"wkpg:{survey_id}:{group_id}")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text="◀ Назад к ростеру", callback_data=f"atnlesson:{survey_id}:{group_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def attendance_mark_keyboard(
    survey_id: int,
    group_id: int,
    items: list[tuple[Person, bool | None, str | None]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Ростер для пост-leson отметки. items — (person, attended, promised_answer).
    Отображает: ✅/❌/☐ слева + ✓/✗/— promise в скобках. Постранично по PAGE_SIZE."""
    page_items, page, total = _page_slice(items, page)
    rows: list[list[InlineKeyboardButton]] = []
    for person, attended, promised in page_items:
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
        rows.append([InlineKeyboardButton(
            text=f"{mark} {person.full_name} ({hint})",
            callback_data=(
                f"atntog:{survey_id}:{person.id}:{_attended_code(attended)}:{page}"
            ),
        )])
    nav = _pagination_row(page, total, f"atnpg:{survey_id}:{group_id}")
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text="➕ Был ещё кто-то (walk-in)",
        callback_data=f"atnwk:{survey_id}:{group_id}",
    )])
    rows.append([InlineKeyboardButton(text="◀ К группе", callback_data=f"grp:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def school_card_keyboard(
    school_id: int, is_owner: bool, multi_school: bool = False
) -> InlineKeyboardMarkup:
    """Карточка школы. Owner видит больше (пригласить, настройки, переименовать,
    timezone, удалить); co-admin — только посмотреть состав и выйти."""
    rows: list[list[InlineKeyboardButton]] = []
    if is_owner:
        rows.append([InlineKeyboardButton(
            text="➕ Пригласить co-админа", callback_data=f"schlink:{school_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="⚙ Настройки автодобора", callback_data=f"schcfg:{school_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="🌐 Часовой пояс", callback_data=f"schtz:{school_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="✏️ Переименовать школу", callback_data=f"schren:{school_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="🗑 Удалить школу", callback_data=f"schdel:{school_id}",
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="↩ Выйти из школы", callback_data=f"schleave:{school_id}",
        )])
    if multi_school:
        rows.append([InlineKeyboardButton(
            text="◀ К списку школ", callback_data="schlist",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Фиксированный список IANA таймзон в picker'е. 12 наиболее популярных:
# Москва + EU (LON/MAD/BER/LIS/PAR/KYIV/IST), США (NY/LA), Латам/JP,
# Россия-Сибирь (Yekaterinburg, Novosibirsk). Свободный ввод не предлагаем —
# валидировать руками рискованно.
TZ_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Europe/Moscow", "🇷🇺 Москва (UTC+3)"),
    ("Europe/Kyiv", "🇺🇦 Киев (UTC+2/+3)"),
    ("Europe/Berlin", "🇩🇪 Берлин (UTC+1/+2)"),
    ("Europe/Madrid", "🇪🇸 Мадрид (UTC+1/+2)"),
    ("Europe/Lisbon", "🇵🇹 Лиссабон (UTC+0/+1)"),
    ("Europe/London", "🇬🇧 Лондон (UTC+0/+1)"),
    ("Europe/Istanbul", "🇹🇷 Стамбул (UTC+3)"),
    ("Asia/Tbilisi", "🇬🇪 Тбилиси (UTC+4)"),
    ("Asia/Yerevan", "🇦🇲 Ереван (UTC+4)"),
    ("Asia/Almaty", "🇰🇿 Алматы (UTC+5/+6)"),
    ("America/New_York", "🇺🇸 Нью-Йорк (UTC-5/-4)"),
    ("America/Los_Angeles", "🇺🇸 Лос-Анджелес (UTC-8/-7)"),
)


def school_tz_keyboard(school_id: int, current: str) -> InlineKeyboardMarkup:
    """Picker таймзоны школы. Текущая помечена ✅."""
    rows: list[list[InlineKeyboardButton]] = []
    for tz_name, label in TZ_OPTIONS:
        mark = "✅ " if tz_name == current else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"schtzset:{school_id}:{tz_name}",
        )])
    rows.append([InlineKeyboardButton(
        text="◀ К школе", callback_data=f"schcard:{school_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def school_config_keyboard(
    school_id: int, threshold: int, cutoff_hours: int
) -> InlineKeyboardMarkup:
    """Двухстрочный picker: «перекос» (1..5) и «за сколько часов проверяем» (2..24).
    Текущее значение помечено ✅."""
    th_row = [
        InlineKeyboardButton(
            text=f"{'✅' if n == threshold else ''} {n}",
            callback_data=f"schcfgt:{school_id}:{n}",
        )
        for n in (1, 2, 3, 4, 5)
    ]
    cut_row = [
        InlineKeyboardButton(
            text=f"{'✅' if h == cutoff_hours else ''} {h}ч",
            callback_data=f"schcfgc:{school_id}:{h}",
        )
        for h in (2, 4, 6, 8, 12, 24)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        th_row,
        cut_row,
        [InlineKeyboardButton(text="◀ К школе", callback_data=f"schcard:{school_id}")],
    ])


def school_members_keyboard(
    school_id: int, members: list[tuple[Admin, SchoolMemberRole]], is_owner: bool
) -> InlineKeyboardMarkup:
    """Inline-список member'ов школы. Если ты owner — у coadmin-строк появляется кнопка убрать."""
    rows: list[list[InlineKeyboardButton]] = []
    for admin, role in members:
        prefix = "👑" if role == SchoolMemberRole.owner else "🛠"
        label = f"{prefix} {admin.full_name}"
        if is_owner and role == SchoolMemberRole.coadmin:
            rows.append([InlineKeyboardButton(
                text=f"🗑 Убрать {admin.full_name}",
                callback_data=f"schrem:{school_id}:{admin.id}",
            )])
        else:
            # Информационная строка — callback no-op
            rows.append([InlineKeyboardButton(text=label, callback_data="noop")])
    rows.append([InlineKeyboardButton(
        text="◀ К карточке школы", callback_data=f"schcard:{school_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def school_picker_keyboard(
    schools: list[tuple[School, SchoolMemberRole]]
) -> InlineKeyboardMarkup:
    """Если у admin'а > 1 школы — выбор школы для дальнейшего действия."""
    kb = InlineKeyboardBuilder()
    for school, role in schools:
        glyph = "👑" if role == SchoolMemberRole.owner else "🛠"
        kb.button(text=f"{glyph} {school.name}", callback_data=f"schcard:{school.id}")
    kb.adjust(1)
    return kb.as_markup()


def school_accept_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"schaccept:{code}")],
            [InlineKeyboardButton(text="✗ Не сейчас", callback_data="schdecline")],
        ]
    )


def confirm_school_delete_keyboard(school_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"schcard:{school_id}")],
            [InlineKeyboardButton(
                text="🗑 Да, удалить школу со всеми группами",
                callback_data=f"schdelok:{school_id}",
            )],
        ]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Preview-confirm перед рассылкой в группу. Отменить и отправить — на разных рядах."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data="bcast:cancel")],
            [InlineKeyboardButton(text="📨 Отправить", callback_data="bcast:send")],
        ]
    )


def confirm_school_leave_keyboard(school_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"schcard:{school_id}")],
            [InlineKeyboardButton(
                text="↩ Да, выйти из школы",
                callback_data=f"schleaveok:{school_id}",
            )],
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
    pending_by_group: list[tuple[str, int, int]],
    span: str = "day",
) -> InlineKeyboardMarkup | None:
    """For /today summary — ping per group + переключатель span (day/week).
    span: 'day' → отображается сегодня+завтра, 'week' → ближайшие 7 дней."""
    has_pings = bool(pending_by_group)
    kb = InlineKeyboardBuilder()
    for name, survey_id, pending in pending_by_group:
        kb.button(text=f"📨 Пинг «{name}» ({pending})", callback_data=f"ping:{survey_id}")
    # Переключатель режима — показывает текущий выбор checked'ом.
    if span == "week":
        kb.button(text="📅 Сегодня/завтра", callback_data="today:day")
    else:
        kb.button(text="📅 На неделю", callback_data="today:week")
    # has_pings влияет только на adjust: 1 кнопка в ряд если есть пинги,
    # иначе — переключатель один внизу.
    if has_pings:
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
    group_id: int,
    person_id: int,
    support_level: int | None,
    gender: Gender | None,
    in_group: bool = True,
) -> InlineKeyboardMarkup:
    """Actions on a person inside a group: pool level, gender, membership, back.

    The pool-level button shows what tap will do next (cycles 1→2→3→null).
    Gender-toggle button лечит ошибки онбординга (выбрал не тот пол).
    `in_group=False` означает что карточка открыта для walk-in гостя — кнопка
    «Убрать» меняется на «Записать постоянно».
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
    if in_group:
        membership_btn = InlineKeyboardButton(
            text="🚪 Убрать из группы",
            callback_data=f"pcardrem:{group_id}:{person_id}",
        )
    else:
        membership_btn = InlineKeyboardButton(
            text="➕ Записать постоянно",
            callback_data=f"pcardadd:{group_id}:{person_id}",
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=level_text,
                callback_data=f"pcardlvl:{group_id}:{person_id}:{_lvl_code(support_level)}",
            )],
            [InlineKeyboardButton(
                text=gender_text,
                callback_data=f"pcardgnd:{group_id}:{person_id}:{_gender_code(gender)}",
            )],
            [membership_btn],
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


# Idempotency-кодировки и парсеры — общая пара encode/decode для одного value.
# Используются в keyboards (encode → callback_data) и в handlers (decode при receive).


def _lvl_code(lvl: int | None) -> str:
    """Encode для tpool / pcardlvl: 'x' = None, иначе цифра."""
    return "x" if lvl is None else str(lvl)


def lvl_from_code(code: str) -> int | None:
    return None if code == "x" else int(code)


def _gender_code(g: Gender | None) -> str:
    if g == Gender.male:
        return "m"
    if g == Gender.female:
        return "f"
    return "x"


def gender_from_code(code: str) -> Gender | None:
    if code == "m":
        return Gender.male
    if code == "f":
        return Gender.female
    return None


def _attended_code(a: bool | None) -> str:
    if a is True:
        return "y"
    if a is False:
        return "n"
    return "x"


def attended_from_code(code: str) -> bool | None:
    if code == "y":
        return True
    if code == "n":
        return False
    return None


def pool_keyboard(persons: list[Person], page: int = 0) -> InlineKeyboardMarkup:
    page_items, page, total = _page_slice(persons, page)
    rows: list[list[InlineKeyboardButton]] = []
    for p in page_items:
        if p.support_level is None:
            label = f"☐ {p.full_name}"
        else:
            helped = f" · помог {p.helped_count}" if p.helped_count else ""
            label = f"✅ {p.full_name} · ур.{p.support_level}{helped}"
        # state и page в callback_data: idempotency-guard + возврат на ту же
        # страницу после toggle.
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"tpool:{p.id}:{_lvl_code(p.support_level)}:{page}",
        )])
    nav = _pagination_row(page, total, "poolpg")
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
MENU_SCHOOL = "🏫 Школа"
MENU_EXPORT = "📥 Экспорт"
MENU_HELP = "❓ Помощь"
MENU_NAME = "✏️ Моё имя"
MENU_TEXTS = frozenset({
    MENU_GROUPS, MENU_TODAY, MENU_STATS, MENU_POOL,
    MENU_NEWGROUP, MENU_SCHOOL, MENU_EXPORT, MENU_HELP, MENU_NAME,
})


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu for the admin — instead of typing slash-commands."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_GROUPS), KeyboardButton(text=MENU_TODAY)],
            [KeyboardButton(text=MENU_STATS), KeyboardButton(text=MENU_POOL)],
            [KeyboardButton(text=MENU_NEWGROUP), KeyboardButton(text=MENU_SCHOOL)],
            [KeyboardButton(text=MENU_EXPORT), KeyboardButton(text=MENU_NAME)],
            [KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def onboarding_menu_keyboard() -> ReplyKeyboardMarkup:
    """Минимальное меню для нового юзера до записи в группу: единственное
    действие — «Помощь». Не молчим, юзер сразу видит что бот живой."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_HELP)]],
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
