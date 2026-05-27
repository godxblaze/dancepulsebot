"""/pool — личный пул саппортов: cmd_pool, cb_pool_toggle, cb_pool_page.

Выделено из [admin.py](admin.py) в рамках C1.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MENU_POOL, lvl_from_code, pool_keyboard
from app.db.models import Person
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc

log = logging.getLogger(__name__)
router = Router()

_MAX_LEVEL = 3


@router.message(Command("pool"))
@router.message(F.text == MENU_POOL)
async def cmd_pool(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            return
        people = await persons_svc.admin_visible_persons(s, teacher.id)
    if not people:
        await message.answer(
            "Пул саппортов пуст — сюда попадут ученики, когда запишутся. "
            "Потом тапом задашь, кого и с каким уровнем можно звать на подмену."
        )
        return
    await message.answer(
        "🆘 Личный пул саппортов.\n"
        "Уровень — приоритет в picker'е при автодоборе: "
        "ур.3 показывается первым, ур.1 — последним.\n"
        "Тап по человеку: 1 → 2 → 3 → убрать из пула.",
        reply_markup=pool_keyboard(people),
    )


@router.callback_query(F.data.startswith("tpool:"))
async def cb_pool_toggle(call: CallbackQuery) -> None:
    """Tap в /pool: cycle 1→2→3→None. idempotency-guard через expected в
    callback_data; 4-й сегмент — текущая страница для возврата."""
    parts = (call.data or "").split(":")
    person_id = int(parts[1])
    expected = lvl_from_code(parts[2]) if len(parts) > 2 else None
    page = int(parts[3]) if len(parts) > 3 else 0
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        person = await s.get(Person, person_id)
        if teacher is None or person is None or person.school_id is None:
            await call.answer("Не найдено", show_alert=True)
            return
        if not await schools_svc.is_member(s, person.school_id, teacher.id):
            await call.answer("Не найдено", show_alert=True)
            return
        current = person.support_level
        stale = len(parts) > 2 and current != expected
        if not stale:
            new_level = None if current == _MAX_LEVEL else (current or 0) + 1
            await persons_svc.set_support_level(s, person_id, new_level)
        people = await persons_svc.admin_visible_persons(s, teacher.id)
    # Перерисовываем клавиатуру с актуальным state в любом случае: при stale —
    # чтобы юзер увидел изменения предыдущего тапа, при не-stale — нормальный
    # update. Иначе быстрый кликер видит «зависшее» сообщение.
    if call.message:
        try:
            await call.message.edit_reply_markup(
                reply_markup=pool_keyboard(people, page=page)
            )
        except Exception as e:  # noqa: BLE001
            # «message is not modified» если keyboard идентичен — это норма.
            log.debug("pool edit_reply_markup: %s", e)
    await call.answer("Уже обновлено" if stale else "")


@router.callback_query(F.data.startswith("poolpg:"))
async def cb_pool_page(call: CallbackQuery) -> None:
    """Переключение страницы в /pool."""
    page = int((call.data or "").split(":", 1)[1])
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        people = await persons_svc.admin_visible_persons(s, teacher.id)
    if call.message:
        await call.message.edit_reply_markup(reply_markup=pool_keyboard(people, page=page))
    await call.answer()
