"""NewGroup wizard на aiogram-dialog.

Поведение совместимо с прежним FSM-мастером:
  style → (style_typing) → level → weekdays → lesson_time
  → venue → (venue_new_name → venue_new_address) → room → teachers → create.

После создания группы сразу отправляется invite-QR одним сообщением.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Back, Button, Multiselect, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from app.bot.dialogs.states import NewGroupSG, user_from
from app.bot.utils import format_group_title, get_bot_username, qr_png
from app.core.config import settings
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from app.services import venues as venues_svc

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── getters ──────────────────────────────────────────────────────────


async def _style_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    """Style-подсказки из собственной школы admin'а."""
    user = user_from(dialog_manager)
    suggestions: list[str] = []
    if user is not None:
        async with session_scope() as s:
            admin = await admins_svc.get_by_tg_id(s, user.id)
            if admin is not None:
                school = await schools_svc.get_owned_school(s, admin.id)
                if school is not None:
                    suggestions = await groups_svc.school_styles(s, school.id)
    return {"styles": list(enumerate(suggestions[:6]))}


async def _weekdays_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    return {"days": [(idx, name) for idx, name in enumerate(_WD_NAMES)]}


async def _venue_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    user = user_from(dialog_manager)
    venues: list = []
    if user is not None:
        async with session_scope() as s:
            admin = await admins_svc.get_by_tg_id(s, user.id)
            if admin is not None:
                school = await schools_svc.get_owned_school(s, admin.id)
                if school is not None:
                    venues = await venues_svc.school_venues(s, school.id)
    items: list[tuple[int, str]] = []
    for v in venues:
        label = f"📍 {v.name}"
        if v.address:
            label += f" — {v.address}"
        if len(label) > 64:
            label = label[:61] + "…"
        items.append((v.id, label))
    return {"venues": items}


# ── on_click / on_input ──────────────────────────────────────────────


async def _on_style_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    user = user_from(manager)
    suggestions: list[str] = []
    if user is not None:
        async with session_scope() as s:
            admin = await admins_svc.get_by_tg_id(s, user.id)
            if admin is not None:
                school = await schools_svc.get_owned_school(s, admin.id)
                if school is not None:
                    suggestions = await groups_svc.school_styles(s, school.id)
    try:
        idx = int(item_id)
    except ValueError:
        return
    if 0 <= idx < len(suggestions):
        manager.dialog_data["style"] = suggestions[idx]
        await manager.switch_to(NewGroupSG.level)


async def _on_style_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["style"] = None
    await manager.switch_to(NewGroupSG.level)


async def _on_style_type(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await manager.switch_to(NewGroupSG.style_typing)


async def _on_style_typed(
    _message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    style = value.strip()
    if not style:
        return  # MessageInput сам не продвинется — попросим ещё раз через UI
    manager.dialog_data["style"] = style
    await manager.switch_to(NewGroupSG.level)


async def _on_level_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        level = int(item_id)
    except ValueError:
        return
    if 0 <= level <= 4:
        manager.dialog_data["level"] = level
        await manager.switch_to(NewGroupSG.weekdays)


async def _on_level_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["level"] = None
    await manager.switch_to(NewGroupSG.weekdays)


async def _on_weekdays_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    ms = manager.find("wd_select")
    selected = sorted(int(i) for i in ms.get_checked()) if ms else []
    if not selected:
        await call.answer("Выбери хотя бы один день", show_alert=True)
        return
    manager.dialog_data["weekdays"] = selected
    await manager.switch_to(NewGroupSG.lesson_time)


async def _on_time_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    raw = value.strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Формат ЧЧ:ММ, например 19:00")
        return
    manager.dialog_data["lesson_time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    await manager.switch_to(NewGroupSG.venue)


async def _on_venue_pick(
    call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        venue_id = int(item_id)
    except ValueError:
        return
    user = user_from(manager)
    if user is None:
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        venue = await venues_svc.get_venue(s, venue_id)
        owned = await schools_svc.get_owned_school(s, admin.id) if admin else None
        if admin is None or venue is None or owned is None or venue.school_id != owned.id:
            await call.answer()
            return
    manager.dialog_data["venue_id"] = venue_id
    await manager.switch_to(NewGroupSG.room)


async def _on_venue_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["venue_id"] = None
    await manager.switch_to(NewGroupSG.room)


async def _on_venue_new(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await manager.switch_to(NewGroupSG.venue_new_name)


async def _on_venue_new_name_typed(
    _message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    name = value.strip()
    if not name:
        return
    manager.dialog_data["venue_new_name"] = name
    await manager.switch_to(NewGroupSG.venue_new_address)


async def _save_new_venue(manager: DialogManager, address: str | None) -> None:
    user = user_from(manager)
    if user is None:
        return
    name = manager.dialog_data.get("venue_new_name")
    if not name:
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None:
            return
        school = await schools_svc.get_or_create_owned_school(s, admin.id)
        venue = await venues_svc.create_venue(s, school.id, name, address)
        manager.dialog_data["venue_id"] = venue.id


async def _on_venue_new_addr_typed(
    _message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    address = value.strip() or None
    await _save_new_venue(manager, address)
    await manager.switch_to(NewGroupSG.room)


async def _on_venue_new_addr_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _save_new_venue(manager, None)
    await manager.switch_to(NewGroupSG.room)


async def _on_room_typed(
    _message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    manager.dialog_data["room"] = value.strip() or None
    await manager.switch_to(NewGroupSG.teachers)


async def _on_room_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["room"] = None
    await manager.switch_to(NewGroupSG.teachers)


async def _on_teachers_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    manager.dialog_data["teachers"] = value.strip() or None
    await _finish_new_group(message, manager)


async def _on_teachers_skip(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["teachers"] = None
    # Финал — отправляем результат как новое сообщение в чат.
    if call.message:
        await _finish_new_group(call.message, manager)


async def _finish_new_group(target: Message, manager: DialogManager) -> None:
    """Создаёт группу из накопленных dialog_data и шлёт invite-QR."""
    from app.bot.utils import derive_group_name
    from app.services.schedule import format_weekdays

    data = manager.dialog_data
    user = user_from(manager)
    if user is None:
        await manager.done()
        return
    weekdays = ",".join(str(d) for d in data.get("weekdays", []))
    style = data.get("style")
    level = data.get("level")
    venue_id = data.get("venue_id")
    room = data.get("room")
    teachers_note = data.get("teachers")
    if not weekdays or not data.get("lesson_time"):
        await target.answer("Не хватает данных для создания группы — отменяю.")
        await manager.done()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None:
            await manager.done()
            return
        school = await schools_svc.get_or_create_owned_school(s, admin.id)
        group = await groups_svc.create_group(
            s,
            school.id,
            derive_group_name(style, level),
            weekdays,
            data["lesson_time"],
            settings.default_lead_hours,
            style=style,
            level=level,
            venue_id=venue_id,
            room=room,
            teachers_note=teachers_note,
        )
        code = group.join_code
        title = format_group_title(group)
        # Сводка прямо здесь — без cross-import'а из admin.py.
        lines = [f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}"]
        if group.style:
            lines.append(f"Направление: {group.style}")
        if group.level is not None:
            lines.append(f"Уровень: {group.level}")
        if group.venue_id is not None:
            venue = await venues_svc.get_venue(s, group.venue_id)
            if venue is not None:
                place = venue.name
                if venue.address:
                    place += f" — {venue.address}"
                if group.room:
                    place += f", {group.room}"
                lines.append(f"Где: {place}")
        elif group.room:
            lines.append(f"Зал: {group.room}")
        if group.teachers_note:
            lines.append(f"Ведут: {group.teachers_note}")
        summary = "\n".join(lines)

    bot = manager.middleware_data.get("bot")
    if bot is None:
        log.warning("new_group: bot missing from middleware_data")
        await manager.done()
        return
    username = await get_bot_username(bot)
    link = f"https://t.me/{username}?start=join_{code}"
    await target.answer(f"✅ Группа «{title}» создана.\n{summary}")
    await target.answer_photo(
        BufferedInputFile(qr_png(link), filename="qr.png"),
        caption=(
            f"📨 Приглашение в «{title}»\n\n"
            f"Перешли это сообщение ученикам или покажи QR офлайн.\n\n"
            f"Ссылка: {link}"
        ),
    )
    await manager.done()


# ── Dialog ────────────────────────────────────────────────────────────


new_group_dialog = Dialog(
    Window(
        Const("Какое направление? (например: Бачата, Сальса, Хип-хоп)"),
        Select(
            Format("{item[1]}"),
            id="style_select",
            item_id_getter=lambda item: item[0],
            items="styles",
            on_click=_on_style_pick,
        ),
        Button(Const("✏️ Ввести своё"), id="style_type", on_click=_on_style_type),
        Button(Const("⏭ Пропустить"), id="style_skip", on_click=_on_style_skip),
        getter=_style_getter,
        state=NewGroupSG.style,
    ),
    Window(
        Const("Напиши направление одной строкой:"),
        TextInput(id="style_input", on_success=_on_style_typed),
        SwitchTo(Const("⏭ Пропустить"), id="style_typing_skip", state=NewGroupSG.level),
        Back(Const("◀ Назад")),
        state=NewGroupSG.style_typing,
    ),
    Window(
        Const("Какой уровень? (0–4 от новичков до продвинутых)"),
        Select(
            Format("ур.{item}"),
            id="lvl_select",
            item_id_getter=str,
            items=[0, 1, 2, 3, 4],
            on_click=_on_level_pick,
        ),
        Button(Const("⏭ Пропустить"), id="lvl_skip", on_click=_on_level_skip),
        SwitchTo(Const("◀ Назад"), id="lvl_back", state=NewGroupSG.style),
        state=NewGroupSG.level,
    ),
    Window(
        Const("В какие дни занятия? Отметь дни и нажми «Готово»."),
        Multiselect(
            Format("✅ {item[1]}"),
            Format("{item[1]}"),
            id="wd_select",
            item_id_getter=lambda item: str(item[0]),
            items="days",
        ),
        Button(Const("Готово ▶"), id="wd_done", on_click=_on_weekdays_done),
        SwitchTo(Const("◀ Назад"), id="wd_back", state=NewGroupSG.level),
        getter=_weekdays_getter,
        state=NewGroupSG.weekdays,
    ),
    Window(
        Const("Во сколько начинается занятие? Формат ЧЧ:ММ, например 19:00"),
        TextInput(id="time_input", on_success=_on_time_typed),
        SwitchTo(Const("◀ Назад"), id="time_back", state=NewGroupSG.weekdays),
        state=NewGroupSG.lesson_time,
    ),
    Window(
        Const("Где проходят занятия?"),
        Select(
            Format("{item[1]}"),
            id="venue_select",
            item_id_getter=lambda item: str(item[0]),
            items="venues",
            on_click=_on_venue_pick,
        ),
        Button(Const("➕ Добавить новую"), id="venue_new", on_click=_on_venue_new),
        Button(Const("⏭ Пропустить"), id="venue_skip", on_click=_on_venue_skip),
        SwitchTo(Const("◀ Назад"), id="venue_back", state=NewGroupSG.lesson_time),
        getter=_venue_getter,
        state=NewGroupSG.venue,
    ),
    Window(
        Const(
            "Название площадки одной строкой "
            "(например: «Шлюзовая 8с2» или «Студия Полевик»):"
        ),
        TextInput(id="venue_new_name_input", on_success=_on_venue_new_name_typed),
        SwitchTo(Const("⏭ Отмена"), id="venue_new_cancel", state=NewGroupSG.venue),
        state=NewGroupSG.venue_new_name,
    ),
    Window(
        Const("Адрес (если хочешь). Можно пропустить:"),
        TextInput(id="venue_new_addr_input", on_success=_on_venue_new_addr_typed),
        Button(
            Const("⏭ Пропустить"),
            id="venue_new_addr_skip",
            on_click=_on_venue_new_addr_skip,
        ),
        state=NewGroupSG.venue_new_address,
    ),
    Window(
        Const(
            "В каком зале проходит занятие? (например: «зал 38»). Можно пропустить:"
        ),
        TextInput(id="room_input", on_success=_on_room_typed),
        Button(Const("⏭ Пропустить"), id="room_skip", on_click=_on_room_skip),
        SwitchTo(Const("◀ Назад"), id="room_back", state=NewGroupSG.venue),
        state=NewGroupSG.room,
    ),
    Window(
        Const(
            "Кто ведёт? Свободной строкой — например, «Маша + Петя». Можно пропустить:"
        ),
        TextInput(id="teachers_input", on_success=_on_teachers_typed),
        Button(Const("⏭ Пропустить"), id="teachers_skip", on_click=_on_teachers_skip),
        SwitchTo(Const("◀ Назад"), id="teachers_back", state=NewGroupSG.room),
        state=NewGroupSG.teachers,
    ),
)


__all__ = ["NewGroupSG", "new_group_dialog"]
