"""Unit tests for teacher / group / student CRUD services."""
from datetime import datetime

from app.db.models import SurveyKind
from app.db.session import session_scope
from app.services import groups, students, surveys, teachers


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


async def test_add_student_idempotent_and_updates(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        _, created1 = await students.add_student(s, gid, 10, "old", "Старое Имя")
        assert created1
    async with session_scope() as s:
        st2, created2 = await students.add_student(s, gid, 10, "new", "Новое Имя")
        assert not created2
        assert st2.full_name == "Новое Имя" and st2.tg_username == "new"
    async with session_scope() as s:
        assert await students.count_students(s, gid) == 1


async def test_group_students_ordered_by_name(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        await students.add_student(s, g.id, 1, "a", "Яна")
        await students.add_student(s, g.id, 2, "b", "Аня")
        await students.add_student(s, g.id, 3, "c", "Маша")
        gid = g.id
    async with session_scope() as s:
        names = [st.full_name for st in await students.group_students(s, gid)]
    assert names == ["Аня", "Маша", "Яна"]


async def test_delete_group_cascades(db) -> None:
    """Deleting a group must not leave orphaned students / surveys / responses."""
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        st, _ = await students.add_student(s, g.id, 10, "a", "A")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, datetime(2026, 5, 25, 19, 0)
        )
        await surveys.ensure_responses(s, survey.id, [st.id])
        gid, sid = g.id, survey.id
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        assert g is not None
        await groups.delete_group(s, g)
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is None
        assert await students.count_students(s, gid) == 0
        assert await surveys.get_survey(s, sid) is None
