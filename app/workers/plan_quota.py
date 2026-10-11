"""
Lot 69 — plafonds des formules : toutes les heures, un email à la boutique à 80 % puis à 100 % de sa formule
(une fois par seuil et par mois). Bob ne s'arrête pas, sauf si le Super Admin a choisi le mode « pause ».
"""
import asyncio

from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.plan_quota.check_quotas_task")
def check_quotas_task() -> dict:
    from app.services import plan_quota
    from app.workers.monitoring import _with_session

    return asyncio.run(_with_session(plan_quota.check_all))
