from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import Base

engine = create_async_engine(settings.database_url)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite(dbapi_connection, _record) -> None:
    """Per-connection PRAGMAs: FK enforcement + WAL for concurrent reads/safer crashes."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Columns added to existing tables after the initial schema — kept here so init_db
# can patch already-running SQLite databases without losing data. (table, column, ddl).
_LIGHTWEIGHT_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("groups", "style", "VARCHAR(80)"),
    ("groups", "venue_id", "INTEGER REFERENCES venues(id) ON DELETE SET NULL"),
    ("groups", "room", "VARCHAR(80)"),
    ("groups", "teachers_note", "VARCHAR(200)"),
    ("groups", "level", "INTEGER"),
    ("support_calls", "no_reply_notified", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("responses", "actually_attended", "BOOLEAN"),
    ("groups", "school_id", "INTEGER REFERENCES schools(id) ON DELETE CASCADE"),
    ("persons", "school_id", "INTEGER REFERENCES schools(id) ON DELETE CASCADE"),
    ("venues", "school_id", "INTEGER REFERENCES schools(id) ON DELETE CASCADE"),
)


async def _apply_lightweight_migrations(conn) -> None:
    for table, column, ddl in _LIGHTWEIGHT_MIGRATIONS:
        existing = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = {row[1] for row in existing.fetchall()}
        if column in cols:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def _backfill_schools(conn) -> None:
    """Для каждого Admin без owner-школы создаём «Моя школа» и переносим в неё
    все его группы/учеников/площадки. Идемпотентно — повторный запуск ничего
    не делает."""
    # 1. У каждого admin'а должна быть своя owner-школа.
    await conn.execute(text(
        "INSERT INTO schools (name) "
        "SELECT 'Моя школа' FROM admins a "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM school_members sm "
        "  WHERE sm.admin_id = a.id AND sm.role = 'owner'"
        ")"
    ))
    # Это создаёт N школ — но не привязывает их к admin'ам. Свяжу через
    # «последняя школа без owner-membership = admin без owner-школы». Сложно
    # для one-pass SQL → делаю pythonic loop.
    admins_rows = (await conn.execute(text(
        "SELECT a.id FROM admins a WHERE NOT EXISTS ("
        "  SELECT 1 FROM school_members sm "
        "  WHERE sm.admin_id = a.id AND sm.role = 'owner'"
        ")"
    ))).fetchall()
    free_schools = (await conn.execute(text(
        "SELECT s.id FROM schools s WHERE NOT EXISTS ("
        "  SELECT 1 FROM school_members sm WHERE sm.school_id = s.id"
        ") ORDER BY s.id"
    ))).fetchall()
    for (admin_id,), (school_id,) in zip(admins_rows, free_schools, strict=False):
        await conn.execute(text(
            "INSERT INTO school_members (school_id, admin_id, role) "
            "VALUES (:sid, :aid, 'owner')"
        ), {"sid": school_id, "aid": admin_id})

    # 2. Backfill school_id у объектов, у которых он ещё NULL.
    for table in ("groups", "persons", "venues"):
        await conn.execute(text(
            f"UPDATE {table} SET school_id = ("
            "  SELECT sm.school_id FROM school_members sm "
            f" WHERE sm.admin_id = {table}.admin_id AND sm.role = 'owner' LIMIT 1"
            ") "
            f"WHERE {table}.school_id IS NULL AND {table}.admin_id IS NOT NULL"
        ))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_lightweight_migrations(conn)
        await _backfill_schools(conn)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
