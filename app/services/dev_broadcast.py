"""Phase 11: recipient resolution for the global developer broadcast — pure
queries, no dialog/UI concerns. Broadcast row creation + outbox enqueue still
go through app/services/broadcasts.py + app/services/outbox.py exactly as
Phase 5 built them (the task's explicit guardrail against a second broadcast
mechanism) — this module only answers "who are the recipients"."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Person, School, SchoolMember


async def all_school_ids(session: AsyncSession) -> list[int]:
    return list((await session.execute(select(School.id))).scalars().all())


async def list_schools(session: AsyncSession) -> list[tuple[int, str]]:
    """(id, name) for every school, alphabetical — feeds the scope-pick screen."""
    rows = (await session.execute(select(School.id, School.name).order_by(School.name))).all()
    return [(r[0], r[1]) for r in rows]


async def school_names(session: AsyncSession, school_ids: list[int]) -> list[str]:
    return list((
        await session.execute(
            select(School.name).where(School.id.in_(school_ids)).order_by(School.name)
        )
    ).scalars().all())


async def recipients(
    session: AsyncSession, school_ids: list[int], audience_type: str
) -> list[tuple[int, int]]:
    """(recipient_id, tg_user_id) pairs for the given school scope + audience.
    `audience_type` is `"person"` (every Person in scope) or `"admin"` (every
    distinct Admin who is a SchoolMember of any school in scope). Empty
    `school_ids` correctly yields zero rows, not an error — `IN ()` degrades
    to "matches nothing", the honest answer for an empty scope."""
    if audience_type == "admin":
        stmt = (
            select(Admin.id, Admin.tg_user_id)
            .distinct()
            .join(SchoolMember, SchoolMember.admin_id == Admin.id)
            .where(SchoolMember.school_id.in_(school_ids))
        )
    else:  # "person"
        stmt = select(Person.id, Person.tg_user_id).where(Person.school_id.in_(school_ids))
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def recipient_count(session: AsyncSession, school_ids: list[int], audience_type: str) -> int:
    return len(await recipients(session, school_ids, audience_type))
