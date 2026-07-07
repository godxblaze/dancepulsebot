import os

# Point the app at a throwaway DB before any app module imports settings.
# Per-PID filename — иначе конкурентные pytest-процессы (напр. Stop-hook этой
# сессии и другой параллельной Claude-сессии в том же репо) делят один файл и
# гоняют DROP/CREATE TABLE друг у друга из-под ног («no such table» флейки).
os.environ["DB_PATH"] = f"_pytest_dance_{os.getpid()}.sqlite3"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services import admins as _admins  # noqa: E402
from app.services import schools as _schools_svc  # noqa: E402

# Legacy-таблицы, оставшиеся от прошлой схемы. Drop_all не знает про них
# (они уже не в metadata), но они держат FK на admins/groups — без явного
# DROP не дать пересоздать схему. Дропаем в начале каждого теста.
_LEGACY_TABLES = ("group_access", "coadmin_invites")


@pytest_asyncio.fixture
async def db():
    """Fresh empty schema for each test that needs the database."""
    async with engine.begin() as conn:
        for t in _LEGACY_TABLES:
            await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


# Прод admins.get_or_create НЕ создаёт школу — этот шаг идёт через явный
# CreateSchool FSM в UI. Старые тесты пишут `admins.get_or_create(...)` и
# сразу же используют admin.id как school_id; чтобы их не переписывать
# массово, мокаем admins.get_or_create в тестах: после создания admin'а
# гарантируем ему owned-школу. Совпадение admin.id == school.id (оба
# auto-increment в чистой БД) держит обратную совместимость.
_original_get_or_create = _admins.get_or_create


async def _get_or_create_with_school(session, tg_user_id, tg_username, full_name):
    admin = await _original_get_or_create(session, tg_user_id, tg_username, full_name)
    await _schools_svc.get_or_create_owned_school(session, admin.id)
    return admin


@pytest.fixture(autouse=True)
def _auto_school_for_tests(monkeypatch):
    monkeypatch.setattr(_admins, "get_or_create", _get_or_create_with_school)
