import os

# Point the app at a throwaway DB before any app module imports settings.
os.environ["DB_PATH"] = "_pytest_dance.sqlite3"

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402

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
