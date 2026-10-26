"""Lot 89 — connecteur Naomy : envoi des événements (chaque minute, avec renvois) et ménage de la nuit."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.naomy.send_naomy_events_task")
def send_naomy_events_task() -> dict:
    from app.services.naomy_bridge import dispatch

    return asyncio.run(_with_db(dispatch))


@celery_app.task(name="app.workers.naomy.purge_naomy_task")
def purge_naomy_task() -> dict:
    from app.services.naomy_bridge import purge

    return asyncio.run(_with_db(purge))
