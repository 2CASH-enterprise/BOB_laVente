"""Lot 87 — retouche des photos en arrière-plan : fond blanc studio (chaque minute, 3 photos au plus) et photos
portées terminées chez FASHN (chaque minute)."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.photo_studio.retouch_photos_task")
def retouch_photos_task() -> dict:
    from app.services.photo_studio import run_pending

    return asyncio.run(_with_db(lambda db: run_pending(db, limit=3)))


@celery_app.task(name="app.workers.photo_studio.collect_worn_task")
def collect_worn_task() -> dict:
    from app.core.config import get_settings
    from app.services.photo_studio import collect_worn

    if not get_settings().fashn_api_key:
        return {"skipped": "FASHN non configuré"}
    return asyncio.run(_with_db(collect_worn))
