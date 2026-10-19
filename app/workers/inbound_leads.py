"""Lot 83 — boîte des demandes des agences (portails, formulaires) lue chaque minute ; demandes effacées après un an."""
import asyncio

from app.workers.celery_app import celery_app


async def _with_db(work):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await work(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.inbound_leads.read_leads_inbox_task")
def read_leads_inbox_task() -> dict:
    from app.services.inbound_leads import process_inbox

    return asyncio.run(_with_db(process_inbox))


@celery_app.task(name="app.workers.inbound_leads.purge_leads_task")
def purge_leads_task() -> int:
    from app.services.inbound_leads import purge

    return asyncio.run(_with_db(purge))
