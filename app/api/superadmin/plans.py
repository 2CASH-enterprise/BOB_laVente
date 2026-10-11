"""
Lot 69 — formules dans le Super Admin : plafonds de conversations, conduite au dépassement (prévenir / mettre en
pause), et pour une boutique : conversations du mois, plafond, coût IA à côté du prix de la formule.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, CurrentUser, get_current_superadmin, get_current_user
from app.models.tenant import Tenant
from app.services import plan_quota
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])
tenant_router = APIRouter(prefix="/api/v1/tenants/me", tags=["tenants"])

USD_TO_EUR = 0.92  # estimation pour comparer le coût IA (facturé en dollars) au prix de la formule (en euros)


class PlansIn(BaseModel):
    quotas: dict[str, int | None] = Field(default_factory=dict)
    mode: str = "WARN"


async def _plans(db) -> dict:
    conf = await plan_quota.settings(db)
    return {"mode": conf["mode"], "modes": [{"value": "WARN", "label": "Prévenir seulement (Bob continue)"},
                                             {"value": "PAUSE", "label": "Mettre Bob en pause jusqu'au mois suivant"}],
            "plans": [{"plan": p, "label": plan_quota.LABELS[p], "quota": conf["quotas"].get(p),
                       "price": plan_quota.price_label(p)} for p in plan_quota.ORDER]}


@router.get("/plans")
async def get_plans(db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await _plans(db)


@router.put("/plans")
async def put_plans(payload: PlansIn, db: AsyncSession = Depends(get_db),
                    current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    from app.services.preregistration import settings_row

    if payload.mode not in plan_quota.MODES:
        raise HTTPException(status_code=422, detail="Conduite inconnue")
    quotas = {}
    for plan, value in payload.quotas.items():
        if plan not in plan_quota.ORDER:
            raise HTTPException(status_code=422, detail=f"Formule inconnue : {plan}")
        if value is not None and not 1 <= value <= 1_000_000:
            raise HTTPException(status_code=422, detail="Un plafond va de 1 à 1 000 000 conversations (vide = sans limite)")
        quotas[plan] = value
    row = await settings_row(db)
    before = await _plans(db)
    row.plan_quotas = {**(row.plan_quotas or {}), **quotas}
    row.quota_mode = payload.mode
    await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", action="PLAN_QUOTAS_CHANGED",
                          details={"from": {"mode": before["mode"]}, "to": {"mode": payload.mode, "quotas": quotas}})
    await db.commit()
    return await _plans(db)


@router.get("/tenants/{tenant_id}/plan-usage")
async def tenant_plan_usage(tenant_id: UUID, db: AsyncSession = Depends(get_db),
                            _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    from app.services.llm_usage_service import costs_by_tenant, month_bounds

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Boutique introuvable")
    u = await plan_quota.usage(db, tenant)
    start, end, label = month_bounds(None)
    cost = round(sum(r["cost_usd"] for r in await costs_by_tenant(db, start, end, tenant_id=tenant.id)), 2)
    plan = getattr(tenant.plan, "value", tenant.plan)
    price = plan_quota.PRICES.get(plan)
    share = round(cost * USD_TO_EUR * 100 / price[1]) if price else None
    return {"usage": u, "ai_cost_usd": cost, "ai_cost_eur": round(cost * USD_TO_EUR, 2), "month": label,
            "price": plan_quota.price_label(plan) if plan in plan_quota.ORDER else None,
            "ai_share_pct": share, "costly": share is not None and share >= 50}


@tenant_router.get("/plan-usage")
async def my_plan_usage(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Lot 69 — conversations du mois et plafond de la formule (bandeau de l'accueil à partir de 80 %)."""
    return {"usage": await plan_quota.usage(db, await db.get(Tenant, current_user.tenant_id))}
