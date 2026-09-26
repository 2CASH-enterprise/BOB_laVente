"""
Tâche planifiée : recalcule chaque nuit les opportunités de vente de tous les tenants actifs
(phase 0 de l'apprentissage — mesure des issues). Un tenant en échec ne bloque jamais les autres.
"""
import asyncio
import logging

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.tenant import Tenant
from app.services.opportunity_service import recompute_tenant_opportunities
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _recompute_all_async() -> int:
    total = 0
    async with AsyncSessionLocal() as db:
        tenant_ids = (await db.execute(select(Tenant.id).where(Tenant.active.is_(True)))).scalars().all()
        for tenant_id in tenant_ids:
            try:
                total += await recompute_tenant_opportunities(db, tenant_id)
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.exception("Échec du calcul des opportunités pour le tenant %s", tenant_id)
    return total


@celery_app.task(name="app.workers.opportunities.recompute_opportunities_task")
def recompute_opportunities_task() -> int:
    return asyncio.run(_recompute_all_async())
