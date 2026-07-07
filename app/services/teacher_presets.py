from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TeacherPreset


async def school_presets(session: AsyncSession, school_id: int) -> list[TeacherPreset]:
    """Пресеты «кто ведёт» школы, oldest first — кнопки в мастере группы."""
    stmt = (
        select(TeacherPreset)
        .where(TeacherPreset.school_id == school_id)
        .order_by(TeacherPreset.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_preset(session: AsyncSession, preset_id: int) -> TeacherPreset | None:
    return await session.get(TeacherPreset, preset_id)


async def get_or_create(
    session: AsyncSession, school_id: int, note: str
) -> TeacherPreset:
    """Идемпотентно по точному тексту — введённое в мастере не плодит дублей."""
    stmt = select(TeacherPreset).where(
        TeacherPreset.school_id == school_id, TeacherPreset.note == note
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing
    preset = TeacherPreset(school_id=school_id, note=note)
    session.add(preset)
    await session.flush()
    return preset


async def delete_preset(session: AsyncSession, preset: TeacherPreset) -> None:
    await session.delete(preset)
    await session.flush()
