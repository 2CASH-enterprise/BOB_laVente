"""
Lot 114 — formulaire WhatsApp « Réserver une table » dans le Super Admin : interrupteur de la plateforme (éteint par
défaut) ; pour un restaurant : suivre la plateforme, essai (allumé ici seulement) ou éteint, statut chez Meta,
« Créer / vérifier le formulaire ».
"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.tenant import Tenant
from app.models.whatsapp_flow import WhatsAppFlow
from app.services import wa_flows
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])


class Switch(BaseModel):
    enabled: bool


class Mode(BaseModel):
    mode: Literal["AUTO", "ON", "OFF"]


async def _settings(db) -> dict:
    counts = dict((await db.execute(select(WhatsAppFlow.status, func.count()).group_by(WhatsAppFlow.status))).all())
    trial = (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.wa_flows == "ON"))).scalar_one()
    return {"enabled": await wa_flows.enabled(db), "published": counts.get("PUBLISHED", 0), "errors": counts.get("ERROR", 0),
            "trial": trial}


@router.get("/wa-flows/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await _settings(db)


@router.put("/wa-flows/settings")
async def put_settings(payload: Switch, db: AsyncSession = Depends(get_db),
                       current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    if payload.enabled != await wa_flows.enabled(db):
        await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}",
                              action="WA_FLOWS_ON" if payload.enabled else "WA_FLOWS_OFF")
    await wa_flows.set_enabled(db, payload.enabled)
    return await _settings(db)


async def _tenant(db, tenant_id: UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Boutique introuvable")
    return tenant


async def _overview(db, tenant) -> dict:
    from app.services.business_type import is_restaurant

    row = await wa_flows._row(db, tenant.id)
    return {"restaurant": is_restaurant(tenant), "mode": wa_flows.tenant_mode(tenant), "platform": await wa_flows.enabled(db),
            "active": await wa_flows.active_for(db, tenant), "status": row.status if row else "NOT_CREATED",
            "reason": row.reason if row else None, "version": row.version if row else None}


@router.get("/tenants/{tenant_id}/wa-flows")
async def tenant_flows(tenant_id: UUID, db: AsyncSession = Depends(get_db),
                       _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await _overview(db, await _tenant(db, tenant_id))


@router.put("/tenants/{tenant_id}/wa-flows/mode")
async def set_mode(tenant_id: UUID, payload: Mode, db: AsyncSession = Depends(get_db),
                   current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    tenant = await _tenant(db, tenant_id)
    value = None if payload.mode == "AUTO" else payload.mode
    if tenant.wa_flows != value:
        tenant.wa_flows = value
        await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", tenant_id=tenant.id,
                              action=f"WA_FLOWS_MODE_{payload.mode}")
        await db.commit()
    return await _overview(db, tenant)


@router.post("/tenants/{tenant_id}/wa-flows/sync")
async def sync(tenant_id: UUID, db: AsyncSession = Depends(get_db),
               _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    """Crée et publie le formulaire chez Meta tout de suite (sans attendre une réservation), ou montre le refus."""
    from app.services.business_type import is_restaurant

    tenant = await _tenant(db, tenant_id)
    if not is_restaurant(tenant):
        raise HTTPException(status_code=422, detail="Le formulaire de réservation est réservé aux restaurants.")
    await wa_flows.ensure(db, tenant, force=True)
    await db.commit()
    return await _overview(db, tenant)
