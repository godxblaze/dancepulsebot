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
    ("admins", "has_completed_setup", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("responses", "reminded_4h", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("responses", "reminded_1h", "BOOLEAN DEFAULT 0 NOT NULL"),
)


async def _apply_lightweight_migrations(conn) -> None:
    for table, column, ddl in _LIGHTWEIGHT_MIGRATIONS:
        existing = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = {row[1] for row in existing.fetchall()}
        if column in cols:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def _table_columns(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return {row[1] for row in rows}


async def _backfill_schools(conn) -> None:
    """Одноразовая миграция Admin→School. Выполняется если хотя бы у одной из
    legacy-таблиц ещё есть `admin_id` (полу-смигрированная БД из неудачного
    предыдущего запуска тоже должна добежать). На свежих БД пропускается."""
    has_legacy = False
    for t in ("groups", "persons", "venues"):
        if "admin_id" in await _table_columns(conn, t):
            has_legacy = True
            break
    if has_legacy:
        # 1. У каждого admin'а без owner-школы — создать «Моя школа».
        await conn.execute(text(
            "INSERT INTO schools (name) "
            "SELECT 'Моя школа' FROM admins a "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM school_members sm "
            "  WHERE sm.admin_id = a.id AND sm.role = 'owner'"
            ")"
        ))
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
        # Per-table проверка — если admin_id уже DROP'нут в одной таблице
        # (полу-смигрированная БД), не падать на UPDATE.
        for table in ("groups", "persons", "venues"):
            if "admin_id" not in await _table_columns(conn, table):
                continue
            await conn.execute(text(
                f"UPDATE {table} SET school_id = ("
                "  SELECT sm.school_id FROM school_members sm "
                f" WHERE sm.admin_id = {table}.admin_id AND sm.role = 'owner' LIMIT 1"
                ") "
                f"WHERE {table}.school_id IS NULL AND {table}.admin_id IS NOT NULL"
            ))
    # 3. Все admin'ы у которых уже есть связь со школой — прошли onboarding.
    await conn.execute(text(
        "UPDATE admins SET has_completed_setup = 1 "
        "WHERE id IN (SELECT DISTINCT admin_id FROM school_members)"
    ))


async def _migrate_lesson_at_to_utc(conn) -> None:
    """TZ-рефактор: до него `Survey.lesson_at` и `LessonCancellation.lesson_at`
    хранились как naive local (Europe/Moscow). После — aware UTC. Сдвиг
    выполняем однократно: для каждой строки без 'T' и без '+'/'Z' в text-
    представлении датавремени делаем `dt = naive_local.astimezone(UTC)` и
    переписываем. Идемпотентно: если значение уже содержит TZ-маркер ('+',
    '-' после позиции 10, или 'Z') — пропускаем."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    for table in ("surveys", "lesson_cancellations"):
        rows = (await conn.execute(text(f"SELECT id, lesson_at FROM {table}"))).fetchall()
        for row_id, raw in rows:
            if raw is None or not isinstance(raw, str):
                continue
            # Уже aware? '+'/'-' в позициях 19+ (после YYYY-MM-DD HH:MM:SS) или 'Z'.
            if "+" in raw[19:] or raw.endswith("Z") or "-" in raw[19:]:
                continue
            # Парсим как naive в локальном TZ → aware UTC.
            try:
                naive = datetime.fromisoformat(raw)
            except ValueError:
                continue
            aware_local = naive.replace(tzinfo=tz)
            aware_utc = aware_local.astimezone(ZoneInfo("UTC"))
            new_raw = aware_utc.isoformat(sep=" ")
            await conn.execute(
                text(f"UPDATE {table} SET lesson_at = :v WHERE id = :i"),
                {"v": new_raw, "i": row_id},
            )


async def init_db() -> None:
    """Свежая БД — Base.metadata.create_all уже создаёт таблицы без legacy
    `admin_id`. Старая БД — оставляем колонку в таблице как мёртвую (SQLite не
    даёт ALTER TABLE DROP COLUMN если на колонке висит FK), код её
    игнорирует."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_lightweight_migrations(conn)
        await _backfill_schools(conn)
        await _migrate_lesson_at_to_utc(conn)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
