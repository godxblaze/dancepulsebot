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

from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Multiselect, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from app.bot.dialogs.states import EditGroupSG, user_from
from app.bot.utils import derive_group_name, format_duration, format_group_title
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from app.services import teacher_presets as presets_svc
from app.services import venues as venues_svc
from app.services.schedule import format_weekdays

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_DURATION_OPTIONS = [(60, "1 ч"), (90, "1,5 ч"), (120, "2 ч"), (180, "3 ч")]


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
    """Обновить поля группы. Перевычислить name если меняется style/level.
    Правка lesson_time/weekdays может осиротить открытый Survey (см.
    groups_svc.update_group) — уведомляем ответственных через bot диалога."""
    if not await _owner_check(manager):
        return
    group_id = _gid(manager)
    notify_tgs: list[int] = []
    title = ""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        title = format_group_title(group)
        notify_tgs = await groups_svc.update_group(s, group, **fields)
        if "style" in fields or "level" in fields:
            await groups_svc.update_group(
                s, group, name=derive_group_name(group.style, group.level)
            )
    if notify_tgs:
        bot = manager.middleware_data.get("bot")
        text = (
            f"⚠️ Расписание группы «{escape(title)}» изменилось — открытый "
            "опрос по старому времени закрыт, новый уйдёт по актуальному "
            "расписанию."
        )
        for tg in notify_tgs:
            try:
                await bot.send_message(tg, text, parse_mode=ParseMode.HTML)
            except Exception as e:  # noqa: BLE001
                log.warning("reschedule-notice to %s failed: %s", tg, e)


# ── menu ──────────────────────────────────────────────────────────────


async def _menu_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    group_id = _gid(dialog_manager)
    title = "—"
    summary = ""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None:
            title = format_group_title(group)
            schedule = (
                f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}"
            )
            if group.duration_min:
                schedule += f" ({format_duration(group.duration_min)})"
            lines = [schedule]
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
    suggestions: list[str] = []
    group_id = _gid(dialog_manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None and group.school_id is not None:
            suggestions = await groups_svc.school_styles(s, group.school_id)
    return {"styles": list(enumerate(suggestions[:6]))}


async def _on_style_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        idx = int(item_id)
    except ValueError:
        return
    group_id = _gid(manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        suggestions = (
            await groups_svc.school_styles(s, group.school_id)
            if group is not None and group.school_id is not None else []
        )
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


async def _on_weekdays_back(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    """«◀ Назад» без сохранения — тоже должен сбросить weekdays_initialized,
    иначе при повторном заходе в этот экран getter не переинициализирует
    чекбоксы из актуального состояния группы (флаг уже True с прошлого
    визита) и покажет отменённый черновик выбора."""
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


# ── duration ──────────────────────────────────────────────────────────


async def _on_duration_pick(
    _call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        minutes = int(item_id)
    except ValueError:
        return
    if minutes not in {m for m, _ in _DURATION_OPTIONS}:
        return
    await _patch_group(manager, duration_min=minutes)
    await manager.switch_to(EditGroupSG.menu)


async def _on_duration_clear(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await _patch_group(manager, duration_min=None)
    await manager.switch_to(EditGroupSG.menu)


# ── venue ─────────────────────────────────────────────────────────────


async def _venue_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    venues_list: list = []
    group_id = _gid(dialog_manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None and group.school_id is not None:
            venues_list = await venues_svc.school_venues(s, group.school_id)
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


async def _on_venue_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    """Новая площадка одной строкой (адрес или название студии). Заводим её в
    школе РЕДАКТИРУЕМОЙ группы, а не в owned-школе admin'а — иначе co-admin
    без своей школы получал бы орфанный Venue (и потенциально фантомную
    «Моя школа»), никогда не привязанный к группе."""
    name = value.strip()
    user = user_from(manager)
    if user is None or not name:
        return
    group_id = _gid(manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None or group.school_id is None:
            return
        venue = await venues_svc.get_or_create(s, group.school_id, name[:120])
        venue_id = venue.id
    await _patch_group(manager, venue_id=venue_id)
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


async def _teachers_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    presets: list = []
    group_id = _gid(dialog_manager)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is not None and group.school_id is not None:
            presets = await presets_svc.school_presets(s, group.school_id)
    items: list[tuple[int, str]] = []
    for p in presets:
        label = f"👤 {p.note}"
        if len(label) > 64:
            label = label[:61] + "…"
        items.append((p.id, label))
    return {"presets": items}


async def _on_teachers_pick(
    call: CallbackQuery, _widget: Any, manager: DialogManager, item_id: str
) -> None:
    try:
        preset_id = int(item_id)
    except ValueError:
        return
    group_id = _gid(manager)
    async with session_scope() as s:
        preset = await presets_svc.get_preset(s, preset_id)
        group = await groups_svc.get_group(s, group_id)
        if preset is None or group is None or preset.school_id != group.school_id:
            await call.answer()
            return
        note = preset.note
    await _patch_group(manager, teachers_note=note)
    await manager.switch_to(EditGroupSG.menu)


async def _on_teachers_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    note = value.strip() or None
    if note:
        group_id = _gid(manager)
        async with session_scope() as s:
            group = await groups_svc.get_group(s, group_id)
            if group is not None and group.school_id is not None:
                await presets_svc.get_or_create(s, group.school_id, note[:200])
    await _patch_group(manager, teachers_note=note)
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
    """Закрыть dialog и показать карточку группы — как одноимённые кнопки
    в «Ответственных» и «Зале на занятие». Импорт ленивый: admin.py сам
    лениво импортирует dialogs."""
    from app.bot.handlers.admin import send_group_card

    group_id = _gid(manager)
    await manager.done()
    if call.message:
        await send_group_card(call.message, call.from_user.id, group_id)


# ── Dialog ────────────────────────────────────────────────────────────


edit_group_dialog = Dialog(
    Window(
        Format("✏️ «{title}»\n{summary}\n\nЧто меняем?"),
        SwitchTo(Const("💃 Направление"), id="m_style", state=EditGroupSG.style),
        SwitchTo(Const("🎯 Уровень"), id="m_level", state=EditGroupSG.level),
        SwitchTo(Const("📅 Дни недели"), id="m_days", state=EditGroupSG.weekdays),
        SwitchTo(Const("🕐 Время"), id="m_time", state=EditGroupSG.lesson_time),
        SwitchTo(Const("⏱ Длительность"), id="m_duration", state=EditGroupSG.duration),
        SwitchTo(Const("📍 Место"), id="m_venue", state=EditGroupSG.venue),
        SwitchTo(Const("🚪 Зал"), id="m_room", state=EditGroupSG.room),
        SwitchTo(Const("👤 Преподаватели"), id="m_teachers", state=EditGroupSG.teachers),
        Button(Const("◀ К группе"), id="done", on_click=_on_done),
        getter=_menu_getter,
        state=EditGroupSG.menu,
        parse_mode=ParseMode.HTML,
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
        TextInput(id="es_direct_input", on_success=_on_style_typed),
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
        Button(Const("◀ Назад"), id="wd_back", on_click=_on_weekdays_back),
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
        Const("Сколько длится занятие?"),
        Select(
            Format("{item[1]}"),
            id="ed_select",
            item_id_getter=lambda item: str(item[0]),
            items=_DURATION_OPTIONS,
            on_click=_on_duration_pick,
        ),
        Button(Const("⏭ Очистить"), id="ed_clear", on_click=_on_duration_clear),
        SwitchTo(Const("◀ Назад"), id="ed_back", state=EditGroupSG.menu),
        state=EditGroupSG.duration,
    ),
    Window(
        Const(
            "Новое место? Выбери площадку или напиши новую одной строкой "
            "(адрес или название студии):"
        ),
        Select(
            Format("{item[1]}"),
            id="ev_select",
            item_id_getter=lambda item: str(item[0]),
            items="venues",
            on_click=_on_venue_pick,
        ),
        Button(Const("⏭ Очистить"), id="ev_clear", on_click=_on_venue_clear),
        SwitchTo(Const("◀ Назад"), id="ev_back", state=EditGroupSG.menu),
        TextInput(id="ev_direct_input", on_success=_on_venue_typed),
        getter=_venue_getter,
        state=EditGroupSG.venue,
    ),
    Window(
        Const("Новый зал? Или нажми «Очистить», чтобы убрать."),
        TextInput(id="er_input", on_success=_on_room_typed),
        Button(Const("⏭ Очистить"), id="er_clear", on_click=_on_room_clear),
        SwitchTo(Const("◀ Назад"), id="er_back", state=EditGroupSG.menu),
        state=EditGroupSG.room,
    ),
    Window(
        Const(
            "Кто ведёт? Тапни сохранённую связку или напиши свободной "
            "строкой. Можно очистить, чтобы убрать."
        ),
        Select(
            Format("{item[1]}"),
            id="et2_select",
            item_id_getter=lambda item: str(item[0]),
            items="presets",
            on_click=_on_teachers_pick,
        ),
        TextInput(id="et2_input", on_success=_on_teachers_typed),
        Button(Const("⏭ Очистить"), id="et2_clear", on_click=_on_teachers_clear),
        SwitchTo(Const("◀ Назад"), id="et2_back", state=EditGroupSG.menu),
        getter=_teachers_getter,
        state=EditGroupSG.teachers,
    ),
)


__all__ = ["edit_group_dialog", "EditGroupSG"]
