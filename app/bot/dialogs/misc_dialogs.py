"""Мелкие 1-2-экранные dialog'и: CreateSchool, RenameSchool, EditName, Broadcast.

Сгруппированы в один файл — каждый flow слишком мал чтобы заводить отдельный
модуль, а парадигма одна: TextInput → save → done.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel
from aiogram_dialog.widgets.text import Const, Format

from app.bot.dialogs.states import (
    BroadcastSG,
    CreateSchoolSG,
    EditNameSG,
    RenameSchoolSG,
    user_from,
)
from app.bot.utils import format_group_title
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc

log = logging.getLogger(__name__)


# ── CreateSchool ─────────────────────────────────────────────────────


async def _on_create_school_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    name = value.strip()
    if not name:
        await message.answer("Пустое название — давай ещё раз.")
        return
    if len(name) > 120:
        await message.answer("Слишком длинное название (максимум 120 символов).")
        return
    user = user_from(manager)
    if user is None:
        await manager.done()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None:
            await manager.done()
            return
        existing = await schools_svc.get_owned_school(s, admin.id)
        school = existing or await schools_svc.create_school(s, admin.id, name)
        admin.has_completed_setup = True
        await s.flush()
        school_name = school.name
    await message.answer(
        f"✅ Школа «{escape(school_name)}» создана.\n\n"
        f"Дальше — снизу 👇 меню: создай первую группу через «➕ Новая группа», "
        f"в карточке школы можно пригласить co-админа.",
        parse_mode=ParseMode.HTML,
    )
    # Push reply-keyboard через handler — открыто здесь нельзя, manager.done()
    # удалит inline-keyboard. Reply-меню юзеру уже было до старта diaologа.
    await manager.done()


create_school_dialog = Dialog(
    Window(
        Const(
            "💓 Dance Pulse\n\n"
            "Ты получил статус админа — поздравляю! Дай название своей школе "
            "(контейнер, в котором будут жить твои группы и ученики). "
            "Например: «Wave Dance» или «Школа Маши»."
        ),
        TextInput(id="cs_name", on_success=_on_create_school_typed),
        Cancel(Const("⏭ Отмена")),
        state=CreateSchoolSG.name,
    ),
)


# ── RenameSchool ─────────────────────────────────────────────────────


def _school_id_from_start(manager: DialogManager) -> int | None:
    data = manager.start_data
    if isinstance(data, dict) and "school_id" in data:
        return int(data["school_id"])
    return None


async def _on_rename_school_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    new_name = value.strip()
    if not new_name:
        await message.answer("Пустое название — отменил.")
        await manager.done()
        return
    school_id = _school_id_from_start(manager)
    user = user_from(manager)
    if school_id is None or user is None:
        await manager.done()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await message.answer("Только owner может переименовывать.")
            await manager.done()
            return
        await schools_svc.rename_school(s, school_id, new_name)
    await message.answer(
        f"✅ Теперь школа называется «{escape(new_name)}».",
        parse_mode=ParseMode.HTML,
    )
    await manager.done()


rename_school_dialog = Dialog(
    Window(
        Const("Напиши новое название школы."),
        TextInput(id="rs_name", on_success=_on_rename_school_typed),
        Cancel(Const("⏭ Отмена")),
        state=RenameSchoolSG.name,
    ),
)


# ── EditName ─────────────────────────────────────────────────────────


async def _edit_name_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    user = user_from(dialog_manager)
    current = "—"
    if user is not None:
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, user.id)
            persons_rows = await persons_svc.find_all_by_tg(s, user.id)
        if teacher is not None:
            current = teacher.full_name
        elif persons_rows:
            current = persons_rows[0].full_name
    return {"current": escape(current)}


async def _on_edit_name_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    new_name = value.strip()
    if not new_name:
        await message.answer("Пустое имя — напиши ещё раз.")
        return
    if len(new_name) > 200:
        new_name = new_name[:200]
    user = user_from(manager)
    if user is None:
        await manager.done()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, user.id)
        if teacher is not None:
            await admins_svc.set_name(s, teacher.id, new_name)
        touched = await persons_svc.set_name_everywhere(s, user.id, new_name)
    safe = escape(new_name)
    if teacher is not None and touched == 0:
        await message.answer(
            f"✅ Записал: тебя зовут «{safe}».", parse_mode=ParseMode.HTML,
        )
    elif teacher is not None:
        await message.answer(
            f"✅ Записал: тебя зовут «{safe}». Обновил в {touched} карточке(ах).",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"✅ Записал: тебя зовут «{escape(new_name)}». Преподаватели увидят это имя.",
            parse_mode=ParseMode.HTML,
        )
    await manager.done()


edit_name_dialog = Dialog(
    Window(
        Format(
            "Как тебя называть в опросах и списках? Напиши имя одной строкой.\n"
            "Сейчас: «{current}»"
        ),
        TextInput(id="en_name", on_success=_on_edit_name_typed),
        Cancel(Const("⏭ Отмена")),
        getter=_edit_name_getter,
        state=EditNameSG.name,
    ),
)


# ── Broadcast ────────────────────────────────────────────────────────


def _group_id_from_start(manager: DialogManager) -> int | None:
    data = manager.start_data
    if isinstance(data, dict) and "group_id" in data:
        return int(data["group_id"])
    return None


async def _broadcast_preview_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    group_id = dialog_manager.dialog_data.get("group_id") or _group_id_from_start(dialog_manager)
    text = dialog_manager.dialog_data.get("text", "")
    count = 0
    title = "—"
    if group_id is not None:
        async with session_scope() as s:
            group = await groups_svc.get_group(s, int(group_id))
            if group is not None:
                count = await persons_svc.count_members(s, int(group_id))
                title = format_group_title(group)
    return {"count": count, "title": escape(title), "text": escape(text)}


async def _on_broadcast_text_typed(
    _msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    text = value.strip()
    if not text:
        # MessageInput автоматически дождётся повторного ввода.
        return
    manager.dialog_data["text"] = text
    # Сохраним group_id в dialog_data чтобы getter не лазил в start_data повторно.
    gid = _group_id_from_start(manager)
    if gid is not None:
        manager.dialog_data["group_id"] = gid
    await manager.switch_to(BroadcastSG.confirm)


async def _on_broadcast_send(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    text = manager.dialog_data.get("text")
    group_id = manager.dialog_data.get("group_id") or _group_id_from_start(manager)
    if not text or group_id is None:
        await manager.done()
        return
    user = call.from_user
    async with session_scope() as s:
        group = await groups_svc.get_group(s, int(group_id))
        if group is None:
            if call.message:
                await call.message.answer("Группа не найдена.")
            await manager.done()
            return
        members = await persons_svc.group_members(s, int(group_id))
        title = format_group_title(group)
    sender_name = user.full_name or user.username or "препод"
    body = f"📢 «{escape(title)}» — сообщение от {escape(sender_name)}:\n\n{escape(text)}"
    bot = manager.middleware_data.get("bot")
    sent = blocked = failed = 0
    for p in members:
        try:
            await bot.send_message(p.tg_user_id, body, parse_mode=ParseMode.HTML)
            sent += 1
        except (TelegramForbiddenError, TelegramNotFound):
            blocked += 1
        except Exception as e:  # noqa: BLE001
            log.warning("broadcast to %s failed: %s", p.tg_user_id, e)
            failed += 1
    parts = [f"📢 Отправлено: {sent}/{len(members)}"]
    if blocked:
        parts.append(f"заблокировали бота: {blocked}")
    if failed:
        parts.append(f"не доехало: {failed}")
    if call.message:
        await call.message.answer(" · ".join(parts))
    await manager.done()


broadcast_dialog = Dialog(
    Window(
        Const(
            "📢 Напиши сообщение, которое отправлю всем ученикам группы. "
            "Чтобы передумать — нажми «Отмена»."
        ),
        TextInput(id="bc_text", on_success=_on_broadcast_text_typed),
        Cancel(Const("⏭ Отмена")),
        state=BroadcastSG.text,
    ),
    Window(
        Format(
            "📢 Будет отправлено {count} ученикам «{title}»:\n\n<i>{text}</i>"
        ),
        Button(Const("📨 Отправить"), id="bc_send", on_click=_on_broadcast_send),
        Cancel(Const("◀ Отмена")),
        getter=_broadcast_preview_getter,
        parse_mode=ParseMode.HTML,
        state=BroadcastSG.confirm,
    ),
)


__all__ = [
    "broadcast_dialog",
    "create_school_dialog",
    "edit_name_dialog",
    "rename_school_dialog",
]
