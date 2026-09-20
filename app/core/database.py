"""
Connexion PostgreSQL via SQLAlchemy 2.x (async).
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Classe de base pour tous les modèles ORM (app/models/)."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency FastAPI : une session par requête, fermée automatiquement."""
    async with AsyncSessionLocal() as session:
        yield session
