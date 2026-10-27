"""Lot 92 — « Bob, votre assistant » : traitement d'un message reçu, et la tâche de chaque minute (alertes, envois,
bilan du soir). Rien ne se passe tant que ASSISTANT_ENABLED est éteint ou qu'aucune boutique n'a allumé l'assistant."""
import asyncio
import uuid

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.assistant.process_assistant_message_task")
def process_assistant_message_task(message_id: str) -> str:
    from app.services.merchant_assistant import process

    async def run(db):
        return await process(db, uuid.UUID(message_id))

    return asyncio.run(_with_db(run))


@celery_app.task(name="app.workers.assistant.assistant_tick_task")
def assistant_tick_task() -> dict:
    from app.services.merchant_assistant import tick

    return asyncio.run(_with_db(tick))
