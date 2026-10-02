"""
Tâche planifiée : parcourt les boutiques actives et envoie les relances par email dues (section 23,
lot 49 : email uniquement, jamais WhatsApp). Exécutée toutes les 15 minutes par Celery Beat.

Lot 50 — la tâche n'était pas enregistrée auprès du worker : les relances ne partaient jamais.
Une session par exécution (moteur sans pool : asyncio.run crée une nouvelle boucle à chaque fois).
"""
import asyncio
import logging

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_followups(db, now=None, send_email=None) -> int:
    from app.models.tenant import Tenant
    from app.services.followup_service import run_followups_for_tenant

    total_sent = 0
    tenant_ids = (await db.execute(select(Tenant.id).where(Tenant.active.is_(True)))).scalars().all()
    for tenant_id in tenant_ids:
        try:
            total_sent += await run_followups_for_tenant(db, tenant_id, now=now, send_email=send_email)
        except Exception:  # noqa: BLE001 — une boutique en échec ne doit jamais bloquer les autres
            logger.exception("Échec du traitement des relances pour le tenant %s", tenant_id)
            await db.rollback()
    return total_sent


async def _run() -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await check_followups(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.followups.check_followups_task")
def check_followups_task() -> int:
    return asyncio.run(_run())
