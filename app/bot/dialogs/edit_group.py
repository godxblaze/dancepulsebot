"""EditGroup wizard на aiogram-dialog.

Editing того же набора полей что NewGroup, но как «меню + sub-окна»: одно
главное окно с кнопкой по каждому полю. После сохранения возвращаемся в menu.
group_id передаётся через `dialog_manager.start(EditGroupSG.menu, data=...)`.
"""
from __future__ import annotations

import logging
import re
from html import escape
from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Multiselect, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from app.bot.dialogs.states import EditGroupSG, user_from
from app.bot.utils import derive_group_name, format_group_title
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from app.services import venues as venues_svc
from app.services.schedule import format_weekdays

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── shared helpers ───────────────────────────────────────────────────


def _gid(manager: DialogManager) -> int:
    """group_id, переданный при `start(..., data={'group_id': gid})`. start_data
    живёт пока диалог запущен; дублируется в dialog_data на первом open."""
    gid = manager.dialog_data.get("group_id")
    if gid is None:
        start = manager.start_data
        if isinstance(start, dict):
            gid = start.get("group_id")
        if gid is not None:
            manager.dialog_data["group_id"] = int(gid)
    return int(gid) if gid is not None else 0


async def _owner_check(manager: DialogManager) -> bool:
    """True если открывший диалог — owner школы редактируемой группы."""
    user = user_from(manager)
    if user is None:
        return False
    group_id = _gid(manager)
    if not group_id:
        return False
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        group = await groups_svc.get_group(s, group_id)
        if admin is None or group is None or group.school_id is None:
            return False
        return await schools_svc.is_owner(s, group.school_id, admin.id)


async def _patch_group(manager: DialogManager, **fields: Any) -> None:
    """Обновить поля группы. Перевычислить name если меняется style/level."""
    if not await _owner_check(manager):
        return
    group_id = _gid(manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        await groups_svc.update_group(s, group, **fields)
        if "style" in fields or "level" in fields:
            await groups_svc.update_group(
                s, group, name=derive_group_name(group.style, group.level)
            )


# ── menu ──────────────────────────────────────────────────────────────


async def _menu_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    group_id = _gid(dialog_manager)
    title = "—"
    summary = ""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None:
            title = format_group_title(group)
            lines = [
                f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}"
            ]
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
    return {"title": escape(title), "summary": escape(summary)}


# ── style ─────────────────────────────────────────────────────────────


async def _style_getter(dialog_manager: DialogManager, **_: Any) -> dict:
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


async def _on_style_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        idx = int(item_id)
    except ValueError:
        return
    user = user_from(manager)
    if user is None:
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        school = await schools_svc.get_owned_school(s, admin.id) if admin else None
        suggestions = await groups_svc.school_styles(s, school.id) if school else []
    if 0 <= idx < len(suggestions):
        await _patch_group(manager, style=suggestions[idx])
        await manager.switch_to(EditGroupSG.menu)


async def _on_style_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, style=None)
    await manager.switch_to(EditGroupSG.menu)


async def _on_style_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    style = value.strip()
    if not style:
        return
    await _patch_group(manager, style=style)
    await manager.switch_to(EditGroupSG.menu)


# ── level ─────────────────────────────────────────────────────────────


async def _on_level_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        level = int(item_id)
    except ValueError:
        return
    if 0 <= level <= 4:
        await _patch_group(manager, level=level)
        await manager.switch_to(EditGroupSG.menu)


async def _on_level_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, level=None)
    await manager.switch_to(EditGroupSG.menu)


# ── weekdays ──────────────────────────────────────────────────────────


async def _weekdays_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    group_id = _gid(dialog_manager)
    current: set[int] = set()
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None and group.weekdays:
            current = {int(x) for x in group.weekdays.split(",") if x}
    # При первом открытии — записываем текущее состояние в виджет
    if "weekdays_initialized" not in dialog_manager.dialog_data:
        ms = dialog_manager.find("wd_select")
        if ms is not None:
            for idx in current:
                await ms.set_checked(str(idx), True)
        dialog_manager.dialog_data["weekdays_initialized"] = True
    return {"days": [(idx, name) for idx, name in enumerate(_WD_NAMES)]}


async def _on_weekdays_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    ms = manager.find("wd_select")
    selected = sorted(int(i) for i in ms.get_checked()) if ms else []
    if not selected:
        await call.answer("Выбери хотя бы один день", show_alert=True)
        return
    await _patch_group(manager, weekdays=",".join(str(d) for d in selected))
    manager.dialog_data.pop("weekdays_initialized", None)
    await manager.switch_to(EditGroupSG.menu)


# ── lesson_time ───────────────────────────────────────────────────────


async def _on_time_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    raw = value.strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Формат ЧЧ:ММ, например 19:00")
        return
    await _patch_group(manager, lesson_time=f"{int(m.group(1)):02d}:{m.group(2)}")
    await manager.switch_to(EditGroupSG.menu)


# ── venue ─────────────────────────────────────────────────────────────


async def _venue_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    user = user_from(dialog_manager)
    venues_list: list = []
    if user is not None:
        async with session_scope() as s:
            admin = await admins_svc.get_by_tg_id(s, user.id)
            if admin is not None:
                school = await schools_svc.get_owned_school(s, admin.id)
                if school is not None:
                    venues_list = await venues_svc.school_venues(s, school.id)
    items: list[tuple[int, str]] = []
    for v in venues_list:
        label = f"📍 {v.name}"
        if v.address:
            label += f" — {v.address}"
        if len(label) > 64:
            label = label[:61] + "…"
        items.append((v.id, label))
    return {"venues": items}


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
    group_id = _gid(manager)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        venue = await venues_svc.get_venue(s, venue_id)
        group = await groups_svc.get_group(s, group_id)
        if admin is None or venue is None or group is None or venue.school_id != group.school_id:
            await call.answer()
            return
    await _patch_group(manager, venue_id=venue_id)
    await manager.switch_to(EditGroupSG.menu)


async def _on_venue_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, venue_id=None)
    await manager.switch_to(EditGroupSG.menu)


async def _on_venue_new_name_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    name = value.strip()
    if not name:
        return
    manager.dialog_data["venue_new_name"] = name
    await manager.switch_to(EditGroupSG.venue_new_address)


async def _save_new_venue_and_attach(manager: DialogManager, address: str | None) -> None:
    user = user_from(manager)
    name = manager.dialog_data.get("venue_new_name")
    if user is None or not name:
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None:
            return
        school = await schools_svc.get_or_create_owned_school(s, admin.id)
        venue = await venues_svc.create_venue(s, school.id, name, address)
        venue_id = venue.id
    await _patch_group(manager, venue_id=venue_id)


async def _on_venue_new_addr_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    await _save_new_venue_and_attach(manager, value.strip() or None)
    await manager.switch_to(EditGroupSG.menu)


async def _on_venue_new_addr_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _save_new_venue_and_attach(manager, None)
    await manager.switch_to(EditGroupSG.menu)


# ── room ──────────────────────────────────────────────────────────────


async def _on_room_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    await _patch_group(manager, room=value.strip() or None)
    await manager.switch_to(EditGroupSG.menu)


async def _on_room_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, room=None)
    await manager.switch_to(EditGroupSG.menu)


# ── teachers ──────────────────────────────────────────────────────────


async def _on_teachers_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    await _patch_group(manager, teachers_note=value.strip() or None)
    await manager.switch_to(EditGroupSG.menu)


async def _on_teachers_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, teachers_note=None)
    await manager.switch_to(EditGroupSG.menu)


# ── done ──────────────────────────────────────────────────────────────


async def _on_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    """Закрыть dialog. Не отрисовываем карточку — пользователь увидит её через
    /groups; диалог сам пишет «Сохранил» каждый раз."""
    await manager.done()
    if call.message:
        await call.message.answer("✅ Готово. Открой /groups чтобы увидеть карточку.")


# ── Dialog ────────────────────────────────────────────────────────────


edit_group_dialog = Dialog(
    Window(
        Format("✏️ «{title}»\n{summary}\n\nЧто меняем?"),
        SwitchTo(Const("💃 Направление"), id="m_style", state=EditGroupSG.style),
        SwitchTo(Const("🎯 Уровень"), id="m_level", state=EditGroupSG.level),
        SwitchTo(Const("📅 Дни недели"), id="m_days", state=EditGroupSG.weekdays),
        SwitchTo(Const("🕐 Время"), id="m_time", state=EditGroupSG.lesson_time),
        SwitchTo(Const("📍 Место"), id="m_venue", state=EditGroupSG.venue),
        SwitchTo(Const("🚪 Зал"), id="m_room", state=EditGroupSG.room),
        SwitchTo(Const("👤 Преподаватели"), id="m_teachers", state=EditGroupSG.teachers),
        Button(Const("◀ К группе"), id="done", on_click=_on_done),
        getter=_menu_getter,
        state=EditGroupSG.menu,
    ),
    Window(
        Const("Новое направление?"),
        Select(
            Format("{item[1]}"),
            id="es_style_select",
            item_id_getter=lambda item: item[0],
            items="styles",
            on_click=_on_style_pick,
        ),
        SwitchTo(Const("✏️ Ввести своё"), id="es_type", state=EditGroupSG.style_typing),
        Button(Const("⏭ Очистить"), id="es_clear", on_click=_on_style_clear),
        SwitchTo(Const("◀ Назад"), id="es_back", state=EditGroupSG.menu),
        getter=_style_getter,
        state=EditGroupSG.style,
    ),
    Window(
        Const("Напиши новое направление одной строкой:"),
        TextInput(id="es_style_input", on_success=_on_style_typed),
        SwitchTo(Const("◀ Назад"), id="es_typing_back", state=EditGroupSG.style),
        state=EditGroupSG.style_typing,
    ),
    Window(
        Const("Новый уровень?"),
        Select(
            Format("ур.{item}"),
            id="el_select",
            item_id_getter=str,
            items=[0, 1, 2, 3, 4],
            on_click=_on_level_pick,
        ),
        Button(Const("⏭ Очистить"), id="el_clear", on_click=_on_level_clear),
        SwitchTo(Const("◀ Назад"), id="el_back", state=EditGroupSG.menu),
        state=EditGroupSG.level,
    ),
    Window(
        Const("Новые дни? Отметь и нажми «Готово»."),
        Multiselect(
            Format("✅ {item[1]}"),
            Format("{item[1]}"),
            id="wd_select",
            item_id_getter=lambda item: str(item[0]),
            items="days",
        ),
        Button(Const("Готово ▶"), id="wd_done", on_click=_on_weekdays_done),
        SwitchTo(Const("◀ Назад"), id="wd_back", state=EditGroupSG.menu),
        getter=_weekdays_getter,
        state=EditGroupSG.weekdays,
    ),
    Window(
        Const("Новое время? Формат ЧЧ:ММ, например 19:00"),
        TextInput(id="et_input", on_success=_on_time_typed),
        SwitchTo(Const("◀ Назад"), id="et_back", state=EditGroupSG.menu),
        state=EditGroupSG.lesson_time,
    ),
    Window(
        Const("Новое место?"),
        Select(
            Format("{item[1]}"),
            id="ev_select",
            item_id_getter=lambda item: str(item[0]),
            items="venues",
            on_click=_on_venue_pick,
        ),
        SwitchTo(
            Const("➕ Добавить новую"),
            id="ev_new",
            state=EditGroupSG.venue_new_name,
        ),
        Button(Const("⏭ Очистить"), id="ev_clear", on_click=_on_venue_clear),
        SwitchTo(Const("◀ Назад"), id="ev_back", state=EditGroupSG.menu),
        getter=_venue_getter,
        state=EditGroupSG.venue,
    ),
    Window(
        Const("Название новой площадки одной строкой:"),
        TextInput(id="evn_name_input", on_success=_on_venue_new_name_typed),
        SwitchTo(Const("◀ Назад"), id="evn_name_back", state=EditGroupSG.venue),
        state=EditGroupSG.venue_new_name,
    ),
    Window(
        Const("Адрес площадки (если хочешь). Можно пропустить:"),
        TextInput(id="evn_addr_input", on_success=_on_venue_new_addr_typed),
        Button(
            Const("⏭ Пропустить"),
            id="evn_addr_skip",
            on_click=_on_venue_new_addr_skip,
        ),
        state=EditGroupSG.venue_new_address,
    ),
    Window(
        Const("Новый зал? Или нажми «Очистить», чтобы убрать."),
        TextInput(id="er_input", on_success=_on_room_typed),
        Button(Const("⏭ Очистить"), id="er_clear", on_click=_on_room_clear),
        SwitchTo(Const("◀ Назад"), id="er_back", state=EditGroupSG.menu),
        state=EditGroupSG.room,
    ),
    Window(
        Const("Кто ведёт? Свободной строкой. Можно очистить, чтобы убрать."),
        TextInput(id="et2_input", on_success=_on_teachers_typed),
        Button(Const("⏭ Очистить"), id="et2_clear", on_click=_on_teachers_clear),
        SwitchTo(Const("◀ Назад"), id="et2_back", state=EditGroupSG.menu),
        state=EditGroupSG.teachers,
    ),
)


__all__ = ["edit_group_dialog", "EditGroupSG"]
