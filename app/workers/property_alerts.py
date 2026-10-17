"""
Lot 80 — alertes « nouveau bien » automatiques (agences au plan Pro qui les ont activées). Tâche lancée toutes les
30 minutes ; chaque agence est traitée dans sa propre session, un échec n'empêche jamais les autres. Règles dans
app/services/property_alerts.py.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def run_alerts(session_factory=None, now: datetime | None = None, send_email=None, send_whatsapp=None) -> dict:
    from app.models.tenant import Tenant
    from app.services import property_alerts
    from app.services.business_type import REAL_ESTATE

    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    now = now or datetime.now(timezone.utc)
    total = {"sent": 0, "failed": 0}
    try:
        async with session_factory() as db:
            tenant_ids = (await db.execute(select(Tenant.id).where(
                Tenant.business_type == REAL_ESTATE, Tenant.property_alerts_auto.is_(True), Tenant.active.is_(True),
                Tenant.is_demo.is_(False),
            ))).scalars().all()
        for tenant_id in tenant_ids:
            async with session_factory() as db:
                try:
                    total["sent"] += await property_alerts.run_automatic(
                        db, await db.get(Tenant, tenant_id), now, send_email=send_email, send_whatsapp=send_whatsapp)
                except Exception:  # noqa: BLE001 — une agence en échec ne bloque jamais les autres
                    total["failed"] += 1
                    logger.exception("Alertes « nouveau bien » impossibles (agence %s)", tenant_id)
    finally:
        if engine is not None:
            await engine.dispose()
    return total


@celery_app.task(name="app.workers.property_alerts.run_alerts_task")
def run_alerts_task() -> dict:
    return asyncio.run(run_alerts())
