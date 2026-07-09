"""Phase 10: какой UI разработчик видит на /start — `dev_ui_pref.ui_mode`
(`developer` default | `teacher`). Не привязано к Admin — не требует школы."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DevUiPref

DEFAULT_MODE = "developer"


async def get_ui_mode(session: AsyncSession, tg_user_id: int) -> str:
    row = await session.get(DevUiPref, tg_user_id)
    return row.ui_mode if row is not None else DEFAULT_MODE


async def set_ui_mode(session: AsyncSession, tg_user_id: int, mode: str) -> None:
    stmt = (
        pg_insert(DevUiPref)
        .values(tg_user_id=tg_user_id, ui_mode=mode)
        .on_conflict_do_update(index_elements=["tg_user_id"], set_={"ui_mode": mode})
    )
    await session.execute(stmt)


async def toggle_ui_mode(session: AsyncSession, tg_user_id: int) -> str:
    """Flip developer<->teacher, persist, return the new mode."""
    current = await get_ui_mode(session, tg_user_id)
    new_mode = "teacher" if current == "developer" else "developer"
    await set_ui_mode(session, tg_user_id, new_mode)
    return new_mode
