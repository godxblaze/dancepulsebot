from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Student


async def add_student(
    session: AsyncSession,
    group_id: int,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> tuple[Student, bool]:
    """Returns (student, created). Idempotent on (group_id, tg_user_id)."""
    stmt = select(Student).where(
        Student.group_id == group_id, Student.tg_user_id == tg_user_id
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.tg_username = tg_username
        existing.full_name = full_name
        await session.flush()
        return existing, False

    student = Student(
        group_id=group_id,
        tg_user_id=tg_user_id,
        tg_username=tg_username,
        full_name=full_name,
    )
    session.add(student)
    await session.flush()
    return student, True


async def get_in_group(
    session: AsyncSession, group_id: int, tg_user_id: int
) -> Student | None:
    stmt = select(Student).where(
        Student.group_id == group_id, Student.tg_user_id == tg_user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def group_students(session: AsyncSession, group_id: int) -> list[Student]:
    stmt = select(Student).where(Student.group_id == group_id).order_by(Student.full_name)
    return list((await session.execute(stmt)).scalars().all())


async def count_students(session: AsyncSession, group_id: int) -> int:
    stmt = select(func.count()).select_from(Student).where(Student.group_id == group_id)
    return (await session.execute(stmt)).scalar_one()
