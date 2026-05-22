"""Unit tests for teacher / group / person CRUD services."""
from datetime import datetime

from app.db.models import Person, SurveyKind
from app.db.session import session_scope
from app.services import groups, persons, surveys, teachers


async def test_teacher_get_or_create_idempotent(db) -> None:
    async with session_scope() as s:
        t1 = await teachers.get_or_create(s, 500, "ann", "Анна")
    async with session_scope() as s:
        t2 = await teachers.get_or_create(s, 500, "ann2", "Анна Другая")
    assert t1.id == t2.id  # same teacher, not duplicated


async def test_get_by_tg_id_missing(db) -> None:
    async with session_scope() as s:
        assert await teachers.get_by_tg_id(s, 999) is None


async def test_group_by_join_code(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G1", "0,2", "19:00", 18)
        code = g.join_code
    async with session_scope() as s:
        found = await groups.get_by_join_code(s, code)
        assert found is not None and found.name == "G1"
        assert await groups.get_by_join_code(s, "no-such-code") is None


async def test_teacher_groups_ordered_by_id(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        for name in ("Alpha", "Bravo", "Charlie"):
            await groups.create_group(s, t.id, name, "0", "19:00", 18)
        tid = t.id
    async with session_scope() as s:
        gs = await groups.teacher_groups(s, tid)
    assert [g.name for g in gs] == ["Alpha", "Bravo", "Charlie"]


async def test_active_groups_excludes_inactive(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        await groups.create_group(s, t.id, "On", "0", "19:00", 18)
        off = await groups.create_group(s, t.id, "Off", "0", "19:00", 18)
        off.active = False
    async with session_scope() as s:
        active = await groups.active_groups(s)
    assert [g.name for g in active] == ["On"]


async def test_add_to_group_idempotent_and_updates(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        _, joined1 = await persons.add_to_group(s, g, 10, "old", "Старое Имя")
        assert joined1
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        p2, joined2 = await persons.add_to_group(s, g, 10, "new", "Новое Имя")
        assert not joined2  # already a member
        assert p2.full_name == "Новое Имя" and p2.tg_username == "new"
    async with session_scope() as s:
        assert await persons.count_members(s, gid) == 1


async def test_person_joins_two_groups(db) -> None:
    """One human in two groups = one Person, two memberships."""
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g1 = await groups.create_group(s, t.id, "G1", "0", "19:00", 18)
        g2 = await groups.create_group(s, t.id, "G2", "2", "20:00", 18)
        p1, _ = await persons.add_to_group(s, g1, 10, "u", "Универсал")
        p2, _ = await persons.add_to_group(s, g2, 10, "u", "Универсал")
        assert p1.id == p2.id  # same Person, not duplicated
        g1id, g2id = g1.id, g2.id
    async with session_scope() as s:
        assert await persons.count_members(s, g1id) == 1
        assert await persons.count_members(s, g2id) == 1


async def test_group_members_ordered_by_name(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        await persons.add_to_group(s, g, 1, "a", "Яна")
        await persons.add_to_group(s, g, 2, "b", "Аня")
        await persons.add_to_group(s, g, 3, "c", "Маша")
        gid = g.id
    async with session_scope() as s:
        names = [p.full_name for p in await persons.group_members(s, gid)]
    assert names == ["Аня", "Маша", "Яна"]


async def test_delete_group_cascades(db) -> None:
    """Deleting a group removes memberships/surveys/responses but keeps the person."""
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 10, "a", "A")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, datetime(2026, 5, 25, 19, 0)
        )
        await surveys.ensure_responses(s, survey.id, [p.id])
        gid, sid, pid = g.id, survey.id, p.id
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        assert g is not None
        await groups.delete_group(s, g)
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is None
        assert await persons.count_members(s, gid) == 0
        assert await surveys.get_survey(s, sid) is None
        # the person is teacher-scoped — survives the group deletion
        assert await s.get(Person, pid) is not None
