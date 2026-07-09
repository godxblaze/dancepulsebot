"""Phase 11: global developer broadcast — the single most powerful and most
dangerous tool in the system (can message every user across every school).

Reuses Phase 5's `Broadcast`+`message_outbox` mechanism exactly (see
`app/services/broadcasts.py`/`app/services/dev_broadcast.py`) — no second
broadcast implementation. Gated to `settings.developer_ids` at every entry
point (dialog start AND the send handler itself, defense in depth) and can
only be started from the Phase 10 developer menu.

Flow: scope (all schools / pick specific ones) → audience (Person/Admin) →
text → confirm (LIVE recipient count, queried fresh at render time — never a
cached/estimated number) → send (only reachable from confirm's button) →
progress.
"""
from __future__ import annotations

from html import escape
from typing import Any

from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Multiselect, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from app.bot import ui
from app.bot.dialogs.misc_dialogs import MAX_BROADCAST_TEXT, on_cancel_notify
from app.bot.dialogs.states import DevBroadcastSG
from app.core.config import settings
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import broadcasts as broadcasts_svc
from app.services import dev_broadcast as dev_broadcast_svc
from app.services import outbox as outbox_svc

_VALID_SCOPES = ("all", "subset")
_VALID_AUDIENCES = ("person", "admin")


# ── scope ────────────────────────────────────────────────────────────


async def _on_scope_all(call: CallbackQuery, _btn: Button, manager: DialogManager) -> None:
    manager.dialog_data["scope_type"] = "all"
    manager.dialog_data.pop("school_ids", None)
    await manager.switch_to(DevBroadcastSG.audience)


async def _scope_pick_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    async with session_scope() as s:
        schools = await dev_broadcast_svc.list_schools(s)
    return {"schools": [(sid, escape(name)) for sid, name in schools]}


async def _on_scope_pick_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    ms = manager.find("dbc_school_select")
    selected = sorted(int(i) for i in ms.get_checked()) if ms else []
    if not selected:
        await call.answer("Отметь хотя бы одну школу.", show_alert=True)
        return
    manager.dialog_data["scope_type"] = "subset"
    manager.dialog_data["school_ids"] = selected
    await manager.switch_to(DevBroadcastSG.audience)


# ── audience ─────────────────────────────────────────────────────────


async def _on_audience_pick(
    call: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    manager.dialog_data["audience_type"] = "admin" if button.widget_id == "aud_admin" else "person"
    await manager.switch_to(DevBroadcastSG.text)


# ── text ─────────────────────────────────────────────────────────────


async def _on_text_typed(
    msg, _widget: TextInput, manager: DialogManager, value: str
) -> None:
    text = value.strip()
    if not text:
        return
    if len(text) > MAX_BROADCAST_TEXT:
        await msg.answer(
            f"Слишком длинно ({len(text)} символов, максимум {MAX_BROADCAST_TEXT}) — "
            "сократи и пришли снова."
        )
        return
    manager.dialog_data["text"] = text
    await manager.switch_to(DevBroadcastSG.confirm)


# ── confirm ──────────────────────────────────────────────────────────


def _recipient_nouns(audience_type: str | None) -> tuple[str, str, str]:
    """Audience-specific склонение — «5 ученикам» / «5 преподавателям», не
    общее «получателям» (то дублировало бы уже названную аудиторию)."""
    if audience_type == "admin":
        return "преподавателю", "преподавателям", "преподавателям"
    return "ученику", "ученикам", "ученикам"


async def _confirm_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    """LIVE count — re-queried on every render, never cached from an earlier
    step. If scope/audience were changed and the user navigates back here,
    the number always reflects the CURRENT selection."""
    scope_type = dialog_manager.dialog_data.get("scope_type")
    audience_type = dialog_manager.dialog_data.get("audience_type")
    text = dialog_manager.dialog_data.get("text", "")
    async with session_scope() as s:
        if scope_type == "all":
            school_ids = await dev_broadcast_svc.all_school_ids(s)
            scope_desc = "все школы"
        elif scope_type == "subset":
            school_ids = [int(i) for i in dialog_manager.dialog_data.get("school_ids") or []]
            names = await dev_broadcast_svc.school_names(s, school_ids)
            scope_desc = ", ".join(escape(n) for n in names) if names else "школы не выбраны"
        else:
            school_ids, scope_desc = [], "не выбрано"
        count = await dev_broadcast_svc.recipient_count(
            s, school_ids, audience_type if audience_type in _VALID_AUDIENCES else "person"
        )
    count_phrase = ui.plural_with_count(count, *_recipient_nouns(audience_type))
    return {
        "ui_broadcast": ui.BROADCAST,
        "scope_desc": scope_desc,
        "count_phrase": count_phrase,
        "text": escape(text),
    }


async def _on_dev_broadcast_send(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    if manager.dialog_data.get("sending"):
        await call.answer("Уже отправляю…")
        return
    tg = call.from_user
    if tg is None or tg.id not in settings.developer_ids:
        # Экран недостижим для не-дева (см. cb_dev_broadcast_start), но
        # хендлер всё равно не должен доверять этому молча — defense in depth.
        await call.answer()
        return

    scope_type = manager.dialog_data.get("scope_type")
    audience_type = manager.dialog_data.get("audience_type")
    text = manager.dialog_data.get("text")
    if scope_type not in _VALID_SCOPES or audience_type not in _VALID_AUDIENCES or not text:
        # Незавершённый или подделанный dialog_data (напр. прыжок сразу на
        # confirm мимо scope/audience/text) — fail safe: ничего не создаём и
        # не отправляем, просто закрываем диалог с понятным сообщением.
        if call.message:
            await call.message.answer(
                "Сессия рассылки повреждена или неполна — начни заново."
            )
        await manager.done()
        return

    async with session_scope() as s:
        if scope_type == "all":
            school_ids = await dev_broadcast_svc.all_school_ids(s)
        else:
            school_ids = [int(i) for i in manager.dialog_data.get("school_ids") or []]
            if not school_ids:
                await call.answer("Не выбрано ни одной школы.", show_alert=True)
                return
        pairs = await dev_broadcast_svc.recipients(s, school_ids, audience_type)
        if not pairs:
            # Phase 12 states sub-pass: 0 получателей блокирует send, а не
            # создаёт пустой Broadcast молча — на этом масштабе (мис-конфиг
            # scope/audience) это почти всегда ошибка пользователя, не
            # осмысленное «разослать никому».
            await call.answer("Получателей 0 — проверь выбор школ/аудитории.", show_alert=True)
            return
        admin = await admins_svc.get_by_tg_id(s, tg.id)
        # Строку "sending" ставим только теперь, когда прошли все проверки —
        # неудачная валидация не должна оставлять диалог заблокированным для
        # повторной (уже корректной) попытки.
        manager.dialog_data["sending"] = True
        body = f"{ui.BROADCAST} Сообщение от команды Dance Pulse:\n\n{escape(text)}"
        broadcast = await broadcasts_svc.create_broadcast(
            s, scope_type=scope_type,
            scope_payload={"school_ids": school_ids},
            audience_type=audience_type, text=text,
            created_by_admin_id=admin.id if admin else None,
        )
        payload = outbox_svc.build_payload(
            body, parse_mode=ParseMode.HTML.value, kind="dev_broadcast",
        )
        for recipient_id, tg_user_id in pairs:
            key = f"devbroadcast:{broadcast.id}:{recipient_id}"
            await outbox_svc.enqueue(s, key, tg_user_id, payload, broadcast_id=broadcast.id)
        recipient_count = len(pairs)
        broadcast_id = broadcast.id

    manager.dialog_data["broadcast_id"] = broadcast_id
    if call.message:
        count_phrase = ui.plural_with_count(recipient_count, *_recipient_nouns(audience_type))
        await call.message.answer(f"{ui.BROADCAST} Рассылка поставлена в очередь: {count_phrase}.")
    await manager.switch_to(DevBroadcastSG.progress)


# ── progress ─────────────────────────────────────────────────────────


async def _progress_getter(dialog_manager: DialogManager, **_: Any) -> dict:
    broadcast_id = dialog_manager.dialog_data.get("broadcast_id")
    if broadcast_id is None:
        return {"text": "Рассылка не найдена."}
    async with session_scope() as s:
        counts = await broadcasts_svc.progress(s, int(broadcast_id))
    text = ui.card(
        ui.title(ui.BROADCAST, "Глобальная рассылка"),
        body=[
            f"{ui.YES} доставлено: {counts['sent']}",
            f"{ui.PENDING} в очереди: {counts['pending']}",
            f"{ui.UNDELIVERED} не доставлено: {counts['terminal_failed']}",
            f"всего: {counts['total']}",
        ],
    )
    return {"text": text}


async def _on_progress_refresh(
    call: CallbackQuery, _btn: Button, _manager: DialogManager
) -> None:
    await call.answer("Обновлено")


async def _on_progress_done(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    await manager.done()


dev_broadcast_dialog = Dialog(
    Window(
        Format(
            "{ui_broadcast} Глобальная рассылка. Каким школам?"
        ),
        Button(Const("🌍 Все школы"), id="scope_all", on_click=_on_scope_all),
        SwitchTo(Const("🏫 Выбрать школы"), id="scope_pick_btn", state=DevBroadcastSG.scope_pick),
        Cancel(Const("⏭ Отмена"), on_click=on_cancel_notify),
        getter=lambda **_: {"ui_broadcast": ui.BROADCAST},
        state=DevBroadcastSG.scope,
    ),
    Window(
        Const("Отметь школы и нажми «Готово»."),
        Multiselect(
            Format("✅ {item[1]}"),
            Format("{item[1]}"),
            id="dbc_school_select",
            item_id_getter=lambda item: str(item[0]),
            items="schools",
        ),
        Button(Const("Готово ▶"), id="scope_pick_done", on_click=_on_scope_pick_done),
        SwitchTo(Const("◀ Назад"), id="scope_pick_back", state=DevBroadcastSG.scope),
        getter=_scope_pick_getter,
        state=DevBroadcastSG.scope_pick,
    ),
    Window(
        Const("Кому конкретно — все ученики выбранных школ, или только преподаватели?"),
        Button(Const("👥 Ученики"), id="aud_person", on_click=_on_audience_pick),
        Button(Const("👤 Преподаватели"), id="aud_admin", on_click=_on_audience_pick),
        SwitchTo(Const("◀ Назад"), id="aud_back", state=DevBroadcastSG.scope),
        state=DevBroadcastSG.audience,
    ),
    Window(
        Const("Напиши текст сообщения одной или несколькими строками."),
        TextInput(id="dbc_text", on_success=_on_text_typed),
        Cancel(Const("⏭ Отмена"), on_click=on_cancel_notify),
        state=DevBroadcastSG.text,
    ),
    Window(
        Format(
            "{ui_broadcast} Уйдёт {count_phrase} ({scope_desc}):\n\n<i>{text}</i>"
        ),
        Button(Const("📨 Отправить"), id="dbc_send", on_click=_on_dev_broadcast_send),
        Cancel(Const("◀ Отмена"), on_click=on_cancel_notify),
        getter=_confirm_getter,
        parse_mode=ParseMode.HTML,
        state=DevBroadcastSG.confirm,
    ),
    Window(
        Format("{text}"),
        Button(Const("🔄 Обновить"), id="dbc_refresh", on_click=_on_progress_refresh),
        Button(Const("✅ Готово"), id="dbc_done", on_click=_on_progress_done),
        getter=_progress_getter,
        parse_mode=ParseMode.HTML,
        state=DevBroadcastSG.progress,
    ),
)


__all__ = ["dev_broadcast_dialog"]
