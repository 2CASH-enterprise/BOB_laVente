"""
Lot 68 — modèles WhatsApp : toutes les heures, statut chez Meta des modèles encore en attente (au cas où le
webhook « message_template_status_update » n'est pas arrivé). Rien si l'interrupteur est éteint.
"""
import asyncio

from app.workers.celery_app import celery_app


async def refresh() -> dict:
    from app.services import wa_templates
    from app.workers.monitoring import _with_session

    return await _with_session(wa_templates.refresh_pending)


@celery_app.task(name="app.workers.wa_templates.refresh_templates_task")
def refresh_templates_task() -> dict:
    return asyncio.run(refresh())


@celery_app.task(name="app.workers.wa_templates.refresh_accounts_task")
def refresh_accounts_task() -> dict:
    """Lot 73 d — chaque nuit : numéro, nom vérifié et statut du nom de chaque compte WhatsApp, d'après Meta."""
    from app.services import wa_account_info
    from app.workers.monitoring import _with_session

    return asyncio.run(_with_session(wa_account_info.refresh_all))
