"""
Lot 37b — notifications sur l'appareil du commerçant.

- check_tenant_task : juste après un message WhatsApp reçu (nouvelle conversation à reprendre,
  rendez-vous demandé, commande créée par Bob, client à rappeler…).
- check_all_task : toutes les 10 minutes, pour ce qui apparaît avec le temps (rendez-vous passé
  sans issue, relance à faire à la main) ou depuis le tableau de bord. Seules les boutiques ayant
  au moins un appareil abonné sont vérifiées. Chaque boutique dans sa propre session.
"""
import asyncio
import logging
import uuid

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _run(job, session_factory=None):
    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        return await job(session_factory)
    finally:
        if engine is not None:
            await engine.dispose()


async def check_one(tenant_id, session_factory=None, sender=None) -> dict:
    from app.services import notifications

    async def job(factory):
        async with factory() as db:
            return await notifications.check_tenant(db, uuid.UUID(str(tenant_id)), sender=sender)

    return await _run(job, session_factory)


async def check_all(session_factory=None, sender=None) -> dict:
    from app.models.push_subscription import PushSubscription
    from app.services import notifications

    async def job(factory):
        async with factory() as db:
            tenant_ids = (await db.execute(select(PushSubscription.tenant_id).distinct())).scalars().all()
        report = {"tenants": 0, "sent": 0, "failed": 0}
        for tenant_id in tenant_ids:
            try:
                async with factory() as db:
                    result = await notifications.check_tenant(db, tenant_id, sender=sender)
                report["tenants"] += 1
                report["sent"] += result["sent"]
            except Exception:  # noqa: BLE001 — une boutique en échec ne bloque jamais les autres
                report["failed"] += 1
                logger.warning("Vérification des notifications impossible (boutique %s)", tenant_id)
        return report

    return await _run(job, session_factory)


@celery_app.task(name="app.workers.notifications.check_tenant_task")
def check_tenant_task(tenant_id: str) -> dict:
    return asyncio.run(check_one(tenant_id))


@celery_app.task(name="app.workers.notifications.check_all_task")
def check_all_task() -> dict:
    return asyncio.run(check_all())
