"""
pytest conftest – spins up a test PostgreSQL database, creates all tables once
per session, and clears rows between every test for full isolation.

The test DB URL is derived automatically from DATABASE_URL in .env by swapping
the database name to 'justeats_test', so credentials always stay in sync.
Set TEST_DATABASE_URL env var to override completely.
"""
import os

import psycopg2
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Register every model with Base.metadata before create_all
import app.models.cart  # noqa: F401
import app.models.menu_item  # noqa: F401
import app.models.order  # noqa: F401
import app.models.refresh_token  # noqa: F401
import app.models.restaurant  # noqa: F401
import app.models.user  # noqa: F401
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app

# Derive test URL from the app's DATABASE_URL (picks up correct password from .env)
# Just swap the database name to justeats_test.
_base_url = settings.DATABASE_URL.rsplit("/", 1)[0]
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    f"{_base_url}/justeats_test",
)


def _ensure_test_db_exists() -> None:
    """Create the justeats_test database if it does not already exist."""
    # Parse connection params from the asyncpg URL
    # Format: postgresql+asyncpg://user:password@host:port/dbname
    url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "")
    userinfo, rest = url.split("@", 1)
    user, password = userinfo.split(":", 1)
    host_port, dbname = rest.rsplit("/", 1)
    parts = host_port.split(":", 1)
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 5432

    conn = psycopg2.connect(
        host=host, port=port, user=user, password=password, dbname="postgres"
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{dbname}"')
        cur.close()
    finally:
        conn.close()


# Create the test database once at import time (before any fixture runs)
_ensure_test_db_exists()


# ── Session-scoped engine: create tables once, drop after the full run ────────


@pytest_asyncio.fixture(scope="session")
async def engine():
    _engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield _engine
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()


# ── Wipe every table after each test ─────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine):
    yield
    async with engine.begin() as conn:
        # Disable FK checks temporarily so we can truncate in any order
        await conn.execute(text("SET session_replication_role = replica"))
        for table in Base.metadata.sorted_tables:
            await conn.execute(table.delete())
        await conn.execute(text("SET session_replication_role = DEFAULT"))


# ── HTTP client with get_db overridden to use the test engine ─────────────────


@pytest_asyncio.fixture
async def client(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
