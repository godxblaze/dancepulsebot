import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import Base

log = logging.getLogger(__name__)

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
    ("schools", "timezone", "VARCHAR(64) DEFAULT 'Europe/Moscow' NOT NULL"),
    ("support_calls", "topup_done", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("support_calls", "pending_notified", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("support_claims", "attended", "BOOLEAN"),
    # Настройки автодобора — личные на Admin (с DB-default'ами, чтобы будущее
    # удаление поля из модели не ломало INSERT — урок прошлой итерации).
    # На старой БД колонки schools.support_* остаются мёртвыми (у них есть
    # default, INSERT школы не ломают) — бэкфилл читает из них в Admin.
    ("admins", "support_threshold", "INTEGER DEFAULT 2 NOT NULL"),
    ("admins", "support_cutoff_hours", "INTEGER DEFAULT 3 NOT NULL"),
    ("admins", "recruit_evening_time", "VARCHAR(5) DEFAULT '20:00' NOT NULL"),
    ("admins", "recruit_mode", "VARCHAR(16) DEFAULT 'curated' NOT NULL"),
    ("admins", "support_code", "VARCHAR(32)"),
    ("groups", "recruit_lead_admin_id", "INTEGER REFERENCES admins(id) ON DELETE SET NULL"),
    ("persons", "last_reactivation_at", "DATETIME"),
    ("support_calls", "picker_chat_id", "BIGINT"),
    ("support_calls", "picker_message_id", "BIGINT"),
)


async def _apply_lightweight_migrations(conn) -> set[tuple[str, str]]:
    """Добавляет недостающие колонки. Возвращает множество (table, column),
    которые были добавлены именно в этот запуск — чтобы привязать одноразовый
    бэкфилл к моменту фактической миграции."""
    added: set[tuple[str, str]] = set()
    for table, column, ddl in _LIGHTWEIGHT_MIGRATIONS:
        existing = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = {row[1] for row in existing.fetchall()}
        if column in cols:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        added.add((table, column))
    return added


async def _backfill_admin_support_settings(conn) -> None:
    """Одноразовый перенос настроек автодобора School → Admin(owner) (реверс
    прошлой итерации: настройки стали личными у препода). Вызывается только когда
    колонки admins.support_* были только что добавлены. Имена колонок фиксированы
    (не из ввода); каждая переносится только если ещё есть в schools."""
    school_cols = await _table_columns(conn, "schools")
    for col in ("support_threshold", "support_cutoff_hours", "recruit_evening_time"):
        if col not in school_cols:
            continue
        # COALESCE: если у школы значение NULL — оставить DB-default на Admin'е.
        await conn.execute(text(
            f"UPDATE admins SET {col} = COALESCE(("
            f"  SELECT s.{col} FROM schools s "
            "  JOIN school_members sm ON sm.school_id = s.id "
            f"  WHERE sm.admin_id = admins.id AND sm.role = 'owner' LIMIT 1"
            f"), {col}) WHERE id IN (SELECT admin_id FROM school_members WHERE role = 'owner')"
        ))


async def _backfill_support_pool(conn) -> None:
    """Одноразовый перенос school-wide Person.support_level → SupportPoolEntry в
    пул owner'а школы. Гейт: таблица пула пуста и колонка persons.support_level
    ещё существует (на свежей БД её нет — модель убрала)."""
    if "support_level" not in await _table_columns(conn, "persons"):
        return
    has_entries = (await conn.execute(
        text("SELECT 1 FROM support_pool_entries LIMIT 1")
    )).first()
    if has_entries:
        return
    await conn.execute(text(
        "INSERT INTO support_pool_entries (admin_id, person_id, level) "
        "SELECT sm.admin_id, p.id, p.support_level "
        "FROM persons p "
        "JOIN school_members sm ON sm.school_id = p.school_id AND sm.role = 'owner' "
        "WHERE p.support_level IS NOT NULL"
    ))


async def _backfill_recruit_lead(conn) -> None:
    """Существующим группам без lead'а ставим куратором добора owner'а школы."""
    await conn.execute(text(
        "UPDATE groups SET recruit_lead_admin_id = ("
        "  SELECT sm.admin_id FROM school_members sm "
        "  WHERE sm.school_id = groups.school_id AND sm.role = 'owner' LIMIT 1"
        ") WHERE recruit_lead_admin_id IS NULL AND school_id IS NOT NULL"
    ))


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
    # uniqueness-ключ (кроме lesson_at) — чтобы пропустить строки, чей
    # сконвертированный lesson_at столкнётся с уже существующей строкой
    # (мусор от дублей/полу-миграций не должен ронять старт).
    key_cols = {"surveys": ("group_id", "kind"), "lesson_cancellations": ("group_id",)}
    for table, keys in key_cols.items():
        cols = ", ".join(keys)
        rows = (await conn.execute(
            text(f"SELECT id, {cols}, lesson_at FROM {table}")
        )).fetchall()
        for row in rows:
            row_id, raw = row[0], row[-1]
            key_vals = row[1:-1]
            if raw is None or not isinstance(raw, str):
                continue
            if "+" in raw[19:] or raw.endswith("Z") or "-" in raw[19:]:
                continue
            try:
                naive = datetime.fromisoformat(raw)
            except ValueError:
                continue
            new_raw = naive.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat(sep=" ")
            where = " AND ".join(f"{c} = :k{i}" for i, c in enumerate(keys))
            params = {f"k{i}": v for i, v in enumerate(key_vals)}
            params.update(v=new_raw, rid=row_id)
            clash = (await conn.execute(
                text(f"SELECT 1 FROM {table} WHERE {where} AND lesson_at = :v "
                     "AND id != :rid LIMIT 1"),
                params,
            )).first()
            if clash:
                log.warning(
                    "lesson_at→UTC: пропуск %s id=%s — целевое значение коллидит",
                    table, row_id,
                )
                continue
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
        added = await _apply_lightweight_migrations(conn)
        await _backfill_schools(conn)
        if ("admins", "support_threshold") in added:
            await _backfill_admin_support_settings(conn)
        if ("groups", "recruit_lead_admin_id") in added:
            await _backfill_recruit_lead(conn)
        # Не привязан к `added`: триггер — «таблица пула пуста + persons.support_level
        # ещё есть» (см. функцию). На боевой БД persons.support_level существует с
        # прошлой схемы, поэтому гейт по `added` тут не подходит.
        await _backfill_support_pool(conn)
        # lesson_at→UTC — РОВНО одноразовая конверсия legacy naive-local значений,
        # гейтим через user_version. КРИТИЧНО: SQLite-слой хранит даже aware-UTC
        # datetime как naive-строку (без TZ-маркера), поэтому проверка
        # идемпотентности «есть ли маркер» внутри миграции не отличает легально
        # сохранённое рантайм-значение от legacy — повторный прогон сдвинул бы
        # корректные опросы на TZ-офсет при КАЖДОМ рестарте. user_version=1 после
        # первого прогона навсегда выключает миграцию.
        version = (await conn.execute(text("PRAGMA user_version"))).scalar() or 0
        if version < 1:
            await _migrate_lesson_at_to_utc(conn)
            await conn.execute(text("PRAGMA user_version = 1"))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
