import os

# Point the app at a throwaway DB before any app module imports settings.
os.environ["DB_PATH"] = "_pytest_dance.sqlite3"

import pytest_asyncio  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402


@pytest_asyncio.fixture
async def db():
    """Fresh empty schema for each test that needs the database."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
