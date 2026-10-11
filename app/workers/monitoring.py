"""
Lot 67 — monitoring technique : chaque minute, les erreurs en file sont rangées en base (regroupées) et les
Super Admins prévenus si besoin (nouvelle erreur, pic) ; chaque nuit, ce qui a plus de 30 jours est effacé.
"""
import asyncio

from app.workers.celery_app import celery_app


async def _with_session(fn):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await fn(db)
    finally:
        await engine.dispose()


async def drain_and_alert(db) -> dict:
    from app.services import monitoring

    report = await monitoring.drain(db)
    report["alerts"] = await monitoring.send_alerts(db)
    return report


@celery_app.task(name="app.workers.monitoring.drain_task")
def drain_task() -> dict:
    return asyncio.run(_with_session(drain_and_alert))


@celery_app.task(name="app.workers.monitoring.purge_task")
def purge_task() -> dict:
    from app.services import monitoring

    return asyncio.run(_with_session(monitoring.purge))
