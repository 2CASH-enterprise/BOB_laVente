"""
Lot 50 — alerte de budget IA : toutes les heures, une boutique dont le coût IA estimé du mois dépasse
le seuil (settings.llm_monthly_alert_usd) déclenche UN email aux Super Admins (une fois par mois).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def check_budgets(db, now: datetime | None = None, send=None) -> list[str]:
    """Renvoie les boutiques signalées. L'alerte est réservée en base AVANT l'envoi (jamais deux fois)."""
    from sqlalchemy.exc import IntegrityError

    from app.core.config import get_settings
    from app.models.llm_usage import LlmBudgetAlert
    from app.models.superadmin_user import SuperAdminUser
    from app.services.llm_usage_service import costs_by_tenant, month_bounds

    if send is None:
        from app.services.email_service import send_email as send
    threshold = get_settings().llm_monthly_alert_usd
    start, end, month = month_bounds(None, now)
    over = [r for r in await costs_by_tenant(db, start, end) if r["cost_usd"] > threshold]
    if not over:
        return []
    admins = (await db.execute(select(SuperAdminUser.email))).scalars().all()
    flagged = []
    for row in over:
        try:
            async with db.begin_nested():
                db.add(LlmBudgetAlert(tenant_id=uuid.UUID(row["tenant_id"]), month=month, cost_usd=f"{row['cost_usd']:.2f}"))
        except IntegrityError:
            continue  # déjà signalée ce mois-ci
        await db.commit()
        flagged.append(row["tenant"])
        body = (f"Le coût IA estimé de « {row['tenant']} » a dépassé {threshold:.0f} $ ce mois-ci.\n\n"
                f"Coût estimé : {row['cost_usd']:.2f} $\n"
                f"Conversations : {row['conversations']}\n"
                f"Taux de cache : {row['cache_rate_pct'] if row['cache_rate_pct'] is not None else '—'} %\n\n"
                "Détail : Super Admin → Coûts IA.\n— Bob AI")
        for email in admins:
            send(to=email, subject=f"Budget IA dépassé — {row['tenant']} ({month})", body=body)
    return flagged


async def _run() -> list[str]:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)() as db:
            return await check_budgets(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.llm_budget.check_budgets_task")
def check_budgets_task() -> list[str]:
    return asyncio.run(_run())
