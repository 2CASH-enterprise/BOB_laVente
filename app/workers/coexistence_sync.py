"""
Lot 52 — coexistence : toutes les 15 minutes, redemande à Meta les contacts et l'historique du téléphone
des numéros connectés depuis moins de 24 h dont la demande a échoué (Meta n'accepte plus après 24 h).
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def retry_syncs(db, now: datetime | None = None, oauth_client=None) -> dict:
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.coexistence import SYNC_WINDOW, request_syncs

    if oauth_client is None:
        from app.integrations.whatsapp.dependency import get_meta_oauth_client

        oauth_client = get_meta_oauth_client()
    now = now or datetime.now(timezone.utc)
    accounts = (await db.execute(select(WhatsAppAccount).where(
        WhatsAppAccount.coexistence_mode.is_(True),
        WhatsAppAccount.connected_at >= now - SYNC_WINDOW,
        or_(WhatsAppAccount.contacts_sync_requested_at.is_(None), WhatsAppAccount.history_sync_requested_at.is_(None)),
    ))).scalars().all()
    report = {}
    for account in accounts:
        try:
            report[account.phone_number_id] = await request_syncs(db, account, oauth_client, now)
        except Exception:  # noqa: BLE001 — un numéro en échec ne bloque jamais les autres
            logger.warning("Synchronisation coexistence en échec (numéro %s)", account.phone_number_id)
            await db.rollback()
    return report


async def _run() -> dict:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await retry_syncs(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.coexistence_sync.retry_syncs_task")
def retry_syncs_task() -> dict:
    return asyncio.run(_run())
