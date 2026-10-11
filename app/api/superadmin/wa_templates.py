"""
Lot 68 — modèles WhatsApp dans le Super Admin : interrupteur de la plateforme (éteint par défaut), et pour une
boutique : autoriser ou non, voir le statut chez Meta, « Créer / vérifier les modèles ».
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.tenant import Tenant
from app.models.whatsapp_template import WhatsAppTemplate
from app.services import wa_templates
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])


class Switch(BaseModel):
    enabled: bool


class Allowed(BaseModel):
    allowed: bool


async def _settings(db) -> dict:
    counts = dict((await db.execute(select(WhatsAppTemplate.status, func.count()).group_by(WhatsAppTemplate.status))).all())
    shops = (await db.execute(select(func.count(func.distinct(WhatsAppTemplate.tenant_id))))).scalar_one()
    return {"enabled": await wa_templates.enabled(db), "shops": shops, "approved": counts.get("APPROVED", 0),
            "pending": counts.get("PENDING", 0) + counts.get("IN_APPEAL", 0),
            "rejected": counts.get("REJECTED", 0) + counts.get("ERROR", 0) + counts.get("DISABLED", 0) + counts.get("PAUSED", 0)}


@router.get("/wa-templates/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await _settings(db)


@router.put("/wa-templates/settings")
async def put_settings(payload: Switch, db: AsyncSession = Depends(get_db),
                       current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    if payload.enabled != await wa_templates.enabled(db):
        await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}",
                              action="WA_TEMPLATES_ON" if payload.enabled else "WA_TEMPLATES_OFF")
    await wa_templates.set_enabled(db, payload.enabled)
    return await _settings(db)


async def _tenant(db, tenant_id: UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Boutique introuvable")
    return tenant


@router.get("/tenants/{tenant_id}/wa-templates")
async def tenant_templates(tenant_id: UUID, db: AsyncSession = Depends(get_db),
                           _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await wa_templates.overview(db, await _tenant(db, tenant_id))


@router.post("/tenants/{tenant_id}/wa-templates/sync")
async def sync_templates(tenant_id: UUID, db: AsyncSession = Depends(get_db),
                         current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    tenant = await _tenant(db, tenant_id)
    report = await wa_templates.sync(db, tenant)
    if report["error"] is None:
        await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", action="WA_TEMPLATES_SYNCED",
                              tenant_id=tenant.id, details={"created": report["created"], "errors": report["errors"]})
        await db.commit()
    return {"report": report, **await wa_templates.overview(db, tenant)}


@router.put("/tenants/{tenant_id}/wa-templates/allowed")
async def set_allowed(tenant_id: UUID, payload: Allowed, db: AsyncSession = Depends(get_db),
                      current: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    tenant = await _tenant(db, tenant_id)
    if tenant.wa_templates_allowed != payload.allowed:
        tenant.wa_templates_allowed = payload.allowed
        await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", tenant_id=tenant.id,
                              action="WA_TEMPLATES_ALLOWED" if payload.allowed else "WA_TEMPLATES_BLOCKED")
        await db.commit()
    return await wa_templates.overview(db, tenant)
