"""Мелкие 1-2-экранные dialog'и: CreateSchool, RenameSchool, EditName, Broadcast.

Сгруппированы в один файл — каждый flow слишком мал чтобы заводить отдельный
модуль, а парадигма одна: TextInput → save → done.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel
from aiogram_dialog.widgets.text import Const, Format

from app.bot import ui
from app.bot.dialogs.states import (
    AddTeacherPresetSG,
    AddVenueSG,
    BroadcastSG,
    CreateSchoolSG,
    EditNameSG,
    RenameSchoolSG,
    user_from,
)
from app.bot.keyboards import admin_menu_keyboard
from app.bot.utils import format_group_title
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import broadcasts as broadcasts_svc
from app.services import groups as groups_svc
from app.services import outbox as outbox_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import teacher_presets as presets_svc
from app.services import venues as venues_svc

log = logging.getLogger(__name__)


async def on_cancel_notify(
    call: CallbackQuery, _btn: Button, _manager: DialogManager
) -> None:
    """Единый фидбек Cancel-кнопок: молчаливое снятие клавиатуры на пилоте
    читается как «бот завис» — шлём короткое подтверждение."""
    if call.message:
        await call.message.answer("⏭ Отменено")


# ── CreateSchool ─────────────────────────────────────────────────────


async def _finish_create_school(message: Message, manager: DialogManager) -> None:
    school_name = manager.dialog_data.get("school_name", "")
    await message.answer(
        f"✅ Школа «{escape(school_name)}» создана.\n\n"
        f"Дальше — снизу 👇 меню: создай первую группу через «➕ Новая группа», "
        f"в карточке школы можно пригласить co-админа.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )
    await manager.done()


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
        school_id = school.id
        already_had_school = existing is not None
    if already_had_school:
        # У admin'а уже была своя школа (напр. повторный визит в этот wizard) —
        # честно говорим, что новую не завели, а не врём про «создана».
        await message.answer(
            f"У тебя уже есть школа «{escape(school_name)}» — новую заводить не стал.\n\n"
            f"Дальше — снизу 👇 меню: создай первую группу через «➕ Новая группа», "
            f"в карточке школы можно пригласить co-админа.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        await manager.done()
        return
    manager.dialog_data["school_name"] = school_name
    manager.dialog_data["school_id"] = school_id
    await manager.switch_to(CreateSchoolSG.place)


async def _on_create_school_place_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    place = value.strip()
    if not place:
        return
    school_id = manager.dialog_data.get("school_id")
    if school_id is not None:
        async with session_scope() as s:
            await venues_svc.get_or_create(s, int(school_id), place[:120])
    await _finish_create_school(message, manager)


async def _on_create_school_place_skip(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    if call.message:
        await _finish_create_school(call.message, manager)


create_school_dialog = Dialog(
    Window(
        Const(
            "💓 Dance Pulse\n\n"
            "Ты получил статус админа — поздравляю! Дай название своей школе "
            "(контейнер, в котором будут жить твои группы и ученики). "
            "Например: «Школа сальсы» или «Студия Ритм»."
        ),
        TextInput(id="cs_name", on_success=_on_create_school_typed),
        Cancel(Const("⏭ Отмена"), on_click=on_cancel_notify),
        state=CreateSchoolSG.name,
    ),
    Window(
        Const(
            "Где проходят занятия? Напиши адрес или название студии одной "
            "строкой (например: «Тверская 10» или «Студия Ритм») — "
            "это станет местом по умолчанию для твоих групп. Площадки можно "
            "менять в карточке школы."
        ),
        TextInput(id="cs_place", on_success=_on_create_school_place_typed),
        Button(
            Const("⏭ Пропустить"), id="cs_place_skip",
            on_click=_on_create_school_place_skip,
        ),
        state=CreateSchoolSG.place,
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
        Cancel(Const("⏭ Отмена"), on_click=on_cancel_notify),
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
        Cancel(Const("⏭ Отмена"), on_click=on_cancel_notify),
        getter=_edit_name_getter,
        state=EditNameSG.name,
        parse_mode=ParseMode.HTML,
    ),
)


# ── Broadcast ────────────────────────────────────────────────────────

# Telegram's sendMessage text limit is 4096 символов. Берём с запасом ниже —
# на префикс («📢 «Title» — сообщение от X:\n\n») и на расширение при
# HTML-экранировании (`<`→`&lt;` и т.п.) остаётся честный запас, а не
# «упало прямо на границе».
MAX_BROADCAST_TEXT = 3500


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
    count_phrase = ui.plural_with_count(count, "ученику", "ученикам", "ученикам")
    return {"count_phrase": count_phrase, "title": escape(title), "text": escape(text)}


async def _on_broadcast_text_typed(
    msg: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    text = value.strip()
    if not text:
        # MessageInput автоматически дождётся повторного ввода.
        return
    if len(text) > MAX_BROADCAST_TEXT:
        await msg.answer(
            f"Слишком длинно ({len(text)} символов, максимум {MAX_BROADCAST_TEXT}) — "
            "сократи и пришли снова."
        )
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
    # dialog_data["sending"]'s role changed with the outbox migration: it no
    # longer needs to prevent duplicate SENDS — enqueue() is idempotent per
    # business_key (broadcast:{broadcast_id}:{person_id}), so re-running this
    # handler after the Broadcast row already exists would just re-enqueue
    # no-ops. What it still prevents is creating two separate Broadcast rows
    # (and thus double-counting/double-billing the "N recipients" number) from
    # a double-tap before manager navigates off this window. Kept as a
    # lightweight UI-level debounce for exactly that, not for send-safety.
    if manager.dialog_data.get("sending"):
        await call.answer("Уже отправляю…")
        return
    manager.dialog_data["sending"] = True
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
        admin = await admins_svc.get_by_tg_id(s, user.id)
        sender_name = user.full_name or user.username or "препод"
        body = f"📢 «{escape(title)}» — сообщение от {escape(sender_name)}:\n\n{escape(text)}"

        # Broadcast row + every recipient's outbox row commit in the SAME
        # transaction — a crash right after this commits leaves durable rows
        # the sender worker still delivers on restart (Phase 3), instead of
        # the old direct send-loop losing everything not yet sent.
        broadcast = await broadcasts_svc.create_broadcast(
            s, scope_type="group", scope_payload={"group_id": int(group_id)},
            audience_type="members", text=text,
            created_by_admin_id=admin.id if admin else None,
        )
        payload = outbox_svc.build_payload(body, parse_mode=ParseMode.HTML.value, kind="broadcast")
        for p in members:
            key = f"broadcast:{broadcast.id}:{p.id}"
            await outbox_svc.enqueue(s, key, p.tg_user_id, payload, broadcast_id=broadcast.id)
        recipient_count = len(members)
        broadcast_id = broadcast.id

    manager.dialog_data["broadcast_id"] = broadcast_id
    manager.dialog_data["title"] = title
    if call.message:
        count_phrase = ui.plural_with_count(recipient_count, "ученику", "ученикам", "ученикам")
        await call.message.answer(f"📢 Рассылка поставлена в очередь: {count_phrase}.")
    await manager.switch_to(BroadcastSG.progress)


async def _broadcast_progress_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    broadcast_id = dialog_manager.dialog_data.get("broadcast_id")
    title = dialog_manager.dialog_data.get("title", "—")
    if broadcast_id is None:
        return {"text": "Рассылка не найдена."}
    async with session_scope() as s:
        counts = await broadcasts_svc.progress(s, int(broadcast_id))
    text = ui.card(
        ui.title(ui.BROADCAST, "Рассылка"),
        meta=f"«{escape(title)}»",
        body=[
            f"{ui.YES} доставлено: {counts['sent']}",
            f"{ui.PENDING} в очереди: {counts['pending']}",
            f"{ui.UNDELIVERED} не доставлено: {counts['terminal_failed']}",
            f"всего: {counts['total']}",
        ],
    )
    return {"text": text}


async def _on_broadcast_progress_refresh(
    call: CallbackQuery, _btn: Button, _manager: DialogManager
) -> None:
    # Тап ничего не меняет — aiogram-dialog перерисовывает текущее окно
    # (заново вызывает getter) после любого widget-взаимодействия, если
    # хэндлер не ушёл в manager.done()/switch_to() — этого достаточно, чтобы
    # получить свежие counts.
    await call.answer("Обновлено")


async def _on_broadcast_progress_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await manager.done()


async def _on_broadcast_cancel(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    """Broadcast открывается из карточки группы и съедает её — на Cancel
    возвращаем карточку заново, а не голое «Отменено». Импорт ленивый —
    admin.py сам лениво импортирует dialogs."""
    if call.message is None:
        return
    group_id = manager.dialog_data.get("group_id") or _group_id_from_start(manager)
    if group_id is None:
        await call.message.answer("⏭ Отменено")
        return
    from app.bot.handlers.admin import send_group_card

    await send_group_card(call.message, call.from_user.id, int(group_id))


broadcast_dialog = Dialog(
    Window(
        Const(
            "📢 Напиши сообщение, которое отправлю всем ученикам группы. "
            "Чтобы передумать — нажми «Отмена»."
        ),
        TextInput(id="bc_text", on_success=_on_broadcast_text_typed),
        Cancel(Const("⏭ Отмена"), on_click=_on_broadcast_cancel),
        state=BroadcastSG.text,
    ),
    Window(
        Format(
            "📢 Будет отправлено {count_phrase} «{title}»:\n\n<i>{text}</i>"
        ),
        Button(Const("📨 Отправить"), id="bc_send", on_click=_on_broadcast_send),
        Cancel(Const("◀ Отмена"), on_click=_on_broadcast_cancel),
        getter=_broadcast_preview_getter,
        parse_mode=ParseMode.HTML,
        state=BroadcastSG.confirm,
    ),
    Window(
        Format("{text}"),
        Button(Const("🔄 Обновить"), id="bc_refresh", on_click=_on_broadcast_progress_refresh),
        Button(Const("✅ Готово"), id="bc_done", on_click=_on_broadcast_progress_done),
        getter=_broadcast_progress_getter,
        parse_mode=ParseMode.HTML,
        state=BroadcastSG.progress,
    ),
)


# ── AddVenue / AddTeacherPreset (из меню школы) ──────────────────────


async def _member_school_id(manager: DialogManager) -> int | None:
    """school_id из start_data, если открывший диалог — член этой школы."""
    school_id = _school_id_from_start(manager)
    user = user_from(manager)
    if school_id is None or user is None:
        return None
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, user.id)
        if admin is None or not await schools_svc.is_member(s, school_id, admin.id):
            return None
    return school_id


def _back_to_kb(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=callback_data)
    ]])


async def _on_add_venue_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    name = value.strip()
    if not name:
        return
    school_id = await _member_school_id(manager)
    if school_id is None:
        await manager.done()
        return
    async with session_scope() as s:
        await venues_svc.get_or_create(s, school_id, name[:120])
    await message.answer(
        f"✅ Площадка «{escape(name[:120])}» добавлена.",
        parse_mode=ParseMode.HTML,
        reply_markup=_back_to_kb("📍 К площадкам", f"schvenues:{school_id}"),
    )
    await manager.done()


add_venue_dialog = Dialog(
    Window(
        Const(
            "Напиши место одной строкой — адрес или название студии "
            "(например: «Тверская 10» или «Студия Ритм»):"
        ),
        TextInput(id="av_name", on_success=_on_add_venue_typed),
        Cancel(Const("✖ Отмена"), on_click=on_cancel_notify),
        state=AddVenueSG.name,
    ),
)


async def _on_add_preset_typed(
    message: Message, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    note = value.strip()
    if not note:
        return
    school_id = await _member_school_id(manager)
    if school_id is None:
        await manager.done()
        return
    async with session_scope() as s:
        await presets_svc.get_or_create(s, school_id, note[:200])
    await message.answer(
        f"✅ Сохранил: «{escape(note[:200])}».",
        parse_mode=ParseMode.HTML,
        reply_markup=_back_to_kb("👤 К преподавателям", f"schteachers:{school_id}"),
    )
    await manager.done()


add_teacher_preset_dialog = Dialog(
    Window(
        Const(
            "Кто ведёт? Свободной строкой — например, «Аня + Иван». "
            "Связка появится кнопкой в мастере группы:"
        ),
        TextInput(id="ap_note", on_success=_on_add_preset_typed),
        Cancel(Const("✖ Отмена"), on_click=on_cancel_notify),
        state=AddTeacherPresetSG.note,
    ),
)


__all__ = [
    "add_teacher_preset_dialog",
    "add_venue_dialog",
    "broadcast_dialog",
    "create_school_dialog",
    "edit_name_dialog",
    "rename_school_dialog",
]
