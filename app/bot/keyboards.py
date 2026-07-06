from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import ui
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


def _pack_buttons(
    buttons: list[InlineKeyboardButton], *, max_label: int = 16
) -> list[list[InlineKeyboardButton]]:
    """Пакует кнопки по 2 в ряд, НО кнопку с длинной подписью (> max_label)
    ставит на всю ширину. Так на телефоне короткие действия идут парами, а
    длинные не сжимаются в нечитаемые столбики."""
    rows: list[list[InlineKeyboardButton]] = []
    i = 0
    while i < len(buttons):
        cur = buttons[i]
        nxt = buttons[i + 1] if i + 1 < len(buttons) else None
        if len(cur.text) <= max_label and nxt is not None and len(nxt.text) <= max_label:
            rows.append([cur, nxt])
            i += 2
        else:
            rows.append([cur])
            i += 1
    return rows


def weekdays_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, name in enumerate(_WD_NAMES):
        mark = "✅ " if idx in selected else ""
        kb.button(text=f"{mark}{name}", callback_data=f"wd:{idx}")
    kb.button(text="Готово ▶", callback_data="wd:done")
    kb.adjust(4, 3, 1)
    return kb.as_markup()


def groups_keyboard(
    groups: list[Group], origin_school_id: int | None = None
) -> InlineKeyboardMarkup:
    """Список групп. `origin_school_id` — если список открыт из карточки школы:
    тогда карточка группы вернёт «◀» назад в «Группы школы», а не в «Мои группы»."""
    suffix = f":s{origin_school_id}" if origin_school_id is not None else ""
    kb = InlineKeyboardBuilder()
    for g in groups:
        prefix = "⏸ " if not g.active else ""
        label = f"{prefix}{g.name} · {g.style}" if g.style else f"{prefix}{g.name}"
        if len(label) > 64:
            label = label[:61] + "…"
        kb.button(text=label, callback_data=f"grp:{g.id}{suffix}")
    kb.adjust(1)
    return kb.as_markup()


def group_actions_keyboard(
    group_id: int,
    is_owner: bool,
    is_active: bool = True,
    *,
    has_upcoming: bool = True,
    has_closed_surveys: bool = True,
    back_cb: str = "grplist",
    back_label: str = "◀ К списку",
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
        flat.append(InlineKeyboardButton(
            text="👤 Ответственные", callback_data=f"grpadm:{group_id}",
        ))
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
    # Пакуем по 2 в ряд, но длинные подписи (>16) — на всю ширину, чтобы на
    # телефоне кнопки не сжимались и текст не переносился криво.
    rows = _pack_buttons(flat, max_label=16)
    rows.append(ui.back_row(back_cb, back_label))
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
    if len(candidates) > PAGE_SIZE:
        rows.append([InlineKeyboardButton(
            text="🔍 Поиск по имени",
            callback_data=f"search:walkin:{survey_id}:{group_id}",
        )])
    rows.append([InlineKeyboardButton(
        text="◀ Назад к ростеру", callback_data=f"atnlesson:{survey_id}:{group_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _attended_mark(attended: bool | None) -> str:
    if attended is True:
        return "✅"
    if attended is False:
        return "❌"
    return "☐"


def attendance_mark_keyboard(
    survey_id: int,
    group_id: int,
    items: list[tuple[Person, bool | None, str | None]],
    page: int = 0,
    supports: list[tuple[int, str, bool | None]] | None = None,
) -> InlineKeyboardMarkup:
    """Ростер для пост-leson отметки. items — (person, attended, promised_answer).
    Отображает: ✅/❌/☐ слева + ✓/✗/— promise в скобках. Постранично по PAGE_SIZE.
    supports — (claim_id, name, attended) согласившихся саппортов, отдельной
    секцией 🆘 (помечаем кто реально пришёл — от этого считается «помог N раз»)."""
    page_items, page, total = _page_slice(items, page)
    rows: list[list[InlineKeyboardButton]] = []
    for person, attended, promised in page_items:
        if promised == "yes":
            hint = "✓"
        elif promised == "no":
            hint = "✗"
        else:
            hint = "—"
        rows.append([InlineKeyboardButton(
            text=f"{_attended_mark(attended)} {person.full_name} ({hint})",
            callback_data=(
                f"atntog:{survey_id}:{person.id}:{_attended_code(attended)}:{page}"
            ),
        )])
    nav = _pagination_row(page, total, f"atnpg:{survey_id}:{group_id}")
    if nav:
        rows.append(nav)
    for claim_id, name, attended in supports or []:
        rows.append([InlineKeyboardButton(
            text=f"{_attended_mark(attended)} 🆘 {name}",
            callback_data=(
                f"supatt:{survey_id}:{claim_id}:{_attended_code(attended)}:{page}"
            ),
        )])
    rows.append([InlineKeyboardButton(
        text="➕ Был ещё кто-то (walk-in)",
        callback_data=f"atnwk:{survey_id}:{group_id}",
    )])
    rows.append(ui.back_row(f"grp:{group_id}", "◀ К группе"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_admins_keyboard(
    group_id: int, members: list[tuple[int, str, bool]]
) -> InlineKeyboardMarkup:
    """Назначение ответственных за группу. members — (admin_id, name, assigned).
    Тап переключает ✅/☐. Уведомления группы идут назначенным; если никто не
    отмечен — всем членам школы."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"{'✅' if assigned else '☐'} {name}",
            callback_data=f"grpadmtog:{group_id}:{admin_id}",
        )]
        for admin_id, name, assigned in members
    ]
    rows.append(ui.back_row(f"grp:{group_id}", "◀ К группе"))
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
    """Пресеты причины отмены — каждый тап исполняет отмену НЕМЕДЛЕННО (кроме
    «Другое», которая ведёт в текстовый шаг). Никакого промежуточного
    подтверждения после тапа — брошенный флоу здесь не может отменить занятие."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🤒 Болезнь препода",
                callback_data=f"grpcancelreason:{group_id}:{ts}:sick",
            )],
            [InlineKeyboardButton(
                text="🏢 Зал недоступен",
                callback_data=f"grpcancelreason:{group_id}:{ts}:venue",
            )],
            [InlineKeyboardButton(
                text="📝 Другое (текстом)",
                callback_data=f"grpcancelother:{group_id}:{ts}",
            )],
            [InlineKeyboardButton(
                text="Без причины",
                callback_data=f"grpcancelreason:{group_id}:{ts}:none",
            )],
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"grpcancel:{group_id}")],
        ]
    )


def confirm_typed_cancel_reason_keyboard(group_id: int, ts: int) -> InlineKeyboardMarkup:
    """Подтверждение отмены с ВВЕДЁННОЙ текстом причиной — без него случайное
    сообщение, присланное в течение REASON_STALE_AFTER, тихо исполнило бы
    отмену занятия с этим текстом как причиной."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🚫 Да, отменить с этой причиной",
                callback_data=f"grpcancelreasonok:{group_id}:{ts}",
            )],
            [InlineKeyboardButton(text="◀ Отмена", callback_data=f"grpcancel:{group_id}")],
        ]
    )


def school_card_keyboard(
    school_id: int, is_owner: bool, multi_school: bool = False
) -> InlineKeyboardMarkup:
    """Карточка школы. Owner видит больше (пригласить, настройки, переименовать,
    timezone, удалить); co-admin — только посмотреть состав и выйти."""
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(
        text="📋 Группы школы", callback_data=f"schgroups:{school_id}",
    )])
    # Создавать группы может любой член школы (owner и co-admin).
    rows.append([InlineKeyboardButton(
        text="➕ Новая группа", callback_data=f"schnewgroup:{school_id}",
    )])
    if is_owner:
        rows.append([InlineKeyboardButton(
            text="➕ Пригласить co-админа", callback_data=f"schlink:{school_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="👥 Состав школы", callback_data=f"schmembers:{school_id}",
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
# Москва + EU (Киев/Берлин/Мадрид/Лиссабон/Лондон/Стамбул), Закавказье
# (Тбилиси/Ереван), Средняя Азия (Алматы), США (Нью-Йорк/Лос-Анджелес).
# Свободный ввод не предлагаем — валидировать руками рискованно.
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


EVENING_TIMES = ("18:00", "19:00", "20:00", "21:00", "22:00")
TOPUP_HOURS = (0, 3, 4, 6)
# Человекочитаемые подписи режимов автодобора (B16). Порядок = порядок кнопок;
# ключи = RecruitMode значения.
RECRUIT_MODE_LABELS: dict[str, str] = {
    "curated": "🙋 Сам выбираю",
    "open_race": "🏁 Открытая гонка",
    "auto_top": "🎯 Авто топ-N",
    "escalate": "⏱ Авто-фолбэк",
}


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


def _flip_keyboard(
    current: str,
    positive_token: str,
    offer_when_positive: tuple[str, str],
    offer_when_negative: tuple[str, str],
) -> InlineKeyboardMarkup:
    """One-tap «передумал» single-button keyboard. Offers the opposite of the
    current answer: if `current == positive_token` we currently hold the positive
    answer, so the button offers the negative option, and vice versa."""
    text, data = (
        offer_when_positive if current == positive_token else offer_when_negative
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]]
    )


def flip_answer_keyboard(survey_id: int, current_answer: str) -> InlineKeyboardMarkup:
    """Shown after a student answers — one tap to flip to the opposite answer.

    `current_answer` is "yes" or "no"; the button offers the other one.
    """
    return _flip_keyboard(
        current_answer,
        "yes",
        ("❌ Передумал, не приду", f"att:{survey_id}:no"),
        ("✅ Передумал, приду", f"att:{survey_id}:yes"),
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
) -> InlineKeyboardMarkup:
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


def students_keyboard(
    group_id: int, members: list[Person], page: int = 0
) -> InlineKeyboardMarkup:
    """Group roster — each name is a button opening the person card. Постранично."""
    page_items, page, total = _page_slice(members, page)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"{_gender_glyph(p.gender)} {p.full_name}",
            callback_data=f"pcard:{group_id}:{p.id}",
        )]
        for p in page_items
    ]
    nav = _pagination_row(page, total, f"stupg:{group_id}")
    if nav:
        rows.append(nav)
    if len(members) > PAGE_SIZE:
        rows.append([InlineKeyboardButton(
            text="🔍 Поиск по имени", callback_data=f"search:students:{group_id}",
        )])
    rows.append([InlineKeyboardButton(
        text="📨 Пригласить ещё", callback_data=f"grpinv:{group_id}",
    )])
    rows.append(ui.back_row(f"grp:{group_id}", "◀ К группе"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            ui.back_row(f"grpstu:{group_id}", "◀ К списку учеников"),
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
    """"x" — явное «нет уровня»; нечисловой код (испорченный/подделанный
    callback_data) деградирует в None, как и сестринский attended_from_code,
    а не роняет хендлер ValueError'ом."""
    if code == "x":
        return None
    try:
        return int(code)
    except ValueError:
        return None


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


def pool_keyboard(
    persons: list[Person], levels: dict[int, int], page: int = 0
) -> InlineKeyboardMarkup:
    """Личный пул препода. `levels` — {person_id: уровень} ЭТОГО препода."""
    page_items, page, total = _page_slice(persons, page)
    rows: list[list[InlineKeyboardButton]] = []
    for p in page_items:
        lvl = levels.get(p.id)
        # Без пола автодобор его не позовёт (support_candidates фильтрует по
        # gender) — помечаем, чтобы препод не гадал, почему человек «молчит».
        no_gender = " ⚠️ без пола" if p.gender is None else ""
        if lvl is None:
            label = f"☐ {p.full_name}{no_gender}"
        else:
            helped = f" · помог {p.helped_count}" if p.helped_count else ""
            label = f"✅ {p.full_name} · ур.{lvl}{helped}{no_gender}"
        # state и page в callback_data: idempotency-guard + возврат на ту же
        # страницу после toggle.
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"tpool:{p.id}:{_lvl_code(lvl)}:{page}",
        )])
    nav = _pagination_row(page, total, "poolpg")
    if nav:
        rows.append(nav)
    if len(persons) > PAGE_SIZE:
        rows.append([InlineKeyboardButton(
            text="🔍 Поиск по имени", callback_data="search:pool",
        )])
    rows.append([InlineKeyboardButton(
        text="📨 Пригласить саппорта", callback_data="poolinvite",
    )])
    rows.append([InlineKeyboardButton(
        text="⚙ Настройки добора", callback_data="poolcfg",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recruit_config_keyboard(
    threshold: int, evening_time: str, topup_hours: int, recruit_mode: str
) -> InlineKeyboardMarkup:
    """Личные настройки автодобора препода. Каждая настройка — заголовок-ряд
    (self-labeling, чтобы не сверяться с текстом) + ряд вариантов под ним.
    Колбэки admin-scoped — пишут в Admin звонящего, без school_id."""
    def _header(text: str) -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton(text=text, callback_data="noop")]

    th_row = [
        InlineKeyboardButton(
            text=f"{'✅' if n == threshold else ''} {n}".strip(),
            callback_data=f"rcfgt:{n}",
        )
        for n in (1, 2, 3, 4, 5)
    ]
    eve_row = [
        InlineKeyboardButton(
            text=f"{'✅' if t == evening_time else ''} {t}".strip(),
            callback_data=f"rcfge:{t.replace(':', '')}",
        )
        for t in EVENING_TIMES
    ]
    topup_row = [
        InlineKeyboardButton(
            text=(f"{'✅' if h == topup_hours else ''} "
                  + ("выкл" if h == 0 else f"{h}ч")).strip(),
            callback_data=f"rcfgc:{h}",
        )
        for h in TOPUP_HOURS
    ]
    mode_rows = [
        [InlineKeyboardButton(
            text=f"{'✅ ' if value == recruit_mode else ''}{label}",
            callback_data=f"rcfgmode:{value}",
        )]
        for value, label in RECRUIT_MODE_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        _header(f"{ui.BALANCE} Зову, если без пары останутся ≥"),
        th_row,
        _header(f"{ui.EVENING} Вечерний разбор накануне в"),
        eve_row,
        _header(f"{ui.DAY} Добор в день занятия за"),
        topup_row,
        _header(f"{ui.SUPPORT} Режим автодобора"),
        *mode_rows,
        ui.back_row("poolback", "◀ К пулу"),
    ])


def support_pick_keyboard(
    call_id: int,
    candidates: list[Person],
    selected: set[int],
    levels: dict[int, int] | None = None,
    *,
    can_expand: bool = False,
) -> InlineKeyboardMarkup:
    """Picker добора. `levels` — {person_id: уровень} из пула lead'а (у саппов
    коллег уровня нет). `can_expand` — показать кнопку «➕ из пула коллег»."""
    levels = levels or {}
    kb = InlineKeyboardBuilder()
    for p in candidates:
        mark = "✅" if p.id in selected else "☐"
        lvl = f" · ур.{levels[p.id]}" if p.id in levels else ""
        kb.button(
            text=f"{mark} {p.full_name}{lvl}", callback_data=f"pick:{call_id}:{p.id}"
        )
    if can_expand:
        kb.button(text="➕ из пула коллег", callback_data=f"pickmore:{call_id}")
    kb.button(text=f"📨 Позвать ({len(selected)})", callback_data=f"picksend:{call_id}")
    kb.button(text="✗ Не сейчас · баланс ок", callback_data=f"picknope:{call_id}")
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
    return _flip_keyboard(
        current,
        "join",
        ("✗ Передумал, не смогу", f"sup:skip:{call_id}"),
        ("✅ Передумал, приду помочь", f"sup:join:{call_id}"),
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
    # «Новая группа» уехала в карточку школы, «Экспорт» — в /stats.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_GROUPS), KeyboardButton(text=MENU_TODAY)],
            [KeyboardButton(text=MENU_STATS), KeyboardButton(text=MENU_POOL)],
            [KeyboardButton(text=MENU_SCHOOL), KeyboardButton(text=MENU_NAME)],
            [KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def stats_keyboard(scope: str) -> InlineKeyboardMarkup:
    """Переключатель охвата статистики (школа ↔ мои группы) + экспорт CSV."""
    if scope == "school":
        toggle = InlineKeyboardButton(
            text="👥 Только мои группы", callback_data="statsscope:mine",
        )
    else:
        toggle = InlineKeyboardButton(
            text="🏫 По всей школе", callback_data="statsscope:school",
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="statsexport")],
    ])


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
