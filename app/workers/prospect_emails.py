"""Lot 62 — emails de prospection : un envoi toutes les 3 minutes (sous le plafond), lecture de la boîte toutes les 15 minutes."""
import asyncio

from app.workers.celery_app import celery_app


async def _with_db(work):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await work(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.prospect_emails.send_prospect_email_task")
def send_prospect_email_task() -> str:
    from app.services.prospect_mailer import send_next

    return asyncio.run(_with_db(send_next))


@celery_app.task(name="app.workers.prospect_emails.read_prospect_inbox_task")
def read_prospect_inbox_task() -> dict:
    from app.services.prospect_mailer import process_inbox

    return asyncio.run(_with_db(process_inbox))
