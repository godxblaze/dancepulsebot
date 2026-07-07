"""Регрессия на lightweight-миграции, которые свежая схема не воспроизводит."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.db.models import SurveyKind
from app.db.session import _backfill_support_pool, engine, init_db, session_scope
from app.services import admins, groups, persons, surveys
from app.services.schedule import as_utc

# Тесты дописывают/мутируют DDL мимо metadata — схему после них надо строить заново.
pytestmark = pytest.mark.schema_mutating


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


async def test_lesson_at_migration_runs_once_and_does_not_reshift(db) -> None:
    """Регрессия: SQLite-слой хранит даже aware-UTC lesson_at как naive-строку
    без TZ-маркера, поэтому повторный прогон lesson_at→UTC миграции принимал бы
    корректное рантайм-значение за legacy и сдвигал его на TZ-офсет при КАЖДОМ
    рестарте бота. Гейт user_version должен прогнать миграцию ровно один раз."""
    # Сбрасываем флаг и прогоняем init_db: миграция отрабатывает на пустых
    # данных (no-op) и ставит user_version=1.
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA user_version = 0"))
    await init_db()

    lesson = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)  # 21:00 МСК
    async with session_scope() as s:
        owner = await admins.get_or_create(s, 1, "t", "Owner")
        g = await groups.create_group(s, owner.id, "G", "0", "21:00", 18)
        sv, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, lesson
        )
        sid = sv.id

    # Повторный init_db (= рестарт бота) НЕ должен трогать lesson_at.
    await init_db()

    async with session_scope() as s:
        sv = await surveys.get_survey(s, sid)
    assert sv is not None and as_utc(sv.lesson_at) == lesson  # не сдвинулось
