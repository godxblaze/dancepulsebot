import contextlib
import os

# Point the app at a throwaway DB before any app module imports settings.
# Per-PID filename — иначе конкурентные pytest-процессы (напр. Stop-hook этой
# сессии и другой параллельной Claude-сессии в том же репо, или xdist-воркеры)
# делят один файл и гоняют DROP/CREATE TABLE друг у друга из-под ног
# («no such table» флейки).
os.environ["DB_PATH"] = f"_pytest_dance_{os.getpid()}.sqlite3"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services import admins as _admins  # noqa: E402
from app.services import schools as _schools_svc  # noqa: E402

# Legacy-таблицы, оставшиеся от прошлой схемы. Drop_all не знает про них
# (они уже не в metadata), но они держат FK на admins/groups — без явного
# DROP не дать пересоздать схему. Дропаем при построении схемы.
_LEGACY_TABLES = ("group_access", "coadmin_invites")


# Тестовый override поверх прод-PRAGMA (порядок listener'ов = порядок
# регистрации, поэтому выполняется после прод-настройки WAL/NORMAL):
# durability тестовой БД не нужна, а fsync'и на Windows — заметная доля
# каждого DDL/чекпойнта.
@event.listens_for(engine.sync_engine, "connect")
def _test_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.close()


# Полный drop_all+create_all на каждый тест (~25 таблиц × 2 DDL через
# aiosqlite) стоил ~60-80мс × 365 тестов ≈ 20с на суит. Поэтому схему строим
# один раз на процесс, а между тестами чистим только данные — одним
# executescript'ом (child→parent, так что FK не мешают). Rowid-таблицы после
# полного DELETE снова выдают id с 1 — инвариант admin.id == school.id
# в старых тестах сохраняется.
_schema_ready = False
_wipe_sql = ""


@pytest_asyncio.fixture
async def db(request):
    """Fresh empty schema for each test that needs the database.

    Тесты, мутирующие DDL (регрессии миграций), помечены
    @pytest.mark.schema_mutating — перед ними и после них схема
    пересоздаётся целиком.
    """
    global _schema_ready, _wipe_sql
    mutating = request.node.get_closest_marker("schema_mutating") is not None
    if _schema_ready and not mutating:
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.executescript(_wipe_sql)
    else:
        async with engine.begin() as conn:
            for t in _LEGACY_TABLES:
                await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        _wipe_sql = "BEGIN;" + "".join(
            f'DELETE FROM "{t.name}";' for t in reversed(Base.metadata.sorted_tables)
        ) + "COMMIT;"
        _schema_ready = True
    yield
    if mutating:
        _schema_ready = False


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _cleanup_test_db():
    """Закрыть пул и удалить per-PID тест-БД (+ WAL/SHM) в конце прогона —
    иначе триплеты файлов копятся в корне репо. У каждого pytest-xdist воркера
    свой PID и свой файл, так что удаление безопасно и для конкурентных
    прогонов. Best-effort: на Windows незакрытый хендл не должен ронять
    зелёный прогон."""
    yield
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(os.environ["DB_PATH"] + suffix)


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
