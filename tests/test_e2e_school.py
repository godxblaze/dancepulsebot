"""E2E for the school management flow — card, timezone, co-admin invite/accept,
leave, remove, delete. Every tap goes through the real school.py handlers."""
from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.school import (
    cb_school_accept,
    cb_school_card,
    cb_school_decline,
    cb_school_delete,
    cb_school_delete_confirm,
    cb_school_groups,
    cb_school_invite_link,
    cb_school_leave,
    cb_school_leave_confirm,
    cb_school_list,
    cb_school_members,
    cb_school_remove_member,
    cb_school_tz,
    cb_school_tz_set,
    cmd_school,
    handle_school_invite_start,
)
from app.bot.handlers.student import join_start
from app.db.models import SchoolMemberRole
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from tests._e2e_helpers import TEACHER_TG, cb, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, SentMessage, fake_user


async def _accept_coadmin(bot: FakeBot, school_id: int, tg_id: int, name: str) -> int:
    """Run an outsider through the partner deep-link → accept flow. Returns their admin.id."""
    async with session_scope() as s:
        invite = await schools_svc.create_invite(s, school_id)
        code = invite.code
    user = fake_user(tg_id, full_name=name)
    # partner deep-link goes through student.join_start → handle_school_invite_start
    start = FakeMessage(user, bot=bot)
    await join_start(start, SimpleNamespace(args=f"partner_{code}"), FakeFSM(), bot)
    assert any("co-админ" in r.text for r in start.replies)
    # tap "✅ Принять"
    accept_msg = start.replies[-1]
    await cb_school_accept(cb(f"schaccept:{code}", tg_id, name=name, message=accept_msg), bot)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, tg_id)
        assert await schools_svc.get_role(s, school_id, admin.id) == SchoolMemberRole.coadmin
        return admin.id


# ── card, list, groups ───────────────────────────────────────────────


async def test_school_card_and_groups(db) -> None:
    bot = FakeBot()
    gid, sid, _ = await make_group()

    # /school → single school → card rendered as a fresh message
    msg = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_school(msg)
    assert msg.replies and "группа" in msg.replies[0].text

    # open the card explicitly via callback
    card = SentMessage(bot, TEACHER_TG, "")
    await cb_school_card(cb(f"schcard:{sid}", TEACHER_TG, message=card))
    assert "Состав" in card.text

    # groups of the school (has one)
    groups_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_school_groups(cb(f"schgroups:{sid}", TEACHER_TG, message=groups_msg))
    assert "Группы школы" in groups_msg.text

    # delete the only group → groups view shows the empty hint
    async with session_scope() as s:
        g = await groups_svc.get_group(s, gid)
        await groups_svc.delete_group(s, g)
    empty_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_school_groups(cb(f"schgroups:{sid}", TEACHER_TG, message=empty_msg))
    assert "пока нет групп" in empty_msg.text


async def test_school_timezone(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()

    tz_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_school_tz(cb(f"schtz:{sid}", TEACHER_TG, message=tz_msg))
    assert "Часовой пояс" in tz_msg.text

    # set a new TZ
    await cb_school_tz_set(
        cb(f"schtzset:{sid}:Europe/Berlin", TEACHER_TG, message=tz_msg), bot
    )
    async with session_scope() as s:
        assert await schools_svc.get_timezone(s, sid) == "Europe/Berlin"

    # setting the same again → "Уже выбрано", no error
    set_again = cb(f"schtzset:{sid}:Europe/Berlin", TEACHER_TG, message=tz_msg)
    await cb_school_tz_set(set_again, bot)
    assert any("Уже выбрано" in a[0] for a in set_again.answers)

    # garbage TZ outside whitelist → silently ignored
    await cb_school_tz_set(
        cb(f"schtzset:{sid}:Mars/Olympus", TEACHER_TG, message=tz_msg), bot
    )
    async with session_scope() as s:
        assert await schools_svc.get_timezone(s, sid) == "Europe/Berlin"


# ── invite / accept / multi-school / leave / remove ──────────────────


async def test_coadmin_full_lifecycle(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()

    # owner generates an invite link
    link_src = SentMessage(bot, TEACHER_TG, "")
    await cb_school_invite_link(cb(f"schlink:{sid}", TEACHER_TG, message=link_src), bot)
    assert any("Ссылка-приглашение" in r.text for r in link_src.replies)

    # outsider accepts → becomes co-admin
    coadmin_id = await _accept_coadmin(bot, sid, 200, "Маша")

    async with session_scope() as s:
        members = await schools_svc.members_of(s, sid)
        assert len(members) == 2

    # co-admin now owns their own auto-created school too → /school shows a picker
    picker = FakeMessage(fake_user(200, full_name="Маша"), bot=bot)
    await cmd_school(picker)
    assert picker.replies and "выбери" in picker.replies[0].text.lower()
    # cb_school_list re-renders the picker
    list_msg = SentMessage(bot, 200, "")
    await cb_school_list(cb("schlist", 200, name="Маша", message=list_msg))
    assert "школ" in list_msg.text.lower()

    # owner removes the co-admin
    card = SentMessage(bot, TEACHER_TG, "")
    rem = cb(f"schrem:{sid}:{coadmin_id}", TEACHER_TG, message=card)
    await cb_school_remove_member(rem, bot)
    assert any("Убран" in a[0] for a in rem.answers)
    async with session_scope() as s:
        assert await schools_svc.get_role(s, sid, coadmin_id) is None


async def test_schmembers_screen_reachable_from_card_and_removes(db) -> None:
    """Раньше schrem был недостижим из UI — school_members_keyboard никто не
    рендерил. Проверяем реальный путь: карточка → «Состав школы» → тап-удаление."""
    bot = FakeBot()
    _, sid, _ = await make_group()
    coadmin_id = await _accept_coadmin(bot, sid, 201, "Петя")

    # карточка школы содержит кнопку в «Состав школы»
    card = SentMessage(bot, TEACHER_TG, "")
    await cb_school_card(cb(f"schcard:{sid}", TEACHER_TG, message=card))
    members_btn = [
        b for row in card.reply_markup.inline_keyboard for b in row
        if b.callback_data == f"schmembers:{sid}"
    ]
    assert members_btn, "нет кнопки «Состав школы» в карточке"

    # тап по ней → экран состава с кнопкой убрать co-admin'а
    await cb_school_members(cb(f"schmembers:{sid}", TEACHER_TG, message=card))
    assert "Состав школы" in card.text
    rem_btn = [
        b for row in card.reply_markup.inline_keyboard for b in row
        if b.callback_data == f"schrem:{sid}:{coadmin_id}"
    ]
    assert rem_btn, "нет кнопки удаления co-admin'а на экране состава"

    # тап по ней — реально убирает
    await cb_school_remove_member(cb(f"schrem:{sid}:{coadmin_id}", TEACHER_TG, message=card), bot)
    async with session_scope() as s:
        assert await schools_svc.get_role(s, sid, coadmin_id) is None
    assert "Петя" not in card.text  # экран состава перерисован без него

    # co-admin не может открыть экран состава (owner-only guard)
    guard = cb(f"schmembers:{sid}", 999, message=SentMessage(bot, 999, ""))
    await cb_school_members(guard)
    assert any(a[1] for a in guard.answers)  # show_alert


async def test_remove_coadmin_clears_recruit_lead_and_group_admin(db) -> None:
    """Снятый co-admin не должен оставаться куратором добора/ответственным за
    группы школы — иначе он продолжает получать recruit-пинги, а реальные
    члены школы блокируются can_manage_recruit."""
    bot = FakeBot()
    gid, sid, _ = await make_group()
    coadmin_id = await _accept_coadmin(bot, sid, 202, "Настя")

    async with session_scope() as s:
        await groups_svc.set_recruit_lead(s, gid, coadmin_id)
        await groups_svc.set_group_admin(s, gid, coadmin_id, assigned=True)
        group = await groups_svc.get_group(s, gid)
        assert group.recruit_lead_admin_id == coadmin_id
        assert coadmin_id in await groups_svc.group_admin_ids(s, gid)

    rem = cb(f"schrem:{sid}:{coadmin_id}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_school_remove_member(rem, bot)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
        assert group.recruit_lead_admin_id is None  # fallback на owner'а школы
        assert coadmin_id not in await groups_svc.group_admin_ids(s, gid)
        lead = await groups_svc.resolve_recruit_lead(s, group)
        assert lead is not None and lead.tg_user_id == TEACHER_TG


async def test_admin_schools_owner_school_sorts_first(db) -> None:
    """admin_schools() обещает «owner-школа всегда первая» — SchoolMemberRole
    хранится строкой ("owner"/"coadmin"), обычный order_by дал бы "coadmin"
    раньше "owner" лексикографически (баг из аудита), нужен .desc()."""
    _, own_sid, _ = await make_group(teacher_tg=TEACHER_TG, teacher_name="Owner")
    _, other_sid, _ = await make_group(teacher_tg=901, teacher_name="Другой owner")
    async with session_scope() as s:
        owner = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        await schools_svc.add_coadmin(s, other_sid, owner.id)
        result = await schools_svc.admin_schools(s, owner.id)
    assert [school.id for school, _ in result] == [own_sid, other_sid]
    assert [role for _, role in result] == [
        SchoolMemberRole.owner, SchoolMemberRole.coadmin,
    ]


async def test_coadmin_leaves(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()
    coadmin_id = await _accept_coadmin(bot, sid, 201, "Лена")

    # confirm screen
    leave_msg = SentMessage(bot, 201, "")
    await cb_school_leave_confirm(cb(f"schleave:{sid}", 201, name="Лена", message=leave_msg))
    assert "Выйти из школы" in leave_msg.text

    # actually leave
    await cb_school_leave(cb(f"schleaveok:{sid}", 201, name="Лена", message=leave_msg), bot)
    assert "Вышел из школы" in leave_msg.text
    async with session_scope() as s:
        assert await schools_svc.get_role(s, sid, coadmin_id) is None


async def test_owner_cannot_leave(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()
    leave = cb(f"schleaveok:{sid}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_school_leave(leave, bot)
    assert any("Owner не может выйти" in a[0] for a in leave.answers)
    async with session_scope() as s:
        assert await schools_svc.get_role(s, sid, 1) == SchoolMemberRole.owner  # admin.id==1


async def test_invite_decline_and_expired(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()

    # decline path
    decline_msg = SentMessage(bot, 300, "")
    await cb_school_decline(cb("schdecline", 300, name="X", message=decline_msg))
    assert "отклонено" in decline_msg.text

    # bad / unknown invite code via deep-link
    bad = FakeMessage(fake_user(301, full_name="Y"), bot=bot)
    await handle_school_invite_start(bad, "nonexistent")
    assert any("устарела" in r.text for r in bad.replies)

    # accept a consumed invite → rejected
    async with session_scope() as s:
        owner = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        invite = await schools_svc.create_invite(s, sid)
        code = invite.code
        await schools_svc.consume_invite(s, invite, owner.id)
    consumed_msg = SentMessage(bot, 302, "")
    await cb_school_accept(cb(f"schaccept:{code}", 302, name="Z", message=consumed_msg), bot)
    assert "устарела" in consumed_msg.text


async def test_invite_double_accept_race_only_first_wins(db) -> None:
    """Двое приняли одну и ту же co-admin-ссылку одновременно — раньше оба
    консьюмились (check-then-act), оба становились co-admin'ами. consume_invite
    теперь атомарный UPDATE ... WHERE used_at IS NULL: второй вызов должен
    проиграть гонку независимо от того, что invite ещё выглядел валидным на
    момент чтения."""
    _, sid, _ = await make_group()
    async with session_scope() as s:
        invite = await schools_svc.create_invite(s, sid)
        admin_a = await admins_svc.get_or_create(s, 601, "a", "A")
        admin_b = await admins_svc.get_or_create(s, 602, "b", "B")
        # Оба «увидели» invite валидным до того, как кто-то его закрыл.
        assert schools_svc.invite_is_valid(invite)
        first = await schools_svc.consume_invite(s, invite, admin_a.id)
        second = await schools_svc.consume_invite(s, invite, admin_b.id)
    assert first is True
    assert second is False
    async with session_scope() as s:
        invite = await schools_svc.get_invite(s, invite.code)
        assert invite.used_by_admin_id == admin_a.id  # проигравший не перезаписал


async def test_school_delete(db) -> None:
    bot = FakeBot()
    gid, sid, _ = await make_group()

    confirm = SentMessage(bot, TEACHER_TG, "")
    await cb_school_delete_confirm(cb(f"schdel:{sid}", TEACHER_TG, message=confirm))
    assert "Удалить школу" in confirm.text

    await cb_school_delete(cb(f"schdelok:{sid}", TEACHER_TG, message=confirm), bot)
    assert "удалена" in confirm.text
    async with session_scope() as s:
        assert await schools_svc.get_school(s, sid) is None
        assert await groups_svc.get_group(s, gid) is None


async def test_foreign_admin_blocked(db) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()
    # an unrelated admin
    async with session_scope() as s:
        await admins_svc.get_or_create(s, 400, "x", "Чужой")

    for data, handler in (
        (f"schtz:{sid}", cb_school_tz),
        (f"schdel:{sid}", cb_school_delete_confirm),
    ):
        call = cb(data, 400, name="Чужой", message=SentMessage(bot, 400, ""))
        await handler(call)
        assert call.answers and call.answers[-1][1] is True  # show_alert=True block

    # invite link needs a bot arg
    link = cb(f"schlink:{sid}", 400, name="Чужой", message=SentMessage(bot, 400, ""))
    await cb_school_invite_link(link, bot)
    assert any("owner" in a[0].lower() for a in link.answers)
    async with session_scope() as s:
        assert await schools_svc.get_timezone(s, sid) == "Europe/Moscow"  # unchanged
