"""Lot 61 — chaque nuit : effacer les prospects ajoutés il y a plus d'un an qui n'ont jamais réagi."""
import asyncio

from app.workers.celery_app import celery_app


async def _run() -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings
    from app.services.prospection import purge_stale

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await purge_stale(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.prospects.purge_prospects_task")
def purge_prospects_task() -> int:
    return asyncio.run(_run())
