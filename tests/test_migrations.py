"""Регрессия на lightweight-миграции, которые свежая схема не воспроизводит."""
from sqlalchemy import text

from app.db.session import _backfill_support_pool, engine, session_scope
from app.services import admins, groups, persons


async def test_backfill_support_pool_from_legacy_support_level(db) -> None:
    """School-wide Person.support_level → SupportPoolEntry в пул owner'а.
    Свежая схема колонки support_level не содержит — добавляем вручную."""
    async with session_scope() as s:
        owner = await admins.get_or_create(s, 1, "t", "Owner")  # conftest даёт owned-школу
        g = await groups.create_group(s, owner.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 5, "u", "Саппорт")
        owner_id, pid = owner.id, p.id

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE persons ADD COLUMN support_level INTEGER"))
        await conn.execute(
            text("UPDATE persons SET support_level = 2 WHERE id = :i"), {"i": pid}
        )
        await _backfill_support_pool(conn)
        rows = (await conn.execute(
            text("SELECT admin_id, person_id, level FROM support_pool_entries")
        )).fetchall()
        # идемпотентно: повторный вызов не дублирует (таблица уже непуста)
        await _backfill_support_pool(conn)
        rows2 = (await conn.execute(
            text("SELECT count(*) FROM support_pool_entries")
        )).scalar_one()

    assert (owner_id, pid, 2) in [tuple(r) for r in rows]
    assert rows2 == len(rows)
