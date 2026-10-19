"""Lot 73 — démos non transformées en compte effacées 30 jours après leur création (chaque nuit)."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.demo_purge.purge_demos_task")
def purge_demos_task() -> dict:
    from app.services.demo_purge import purge_demos

    return asyncio.run(_with_db(purge_demos))
