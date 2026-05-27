"""School management: карточка, конфиг автодобора, рейн/делете/лив,
partner-invite принятие. Извлечено из admin.py в рамках C1."""
from __future__ import annotations

import logging
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, StartMode

from app.bot.commands import grant_admin_commands
from app.bot.dialogs import RenameSchoolSG
from app.bot.handlers._access import demote_to_student_if_empty
from app.bot.keyboards import (
    MENU_SCHOOL,
    admin_menu_keyboard,
    confirm_school_delete_keyboard,
    confirm_school_leave_keyboard,
    school_accept_keyboard,
    school_card_keyboard,
    school_config_keyboard,
    school_picker_keyboard,
)
from app.bot.utils import get_bot_username
from app.core.config import settings
from app.db.models import Admin
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc

log = logging.getLogger(__name__)
router = Router()


async def _partner_link(bot: Bot, code: str) -> str:
    username = await get_bot_username(bot)
    return f"https://t.me/{username}?start=partner_{code}"


async def _render_school_card(
    target: Message, school_id: int, admin_id: int, *, edit: bool = True
) -> None:
    """edit=True — для callback'ов с inline-keyboard; edit=False — для /school
    или нажатий по reply-меню (новым сообщением)."""
    async with session_scope() as s:
        school = await schools_svc.get_school(s, school_id)
        if school is None:
            return
        role = await schools_svc.get_role(s, school_id, admin_id)
        if role is None:
            return
        members = await schools_svc.members_of(s, school_id)
        all_schools = await schools_svc.admin_schools(s, admin_id)
        group_count = len(await groups_svc.school_groups(s, school_id))
    is_owner = role.value == "owner"
    lines = [
        f"🏫 <b>{escape(school.name)}</b>",
        f"Групп: {group_count}",
    ]
    if is_owner and school.name == schools_svc.DEFAULT_NAME:
        lines.append("")
        lines.append("👉 Школа называется по умолчанию — переименуй её через")
        lines.append("«✏️ Переименовать школу» ниже.")
    lines.append("")
    lines.append("Состав:")
    for admin, r in members:
        glyph = "👑 owner" if r.value == "owner" else "🛠 co-admin"
        lines.append(f"• {escape(admin.full_name)} — {glyph}")
    text = "\n".join(lines)
    multi = len(all_schools) > 1
    markup = school_card_keyboard(school_id, is_owner, multi_school=multi)
    if edit:
        await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await target.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _render_school_list(
    target: Message, admin_id: int, *, edit: bool = False
) -> None:
    async with session_scope() as s:
        all_schools = await schools_svc.admin_schools(s, admin_id)
    if not all_schools:
        return
    if len(all_schools) == 1:
        school, _ = all_schools[0]
        await _render_school_card(target, school.id, admin_id, edit=edit)
        return
    text = "🏫 Твои школы — выбери, какую открыть:"
    markup = school_picker_keyboard(all_schools)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(Command("school"))
@router.message(F.text == MENU_SCHOOL)
async def cmd_school(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, tg.id)
    if admin is None:
        return
    await _render_school_list(message, admin.id, edit=False)


@router.callback_query(F.data == "schlist")
async def cb_school_list(call: CallbackQuery) -> None:
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
    if admin is None or call.message is None:
        await call.answer()
        return
    await _render_school_list(call.message, admin.id, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("schcard:"))
async def cb_school_card(call: CallbackQuery) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
    if admin is None or call.message is None:
        await call.answer()
        return
    await _render_school_card(call.message, school_id, admin.id, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("schlink:"))
async def cb_school_invite_link(call: CallbackQuery, bot: Bot) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может приглашать", show_alert=True)
            return
        invite = await schools_svc.create_invite(s, school_id)
        code = invite.code
        expires_at = invite.expires_at
        school = await schools_svc.get_school(s, school_id)
        school_name = school.name if school else "—"
    link = await _partner_link(bot, code)
    expires_local = expires_at.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    await call.message.answer(
        f"🛠 Ссылка-приглашение co-админом школы «{escape(school_name)}» — "
        f"действует до {expires_local}. Перешли её одному человеку:\n\n"
        f"<code>{escape(link)}</code>",
        parse_mode=ParseMode.HTML,
    )
    await call.answer("Ссылка готова — пересылай")


@router.callback_query(F.data.startswith("schrem:"))
async def cb_school_remove_member(call: CallbackQuery, bot: Bot) -> None:
    parts = (call.data or "").split(":")
    school_id, admin_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        me = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if me is None or not await schools_svc.is_owner(s, school_id, me.id):
            await call.answer("Только owner может убирать", show_alert=True)
            return
        removed_admin = await s.get(Admin, admin_id)
        removed_tg = removed_admin.tg_user_id if removed_admin else None
        removed = await schools_svc.remove_coadmin(s, school_id, admin_id)
    if removed:
        await call.answer("Убран")
        if removed_tg is not None:
            await demote_to_student_if_empty(bot, admin_id, removed_tg)
    else:
        await call.answer("Уже не co-admin", show_alert=True)
    if call.message is not None:
        await _render_school_card(call.message, school_id, me.id, edit=True)


async def _render_school_config(call: CallbackQuery, school_id: int) -> None:
    """Перерисовать экран настроек автодобора. threshold/cutoff живут на
    owner'е школы — берём их оттуда."""
    async with session_scope() as s:
        owner_tg = await schools_svc.get_owner_tg_id(s, school_id)
        owner = await admins_svc.get_by_tg_id(s, owner_tg) if owner_tg else None
        if owner is None:
            await call.answer()
            return
        threshold = owner.support_threshold
        cutoff = owner.support_cutoff_hours
    text = (
        "⚙ <b>Настройки автодобора саппортов</b>\n\n"
        f"Перекос пар (при какой разнице звать): <b>{threshold}</b>\n"
        f"За сколько часов до занятия проверяем: <b>{cutoff} ч</b>\n\n"
        "Тап на цифру — изменить."
    )
    if call.message:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=school_config_keyboard(school_id, threshold, cutoff),
        )


@router.callback_query(F.data.startswith("schcfg:"))
async def cb_school_config(call: CallbackQuery) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может менять настройки", show_alert=True)
            return
    await _render_school_config(call, school_id)
    await call.answer()


@router.callback_query(F.data.startswith("schcfgt:"))
async def cb_school_cfg_threshold(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    school_id, value = int(parts[1]), int(parts[2])
    if value < 1 or value > 5:
        await call.answer()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer()
            return
        owner_tg = await schools_svc.get_owner_tg_id(s, school_id)
        owner = await admins_svc.get_by_tg_id(s, owner_tg) if owner_tg else None
        if owner is None:
            await call.answer()
            return
        if owner.support_threshold == value:
            await call.answer("Уже выбрано")
            return
        await admins_svc.set_support_threshold(s, owner.id, value)
    await _render_school_config(call, school_id)
    await call.answer(f"Перекос: {value}")


@router.callback_query(F.data.startswith("schcfgc:"))
async def cb_school_cfg_cutoff(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    school_id, value = int(parts[1]), int(parts[2])
    if value < 1 or value > 48:
        await call.answer()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer()
            return
        owner_tg = await schools_svc.get_owner_tg_id(s, school_id)
        owner = await admins_svc.get_by_tg_id(s, owner_tg) if owner_tg else None
        if owner is None:
            await call.answer()
            return
        if owner.support_cutoff_hours == value:
            await call.answer("Уже выбрано")
            return
        await admins_svc.set_support_cutoff_hours(s, owner.id, value)
    await _render_school_config(call, school_id)
    await call.answer(f"Отсечка: {value} ч")


@router.callback_query(F.data.startswith("schren:"))
async def cb_school_rename_start(
    call: CallbackQuery, dialog_manager: DialogManager
) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может переименовывать", show_alert=True)
            return
    await dialog_manager.start(
        RenameSchoolSG.name,
        data={"school_id": school_id},
        mode=StartMode.RESET_STACK,
    )
    await call.answer()


@router.callback_query(F.data.startswith("schdel:"))
async def cb_school_delete_confirm(call: CallbackQuery) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может удалять школу", show_alert=True)
            return
        school = await schools_svc.get_school(s, school_id)
        group_count = len(await groups_svc.school_groups(s, school_id))
        name = school.name if school else "—"
    await call.message.edit_text(
        f"⚠️ Удалить школу «{escape(name)}»?\n"
        f"Внутри {group_count} групп(ы) — они тоже будут удалены вместе с "
        f"учениками, опросами и пулом саппортов. Это необратимо.",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_school_delete_keyboard(school_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("schdelok:"))
async def cb_school_delete(call: CallbackQuery, bot: Bot) -> None:
    school_id = int(call.data.split(":", 1)[1])
    affected_tgs: list[tuple[int, int]] = []
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может удалять школу", show_alert=True)
            return
        school = await schools_svc.get_school(s, school_id)
        if school is None:
            await call.answer("Уже удалена", show_alert=True)
            return
        name = school.name
        members = await schools_svc.members_of(s, school_id)
        affected_tgs = [(a.id, a.tg_user_id) for a, _ in members]
        await schools_svc.delete_school(s, school)
    await call.message.edit_text(f"🗑 Школа «{escape(name)}» удалена.")
    await call.answer()
    for admin_id, tg_id in affected_tgs:
        await demote_to_student_if_empty(bot, admin_id, tg_id)


@router.callback_query(F.data.startswith("schleave:"))
async def cb_school_leave_confirm(call: CallbackQuery) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        school = await schools_svc.get_school(s, school_id)
        if school is None:
            await call.answer("Школа не найдена", show_alert=True)
            return
        name = school.name
    await call.message.edit_text(
        f"Выйти из школы «{escape(name)}»? Твои собственные данные не "
        f"пропадут — ты просто перестанешь видеть её группы.",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_school_leave_keyboard(school_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("schleaveok:"))
async def cb_school_leave(call: CallbackQuery, bot: Bot) -> None:
    school_id = int(call.data.split(":", 1)[1])
    admin_id_to_demote: int | None = None
    tg_to_demote: int | None = None
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None:
            return
        role = await schools_svc.get_role(s, school_id, admin.id)
        if role is None:
            await call.answer("Ты не в этой школе", show_alert=True)
            return
        if role.value == "owner":
            await call.answer(
                "Owner не может выйти; удали школу или передай владение.",
                show_alert=True,
            )
            return
        await schools_svc.remove_coadmin(s, school_id, admin.id)
        school = await schools_svc.get_school(s, school_id)
        name = school.name if school else "—"
        admin_id_to_demote = admin.id
        tg_to_demote = call.from_user.id
    await call.message.edit_text(f"↩ Вышел из школы «{escape(name)}».")
    if admin_id_to_demote is not None and tg_to_demote is not None:
        await demote_to_student_if_empty(bot, admin_id_to_demote, tg_to_demote)
    await call.answer()


async def handle_school_invite_start(message: Message, code: str) -> None:
    """Точка входа из student.join_start при partner_<code>. Показывает
    карточку школы и кнопку «Принять» — ничего не пишем в БД до подтверждения."""
    async with session_scope() as s:
        invite = await schools_svc.get_invite(s, code)
        if invite is None or not schools_svc.invite_is_valid(invite):
            await message.answer(
                "Эта ссылка устарела или уже использована. Попроси у owner'а новую."
            )
            return
        school = await schools_svc.get_school(s, invite.school_id)
        if school is None:
            await message.answer("Школа не найдена — возможно, удалена.")
            return
        name = school.name
    await message.answer(
        f"🛠 Тебя приглашают co-админом школы «{escape(name)}». "
        f"Co-admin видит все группы школы, может управлять учениками и "
        f"опросами, но не может удалить школу.\n\nПринимаешь?",
        parse_mode=ParseMode.HTML,
        reply_markup=school_accept_keyboard(code),
    )


@router.callback_query(F.data.startswith("schaccept:"))
async def cb_school_accept(call: CallbackQuery, bot: Bot) -> None:
    code = (call.data or "").split(":", 1)[1]
    tg = call.from_user
    if not tg:
        return
    async with session_scope() as s:
        invite = await schools_svc.get_invite(s, code)
        if invite is None or not schools_svc.invite_is_valid(invite):
            if call.message:
                await call.message.edit_text(
                    "Эта ссылка устарела или уже использована."
                )
            await call.answer()
            return
        admin = await admins_svc.get_or_create(
            s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
        )
        await schools_svc.add_coadmin(s, invite.school_id, admin.id)
        await schools_svc.consume_invite(s, invite, admin.id)
        admin.has_completed_setup = True
        await s.flush()
        school = await schools_svc.get_school(s, invite.school_id)
        name = school.name if school else "—"
    try:
        await grant_admin_commands(bot, tg.id)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not set admin commands for %s: %s", tg.id, e)
    if call.message:
        await call.message.edit_text(
            f"✅ Готово, ты теперь co-админ школы «{escape(name)}»."
        )
    try:
        await bot.send_message(
            tg.id,
            "Открой меню снизу 👇 — там есть «📋 Мои группы», «🏫 Школа» и всё остальное.",
            reply_markup=admin_menu_keyboard(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not push admin menu to %s: %s", tg.id, e)
    await call.answer()


@router.callback_query(F.data == "schdecline")
async def cb_school_decline(call: CallbackQuery) -> None:
    if call.message:
        await call.message.edit_text("Окей, приглашение отклонено.")
    await call.answer()
