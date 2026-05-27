"""Shared access-control helpers для admin handlers, разнесённых по модулям.

Здесь живут чисто-функциональные «получи группу если admin может её видеть»
и «получи группу если admin — owner». Содержание дублирует то что было
в admin.py до C1-распила; импорт-цикл не возникает (этот модуль ничего
не импортирует из handlers/)."""
from __future__ import annotations

import logging

from aiogram import Bot

from app.bot.commands import revoke_admin_commands
from app.bot.keyboards import student_menu_keyboard
from app.db.models import Group
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services.schedule import local_now, upcoming_lessons

log = logging.getLogger(__name__)


async def accessible_group(session, group_id: int, tg_user_id: int) -> Group | None:
    """Group если caller — member школы группы; None иначе. Для view-actions:
    карточка, ростер, ping, /today. Для destructive — `owned_group`."""
    teacher = await admins_svc.get_by_tg_id(session, tg_user_id)
    if teacher is None:
        return None
    group = await groups_svc.get_group(session, group_id)
    if group is None or group.school_id is None:
        return None
    if not await schools_svc.is_member(session, group.school_id, teacher.id):
        return None
    return group


async def owned_group(session, group_id: int, tg_user_id: int) -> Group | None:
    """Group если caller — owner школы группы. Для destructive actions
    (delete, edit settings)."""
    teacher = await admins_svc.get_by_tg_id(session, tg_user_id)
    if teacher is None:
        return None
    group = await groups_svc.get_group(session, group_id)
    if group is None or group.school_id is None:
        return None
    if not await schools_svc.is_owner(session, group.school_id, teacher.id):
        return None
    return group


async def is_owner_of_group_id(session, group_id: int, admin_id: int) -> bool:
    """True если admin владеет школой группы. Принимает admin.id, не tg_user_id."""
    group = await groups_svc.get_group(session, group_id)
    if group is None or group.school_id is None:
        return False
    return await schools_svc.is_owner(session, group.school_id, admin_id)


async def group_card_extras(session, group: Group) -> tuple[bool, bool]:
    """(has_upcoming, has_closed_surveys) — для скрытия мёртвых кнопок
    в group_actions_keyboard."""
    has_upcoming = bool(
        upcoming_lessons(group.weekdays, group.lesson_time, local_now(), n=1)
    )
    closed = await surveys_svc.recent_closed_surveys(session, group.id, limit=1)
    return has_upcoming, bool(closed)


async def demote_to_student_if_empty(bot: Bot, admin_id: int, tg_id: int) -> None:
    """Если у admin'а не осталось школ — полностью снимаем роль: удаляем
    Admin-запись, revoke slash-меню и пушим student reply-меню.

    Удаляем полностью, чтобы при повторном /grant или partner-invite человек
    проходил онбординг заново как новенький, а не «оживал» со старым именем
    и поднятым has_completed_setup."""
    async with session_scope() as s:
        schools = await schools_svc.admin_schools(s, admin_id)
        if schools:
            return
        await admins_svc.delete_admin(s, admin_id)
    try:
        await revoke_admin_commands(bot, tg_id)
    except Exception as e:  # noqa: BLE001
        log.warning("revoke admin cmds for %s failed: %s", tg_id, e)
    try:
        await bot.send_message(
            tg_id,
            "Ты больше не админ ни в одной школе — возвращаю обычное меню. "
            "Если тебя снова пригласят, открой ссылку и нажми «✅ Принять».",
            reply_markup=student_menu_keyboard(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("push student menu to %s failed: %s", tg_id, e)
