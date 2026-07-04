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

from app.bot import ui
from app.bot.commands import grant_admin_commands
from app.bot.dialogs import RenameSchoolSG
from app.bot.handlers._access import demote_to_student_if_empty
from app.bot.keyboards import (
    MENU_SCHOOL,
    TZ_OPTIONS,
    admin_menu_keyboard,
    confirm_school_delete_keyboard,
    confirm_school_leave_keyboard,
    groups_keyboard,
    school_accept_keyboard,
    school_card_keyboard,
    school_picker_keyboard,
    school_tz_keyboard,
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
        ui.title(ui.SCHOOL, escape(school.name)),
        f"{ui.GROUP} {ui.plural_with_count(group_count, 'группа', 'группы', 'групп')}",
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
    text = f"{ui.SCHOOL} Твои школы — выбери, какую открыть:"
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


@router.callback_query(F.data.startswith("schgroups:"))
async def cb_school_groups(call: CallbackQuery) -> None:
    """Все группы школы (любому члену). Tap → карточка группы."""
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_member(s, school_id, admin.id):
            await call.answer("Нет доступа", show_alert=True)
            return
        groups = await groups_svc.school_groups(s, school_id)
    if call.message:
        if groups:
            await call.message.edit_text(
                f"{ui.GROUP} Группы школы:",
                reply_markup=groups_keyboard(groups, origin_school_id=school_id),
            )
        else:
            await call.message.edit_text(
                "В школе пока нет групп. Owner создаёт их кнопкой «➕ Новая группа»."
            )
    await call.answer()


@router.callback_query(F.data.startswith("schnewgroup:"))
async def cb_school_new_group(
    call: CallbackQuery, dialog_manager: DialogManager
) -> None:
    """Старт мастера новой группы из карточки школы. Группу создаёт любой член
    школы (owner или co-admin) — мастер заведёт её именно в этой школе."""
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_member(s, school_id, admin.id):
            await call.answer("Нет доступа к школе", show_alert=True)
            return
    from app.bot.dialogs import NewGroupSG
    await call.answer()
    await dialog_manager.start(
        NewGroupSG.style, mode=StartMode.RESET_STACK, data={"school_id": school_id}
    )


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


async def _render_school_tz(call: CallbackQuery, school_id: int) -> None:
    async with session_scope() as s:
        current = await schools_svc.get_timezone(s, school_id)
        school = await schools_svc.get_school(s, school_id)
        school_name = school.name if school else "—"
    label_map = dict(TZ_OPTIONS)
    current_label = label_map.get(current, current)
    text = (
        f"{ui.TIMEZONE} <b>Часовой пояс школы «{escape(school_name)}»</b>\n\n"
        f"Сейчас: <b>{escape(current_label)}</b>\n\n"
        "Расписание (Пн/Вт/19:00) и отображение всех времён — в этом TZ. "
        "Тап чтобы сменить."
    )
    if call.message:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=school_tz_keyboard(school_id, current),
        )


@router.callback_query(F.data.startswith("schtz:"))
async def cb_school_tz(call: CallbackQuery) -> None:
    school_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может менять TZ", show_alert=True)
            return
    await _render_school_tz(call, school_id)
    await call.answer()


@router.callback_query(F.data.startswith("schtzset:"))
async def cb_school_tz_set(call: CallbackQuery, bot: Bot) -> None:
    parts = (call.data or "").split(":", 2)
    school_id = int(parts[1])
    tz_name = parts[2]
    # Проверяем что tz_name в whitelist'е TZ_OPTIONS — пользователь не должен
    # подсунуть произвольный callback с битым TZ.
    if tz_name not in dict(TZ_OPTIONS):
        await call.answer()
        return
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if admin is None or not await schools_svc.is_owner(s, school_id, admin.id):
            await call.answer("Только owner может менять TZ", show_alert=True)
            return
        current = await schools_svc.get_timezone(s, school_id)
        if current == tz_name:
            await call.answer("Уже выбрано")
            return
        notify_tgs = await schools_svc.set_timezone(s, school_id, tz_name)
    await _render_school_tz(call, school_id)
    await call.answer(f"TZ: {tz_name}")
    if notify_tgs:
        text = (
            "⚠️ Смена часового пояса школы закрыла открытые опросы по старому "
            "времени — новые уйдут по актуальному расписанию."
        )
        for tg in notify_tgs:
            try:
                await bot.send_message(tg, text)
            except Exception as e:  # noqa: BLE001
                log.warning("TZ reschedule-notice to %s failed: %s", tg, e)


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
        f"В ней {ui.plural_with_count(group_count, 'группа', 'группы', 'групп')} — "
        f"они тоже будут удалены вместе с "
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
    await call.message.edit_text(f"🗑 Школа «{name}» удалена.")
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
    await call.message.edit_text(f"↩ Вышел из школы «{name}».")
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
            f"✅ Готово, ты теперь co-админ школы «{name}»."
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
