"""
Lot 81 — pige : chaque heure, charge les ventes DVF des départements où les agences ont des annonces à vendre (une
seule fois, puis tous les 6 mois) ; chaque nuit, efface les annonces sans activité depuis 6 mois. Un département en
échec n'empêche jamais les autres.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _session_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def departments_needed(db) -> list[str]:
    from app.models.pige import PigeListing
    from app.services import dvf

    codes = (await db.execute(select(PigeListing.postal_code).where(
        PigeListing.transaction == "VENTE", PigeListing.postal_code.is_not(None)).distinct())).scalars().all()
    return sorted({d for d in (dvf.department_of(c) for c in codes) if d})


async def refresh_dvf(session_factory=None, now: datetime | None = None, fetch=None) -> dict:
    from app.services import dvf

    engine = None
    if session_factory is None:
        engine, session_factory = _session_factory()
    now = now or datetime.now(timezone.utc)
    report = {"loaded": [], "failed": 0}
    try:
        async with session_factory() as db:
            needed = [d for d in await departments_needed(db) if await dvf.needs_loading(db, d, now)]
        for department in needed[:3]:  # 3 départements par heure au plus : jamais un serveur saturé
            async with session_factory() as db:
                try:
                    await dvf.load_department(db, department, now, fetch)
                    report["loaded"].append(department)
                except Exception:  # noqa: BLE001
                    report["failed"] += 1
                    logger.exception("Ventes DVF impossibles (département %s)", department)
    finally:
        if engine is not None:
            await engine.dispose()
    return report


async def purge_listings(session_factory=None, now: datetime | None = None) -> int:
    from app.services import pige

    engine = None
    if session_factory is None:
        engine, session_factory = _session_factory()
    try:
        async with session_factory() as db:
            return await pige.purge(db, now)
    finally:
        if engine is not None:
            await engine.dispose()


@celery_app.task(name="app.workers.pige.refresh_dvf_task")
def refresh_dvf_task() -> dict:
    return asyncio.run(refresh_dvf())


@celery_app.task(name="app.workers.pige.purge_listings_task")
def purge_listings_task() -> int:
    return asyncio.run(purge_listings())
