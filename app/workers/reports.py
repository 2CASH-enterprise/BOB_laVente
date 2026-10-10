"""
Lot 60 — rapport mensuel de début de mois (du 1er au 3, après 8 h, heure du pays) pour les boutiques sans
échéance proche (plan gratuit, abonnement long). Celles dont l'abonnement se termine dans 14 jours ou moins
reçoivent le leur 7 jours avant l'échéance, avec l'email d'échéance (app/workers/billing.py).
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_monthly_reports(db, now: datetime | None = None, send=None) -> list[str]:
    """Renvoie le nom des boutiques à qui le rapport est parti."""
    from app.models.superadmin_user import SuperAdminUser
    from app.models.tenant import Tenant
    from app.services import monthly_report

    now = now or datetime.now(timezone.utc)
    tenants = (await db.execute(select(Tenant).where(Tenant.active.is_(True), Tenant.is_demo.is_(False)))).scalars().all()
    reply_to = (await db.execute(select(SuperAdminUser.email).where(SuperAdminUser.active.is_(True)).limit(1))).scalar_one_or_none()
    done = []
    for tenant in tenants:
        if not monthly_report.monthly_due(tenant, now):
            continue
        try:
            sent = await monthly_report.send_report(db, tenant, now, send=send, reply_to=reply_to)
        except Exception:  # noqa: BLE001 — une boutique en échec ne bloque jamais les autres
            logger.exception("Rapport mensuel impossible (boutique %s)", tenant.id)
            continue
        if sent:
            await db.commit()
            done.append(tenant.name)
    return done


async def _run() -> list:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await check_monthly_reports(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.reports.check_monthly_reports_task")
def check_monthly_reports_task() -> list:
    return asyncio.run(_run())
