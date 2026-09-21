import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.core.database import Base, get_db  # noqa: E402
from app.core.rate_limit import InMemoryRateLimiter  # noqa: E402
from app.core.rate_limit_dependency import get_rate_limiter  # noqa: E402
from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # Rate limiter en mémoire, frais à chaque test : jamais de vrai Redis en environnement de test,
    # et jamais de fuite d'état entre deux tests. Une SEULE instance par test (capturée dans la
    # fermeture) : sinon FastAPI en recrée une neuve à chaque requête et les compteurs ne persistent pas.
    test_rate_limiter = InMemoryRateLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: test_rate_limiter

    async with session_factory() as session:
        yield session

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def unique_email():
    return f"user_{uuid.uuid4().hex[:8]}@example.com"
