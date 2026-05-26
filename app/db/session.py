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
)


async def _apply_lightweight_migrations(conn) -> None:
    for table, column, ddl in _LIGHTWEIGHT_MIGRATIONS:
        existing = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = {row[1] for row in existing.fetchall()}
        if column in cols:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def _backfill_group_access(conn) -> None:
    """Для уже существующих групп заводим owner-запись в group_access — если её
    ещё нет. Идемпотентно (INSERT OR IGNORE через NOT EXISTS)."""
    await conn.execute(text(
        "INSERT INTO group_access (group_id, admin_id, role) "
        "SELECT g.id, g.admin_id, 'owner' FROM groups g "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM group_access ga "
        "  WHERE ga.group_id = g.id AND ga.admin_id = g.admin_id"
        ")"
    ))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_lightweight_migrations(conn)
        await _backfill_group_access(conn)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
