"""/pool — личный пул саппортов препода + ссылка-приглашение саппорта +
личные настройки автодобора.

Пул per-teacher: уровни хранятся в SupportPoolEntry(admin_id, person_id, level).
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    EVENING_TIMES,
    MENU_POOL,
    TOPUP_HOURS,
    lvl_from_code,
    pool_keyboard,
    recruit_config_keyboard,
)
from app.bot.utils import get_bot_username
from app.db.models import Person
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc

log = logging.getLogger(__name__)
router = Router()

_MAX_LEVEL = 3
_POOL_HEAD = (
    "🆘 Личный пул саппортов.\n"
    "Уровень — приоритет в picker'е при автодоборе: "
    "ур.3 первым, ур.1 последним.\n"
    "Тап по человеку: 1 → 2 → 3 → убрать из пула."
)


async def _pool_view(s, admin_id: int) -> tuple[list[Person], dict[int, int]]:
    people = await persons_svc.admin_visible_persons(s, admin_id)
    levels = await persons_svc.admin_pool_levels(s, admin_id)
    return people, levels


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
        people, levels = await _pool_view(s, teacher.id)
    if not people:
        await message.answer(
            "Пул пуст. Пригласи саппорта ссылкой (кнопка ниже) или отметь "
            "учеников, когда запишутся.",
            reply_markup=pool_keyboard([], {}),
        )
        return
    await message.answer(_POOL_HEAD, reply_markup=pool_keyboard(people, levels))


async def _rerender_pool(call: CallbackQuery, teacher_id: int, page: int = 0) -> None:
    async with session_scope() as s:
        people, levels = await _pool_view(s, teacher_id)
    if call.message:
        await call.message.edit_text(
            _POOL_HEAD, reply_markup=pool_keyboard(people, levels, page=page)
        )


@router.callback_query(F.data.startswith("tpool:"))
async def cb_pool_toggle(call: CallbackQuery) -> None:
    """Tap в /pool: cycle 1→2→3→None в ЛИЧНОМ пуле. idempotency через expected."""
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
        levels = await persons_svc.admin_pool_levels(s, teacher.id)
        current = levels.get(person_id)
        stale = len(parts) > 2 and current != expected
        if not stale:
            new_level = None if current == _MAX_LEVEL else (current or 0) + 1
            await persons_svc.set_pool_level(s, teacher.id, person_id, new_level)
        people, levels = await _pool_view(s, teacher.id)
    if call.message:
        try:
            await call.message.edit_reply_markup(
                reply_markup=pool_keyboard(people, levels, page=page)
            )
        except Exception as e:  # noqa: BLE001
            log.debug("pool edit_reply_markup: %s", e)
    await call.answer("Уже обновлено" if stale else "")


@router.callback_query(F.data.startswith("poolpg:"))
async def cb_pool_page(call: CallbackQuery) -> None:
    page = int((call.data or "").split(":", 1)[1])
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        people, levels = await _pool_view(s, teacher.id)
    if call.message:
        await call.message.edit_reply_markup(
            reply_markup=pool_keyboard(people, levels, page=page)
        )
    await call.answer()


@router.callback_query(F.data == "poolback")
async def cb_pool_back(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        tid = teacher.id
    await _rerender_pool(call, tid)
    await call.answer()


@router.callback_query(F.data == "poolinvite")
async def cb_pool_invite(call: CallbackQuery, bot: Bot) -> None:
    """Персональная переиспользуемая ссылка-приглашение саппорта."""
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        code = await admins_svc.get_or_create_support_code(s, teacher.id)
    username = await get_bot_username(bot)
    link = f"https://t.me/{username}?start=support_{code}"
    if call.message:
        await call.message.answer(
            "📨 Ссылка-приглашение саппорта. Перешли человеку — он откроет, "
            "укажет имя и пол и попадёт сразу в твой пул (без записи в группу):\n"
            f"{link}"
        )
    await call.answer()


# ── личные настройки автодобора ──────────────────────────────────────


def _topup_line(topup: int) -> str:
    return (
        f"☀️ В день занятия за {topup} ч добью остаток."
        if topup > 0
        else "☀️ Добор в день занятия выключен."
    )


async def _render_recruit_config(call: CallbackQuery, teacher) -> None:
    text = (
        "⚙ <b>Автодобор саппортов</b> (твои настройки)\n\n"
        f"🌙 Накануне в <b>{teacher.recruit_evening_time}</b> покажу баланс пар "
        "и кого позвать.\n"
        f"{_topup_line(teacher.support_cutoff_hours)}\n"
        f"⚖️ Зову, если без пары останутся ≥ <b>{teacher.support_threshold}</b> чел.\n\n"
        "Строки ниже: ⚖️ перекос · 🌙 разбор · ☀️ добор."
    )
    if call.message:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=recruit_config_keyboard(
                teacher.support_threshold,
                teacher.recruit_evening_time,
                teacher.support_cutoff_hours,
            ),
        )


@router.callback_query(F.data == "poolcfg")
async def cb_pool_settings(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        await _render_recruit_config(call, teacher)
    await call.answer()


@router.callback_query(F.data.startswith("rcfgt:"))
async def cb_cfg_threshold(call: CallbackQuery) -> None:
    value = int((call.data or "").split(":")[1])
    if value < 1 or value > 5:
        await call.answer()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        if teacher.support_threshold != value:
            await admins_svc.set_support_threshold(s, teacher.id, value)
            await s.refresh(teacher)
        await _render_recruit_config(call, teacher)
    await call.answer(f"Перекос: {value}")


@router.callback_query(F.data.startswith("rcfgc:"))
async def cb_cfg_topup(call: CallbackQuery) -> None:
    value = int((call.data or "").split(":")[1])
    if value not in TOPUP_HOURS:
        await call.answer()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        if teacher.support_cutoff_hours != value:
            await admins_svc.set_support_cutoff_hours(s, teacher.id, value)
            await s.refresh(teacher)
        await _render_recruit_config(call, teacher)
    await call.answer("Добор выключен" if value == 0 else f"Добор: за {value} ч")


@router.callback_query(F.data.startswith("rcfge:"))
async def cb_cfg_evening(call: CallbackQuery) -> None:
    raw = (call.data or "").split(":")[1]
    value = f"{raw[:2]}:{raw[2:]}"
    if value not in EVENING_TIMES:
        await call.answer()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        if teacher.recruit_evening_time != value:
            await admins_svc.set_recruit_evening_time(s, teacher.id, value)
            await s.refresh(teacher)
        await _render_recruit_config(call, teacher)
    await call.answer(f"Разбор: {value}")
