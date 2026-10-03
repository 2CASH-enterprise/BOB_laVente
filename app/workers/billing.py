"""
Lot 51 — échéance de l'abonnement : toutes les heures, chaque boutique qui a une date « payé jusqu'au »
reçoit au plus un email par étape (7 jours avant, jour de grâce, pause). La pause elle-même n'a pas
besoin de tâche : elle est calculée à chaque message (app/services/bob_pause.py).
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_billing(db, now: datetime | None = None, send=None) -> list[tuple[str, str]]:
    """Renvoie [(boutique, étape)] des emails envoyés. L'étape n'est notée qu'une fois l'email parti."""
    from app.models.superadmin_user import SuperAdminUser
    from app.models.tenant import Tenant
    from app.services import bob_pause

    if send is None:
        from app.services.email_service import send_email as send
    now = now or datetime.now(timezone.utc)
    tenants = (await db.execute(select(Tenant).where(Tenant.paid_until.is_not(None), Tenant.active.is_(True)))).scalars().all()
    reply_to = (await db.execute(select(SuperAdminUser.email).where(SuperAdminUser.active.is_(True)).limit(1))).scalar_one_or_none()
    sent = []
    for tenant in tenants:
        stage = bob_pause.notice_due(tenant, now)
        if stage is None:
            continue
        subject, body = bob_pause.billing_email(tenant, stage)
        try:
            ok = send(to=tenant.email, subject=subject, body=body, from_name="Bob", reply_to=reply_to)
        except Exception:  # noqa: BLE001 — une boutique en échec ne bloque jamais les autres
            logger.warning("Email d'échéance impossible (boutique %s)", tenant.id)
            ok = False
        if not ok:
            continue
        tenant.billing_notice_stage, tenant.billing_notice_for = stage, tenant.paid_until
        await db.commit()
        sent.append((tenant.name, stage))
    return sent


async def _run() -> list:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await check_billing(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.billing.check_billing_task")
def check_billing_task() -> list:
    return asyncio.run(_run())
