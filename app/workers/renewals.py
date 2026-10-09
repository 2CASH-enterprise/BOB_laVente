"""
Lot 55 — échéances des contrats du courtier : récapitulatif quotidien au cabinet (à partir de 8 h, heure du
pays) et rappel unique au client (entre 9 h et 20 h). Tâche lancée toutes les heures ; chaque boutique est
traitée dans sa propre session, un échec n'empêche jamais les autres. Règles dans
app/services/insurance_contracts.py.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_renewals(session_factory=None, now: datetime | None = None, send_email=None, send_whatsapp=None) -> dict:
    from app.models.tenant import Tenant
    from app.services import insurance_contracts
    from app.services.business_type import INSURANCE_BROKER

    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    now = now or datetime.now(timezone.utc)
    total = {"digest": 0, "whatsapp": 0, "email": 0, "call": 0, "failed": 0}
    try:
        async with session_factory() as db:
            tenant_ids = (await db.execute(select(Tenant.id).where(
                Tenant.business_type == INSURANCE_BROKER, Tenant.active.is_(True), Tenant.is_demo.is_(False),
            ))).scalars().all()
        for tenant_id in tenant_ids:
            async with session_factory() as db:
                try:
                    report = await insurance_contracts.run_for_tenant(
                        db, await db.get(Tenant, tenant_id), now, send_email=send_email, send_whatsapp=send_whatsapp)
                    for key, value in report.items():
                        total[key] += value
                except Exception:  # noqa: BLE001 — une boutique en échec ne bloque jamais les autres
                    total["failed"] += 1
                    logger.exception("Échéances des contrats impossibles (boutique %s)", tenant_id)
    finally:
        if engine is not None:
            await engine.dispose()
    return total


@celery_app.task(name="app.workers.renewals.check_renewals_task")
def check_renewals_task() -> dict:
    return asyncio.run(check_renewals())
