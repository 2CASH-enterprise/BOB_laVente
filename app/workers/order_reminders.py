"""
Lot 63 — relances des commandes non payées (boutiques en ligne) : tâche lancée toutes les heures, chaque
boutique dans sa propre session ; un échec n'empêche jamais les autres. Règles dans
app/services/order_reminders.py.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_order_reminders(session_factory=None, now: datetime | None = None, send_email=None,
                                send_whatsapp=None) -> dict:
    from app.models.tenant import Tenant
    from app.services import order_reminders

    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    now = now or datetime.now(timezone.utc)
    total = {"whatsapp": 0, "email": 0, "none": 0, "failed": 0}
    try:
        async with session_factory() as db:
            tenant_ids = (await db.execute(select(Tenant.id).where(
                Tenant.active.is_(True), Tenant.is_demo.is_(False),
                Tenant.order_reminders_enabled.is_(True),
            ))).scalars().all()  # le secteur (boutique en ligne) est vérifié par run_for_tenant
        for tenant_id in tenant_ids:
            async with session_factory() as db:
                try:
                    report = await order_reminders.run_for_tenant(
                        db, await db.get(Tenant, tenant_id), now, send_email=send_email, send_whatsapp=send_whatsapp)
                    for key, value in report.items():
                        total[key] += value
                except Exception:  # noqa: BLE001 — une boutique en échec ne bloque jamais les autres
                    total["failed"] += 1
                    logger.exception("Relances de commandes impossibles (boutique %s)", tenant_id)
    finally:
        if engine is not None:
            await engine.dispose()
    return total


@celery_app.task(name="app.workers.order_reminders.check_order_reminders_task")
def check_order_reminders_task() -> dict:
    return asyncio.run(check_order_reminders())
