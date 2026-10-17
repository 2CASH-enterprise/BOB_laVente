"""
Lot 80 — alertes « nouveau bien » et bailleurs en DPE F ou G (agence immobilière). Réservé à l'agence (verrou côté
serveur), toujours limité aux biens et aux contacts de SA boutique ; plafonds et options Pro vérifiés ici aussi.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.product import Product
from app.models.tenant import Tenant
from app.services import property_alerts
from app.services.audit import log_audit_event
from app.services.business_type import only_real_estate

router = APIRouter(prefix="/api/v1/property-alerts", tags=["property-alerts"], dependencies=[Depends(only_real_estate())])


class SettingsIn(BaseModel):
    auto: bool | None = None
    templates: bool | None = None


async def _tenant(db, current_user) -> Tenant:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not property_alerts.available(tenant):
        raise HTTPException(status_code=403, detail=property_alerts.NOT_AVAILABLE)
    return tenant


async def _settings(db, tenant: Tenant) -> dict:
    from app.services import wa_templates

    row = await wa_templates.approved(db, tenant, "nouveau_bien") if property_alerts.is_pro(tenant) else None
    return {
        "available": property_alerts.available(tenant), "pro": property_alerts.is_pro(tenant),
        "quota": property_alerts.monthly_quota(tenant), "used": await property_alerts.used_this_month(db, tenant),
        "auto": bool(tenant.property_alerts_auto) and property_alerts.is_pro(tenant),
        "templates": bool(tenant.property_alert_templates) and property_alerts.is_pro(tenant),
        "template_approved": row is not None,
    }


@router.get("/settings")
async def get_settings(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not property_alerts.available(tenant):
        return {"available": False, "message": property_alerts.NOT_AVAILABLE}
    return await _settings(db, tenant)


@router.put("/settings", dependencies=[Depends(require_role("ADMIN"))])
async def put_settings(payload: SettingsIn, current_user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    if not property_alerts.is_pro(tenant):
        raise HTTPException(status_code=403, detail=property_alerts.PRO_ONLY)
    if payload.auto is not None:
        tenant.property_alerts_auto = payload.auto
    if payload.templates is not None:
        tenant.property_alert_templates = payload.templates
    await log_audit_event(db, actor=str(current_user.user_id), action="PROPERTY_ALERTS_SETTINGS", tenant_id=tenant.id,
                          details={"auto": tenant.property_alerts_auto, "templates": tenant.property_alert_templates})
    await db.commit()
    return await _settings(db, tenant)


@router.get("/counts")
async def counts(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not property_alerts.available(tenant):
        return {}
    return await property_alerts.match_counts(db, tenant)


async def _product(db, tenant, product_id: UUID) -> Product:
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Bien introuvable")
    return product


@router.get("/products/{product_id}")
async def product_matches(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    product = await _product(db, tenant, product_id)
    items = property_alerts.public(await property_alerts.candidates(db, tenant, product))
    return {"items": items, "ready": sum(1 for i in items if i["blocked"] is None),
            "quota_left": await property_alerts.quota_left(db, tenant)}


@router.post("/products/{product_id}/send", dependencies=[Depends(require_role("MANAGER"))])
async def send_alerts(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    product = await _product(db, tenant, product_id)
    report = await property_alerts.send_for_product(db, tenant, product, str(current_user.user_id))
    await log_audit_event(db, actor=str(current_user.user_id), action="PROPERTY_ALERTS_SENT", tenant_id=tenant.id,
                          details={"product_id": str(product.id), "sent": report["sent"]})
    await db.commit()
    return report


@router.get("/dpe-owners")
async def dpe_owners(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not property_alerts.is_pro(tenant):
        raise HTTPException(status_code=403, detail=property_alerts.PRO_ONLY)
    return {"items": await property_alerts.dpe_owners(db, tenant)}
