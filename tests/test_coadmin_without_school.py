"""Пилот-гейт «co-admin без owned-школы»: до фикса co-admin, приглашённый
partner-ссылкой (никогда не создававший СВОЮ школу), ломал /newgroup (фантомная
«Моя школа»), support-ссылку («не найдена») и /export (b"" → Telegram отклонял
файл). В тестовой БД admins.get_or_create замокан так, что КАЖДЫЙ admin
получает owned-школу (см. tests/conftest.py) — этот файл обходит мок и создаёт
co-admin'а напрямую, как это происходит в проде через partner-invite."""
from __future__ import annotations

from types import SimpleNamespace

from app.bot.dialogs.new_group import _target_school
from app.bot.handlers.admin import cmd_export, newgroup_start
from app.bot.handlers.student import _support_invite_start
from app.db.models import Admin
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from tests._e2e_helpers import TEACHER_TG, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, fake_user


async def _bare_coadmin(session, school_id: int, tg_id: int, name: str) -> Admin:
    """Co-admin без owned-школы — как реальный partner-invite accept, в обход
    тестового автосоздания школы в admins.get_or_create."""
    admin = Admin(tg_user_id=tg_id, tg_username=None, full_name=name)
    session.add(admin)
    await session.flush()
    await schools_svc.add_coadmin(session, school_id, admin.id)
    return admin


class _FakeDialogManager:
    def __init__(self) -> None:
        self.started: tuple | None = None

    async def start(self, state, mode=None, data=None, **kwargs) -> None:
        self.started = (state, mode, data)


async def test_target_school_prefers_start_data(db) -> None:
    _, sid, _ = await make_group()
    manager = SimpleNamespace(start_data={"school_id": sid})
    async with session_scope() as s:
        school = await _target_school(s, manager, admin_id=999999)
        assert school is not None and school.id == sid


async def test_newgroup_single_school_uses_it_not_phantom(db) -> None:
    """Co-admin без owned-школы, member ровно одной (чужой) — /newgroup должен
    завести группу именно в ней, а не молча создать «Моя школа»."""
    _, sid, _ = await make_group()
    async with session_scope() as s:
        coadmin = await _bare_coadmin(s, sid, 777, "Co")
        coadmin_id = coadmin.id

    dm = _FakeDialogManager()
    await newgroup_start(FakeMessage(fake_user(777, full_name="Co")), dm)
    assert dm.started is not None
    _, _, data = dm.started
    assert data == {"school_id": sid}

    async with session_scope() as s:
        # никакой левой школы co-admin'у не завели
        assert await schools_svc.get_owned_school(s, coadmin_id) is None


async def test_newgroup_multi_school_prompts_instead_of_guessing(db) -> None:
    """Co-admin двух школ — неоднозначно, куда положить группу; вместо
    молчаливого выбора просим открыть карточку школы явно."""
    _, sid1, _ = await make_group(teacher_tg=TEACHER_TG, teacher_name="Owner1")
    _, sid2, _ = await make_group(teacher_tg=901, teacher_name="Owner2")
    async with session_scope() as s:
        await _bare_coadmin(s, sid1, 777, "Co")
        await schools_svc.add_coadmin(s, sid2, (await admins_svc.get_by_tg_id(s, 777)).id)

    dm = _FakeDialogManager()
    msg = FakeMessage(fake_user(777, full_name="Co"))
    await newgroup_start(msg, dm)
    assert dm.started is None  # мастер не стартовал
    assert msg.replies and "несколько школ" in msg.replies[0].text


async def test_support_link_falls_back_to_membership_school(db) -> None:
    """Support-ссылка co-admin'а без owned-школы больше не мертва."""
    _, sid, _ = await make_group()
    async with session_scope() as s:
        coadmin = await _bare_coadmin(s, sid, 778, "Co")
        code = await admins_svc.get_or_create_support_code(s, coadmin.id)

    fsm = FakeFSM()
    bot = FakeBot()
    msg = FakeMessage(fake_user(500, full_name="Саппорт"), bot=bot)
    await _support_invite_start(msg, code, fsm, bot)
    assert msg.replies
    assert "не найдена" not in msg.replies[0].text
    assert fsm.data.get("support_school_id") == sid


async def test_export_works_for_coadmin_without_owned_school(db) -> None:
    """/export у co-admin'а без owned-школы отдаёт CSV его рабочей школы,
    а не падает на пустом BufferedInputFile."""
    gid, sid, _ = await make_group()
    async with session_scope() as s:
        from app.services import persons as persons_svc
        group = await groups_svc.get_group(s, gid)
        await persons_svc.add_to_group(s, group, 600, "u", "Ученик")
        await _bare_coadmin(s, sid, 779, "Co")

    bot = FakeBot()
    msg = FakeMessage(fake_user(779, full_name="Co"), bot=bot)
    await cmd_export(msg, bot)
    assert len(msg.replies) == 2  # students.csv + attendance.csv
    for reply in msg.replies:
        assert len(reply.document.data) > 0  # не пустой BufferedInputFile
