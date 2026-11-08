"""Lot 113 — positions de livraison effacées 30 jours après la livraison (chaque nuit)."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.positions.purge_positions_task")
def purge_positions_task() -> dict:
    from app.services.geo_ci import purge_positions

    return {"cleared": asyncio.run(_with_db(purge_positions))}
