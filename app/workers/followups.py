"""
Tâche planifiée : parcourt tous les tenants ayant activé les relances (section 23)
et envoie celles qui sont dues. Exécutée toutes les 15 minutes par Celery Beat.
"""
import asyncio
import logging

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.tenant import Tenant
from app.services.followup_service import run_followups_for_tenant
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _check_followups_async() -> int:
    total_sent = 0
    async with AsyncSessionLocal() as db:
        tenant_ids = (await db.execute(select(Tenant.id).where(Tenant.active.is_(True)))).scalars().all()
        for tenant_id in tenant_ids:
            try:
                sent = await run_followups_for_tenant(db, tenant_id)
                total_sent += sent
            except Exception:  # noqa: BLE001 — un tenant en échec ne doit jamais bloquer les autres
                logger.exception("Échec du traitement des relances pour le tenant %s", tenant_id)
    return total_sent


@celery_app.task(name="app.workers.followups.check_followups_task")
def check_followups_task() -> int:
    return asyncio.run(_check_followups_async())
