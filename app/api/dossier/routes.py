"""
Lot 58 — « Dossier du client » du courtier (règlement CIMA 01-24, art. 5 et 15 : piste d'audit, contrôles).
Page autonome et imprimable ; chaque édition est notée dans le journal d'audit.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import ROLE_HIERARCHY, CurrentUser, get_current_user, require_role
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.models.user import User
from app.services.audit import log_audit_event
from app.services.business_type import only_insurance

router = APIRouter(prefix="/api/v1/customers", tags=["dossier"], dependencies=[Depends(only_insurance())])


@router.get("/{customer_id}/dossier", response_class=HTMLResponse, dependencies=[Depends(require_role("AGENT"))])
async def customer_dossier(
    customer_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    from app.services.insurance_exports import dossier_html

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Client introuvable")
    tenant = await db.get(Tenant, current_user.tenant_id)
    user = await db.get(User, current_user.user_id)
    label = (user.full_name or user.email) if user else "le cabinet"
    admin = ROLE_HIERARCHY.get(current_user.role, -1) >= ROLE_HIERARCHY["ADMIN"]
    page = await dossier_html(db, tenant, customer, label, admin)
    await log_audit_event(db, actor=str(current_user.user_id), action="CUSTOMER_DOSSIER_EXPORTED", tenant_id=tenant.id,
                          details={"customer_id": str(customer.id)})
    await db.commit()
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
