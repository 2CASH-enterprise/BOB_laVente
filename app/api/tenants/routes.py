from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.tenant import Tenant

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


class TenantResponse(BaseModel):
    id: UUID
    name: str
    country: str
    currency: str
    company_size: str
    active: bool

    model_config = ConfigDict(from_attributes=True)


@router.get("/me", response_model=TenantResponse)
async def get_my_tenant(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """
    Retourne UNIQUEMENT le tenant de l'utilisateur authentifié.
    Aucun tenant_id n'est accepté en paramètre : impossible de consulter un autre tenant (section 30).
    """
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant introuvable")
    return tenant


@router.put("/me/company-size", response_model=TenantResponse, dependencies=[Depends(require_role("ADMIN"))])
async def update_company_size(
    company_size: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Section 57.3 — le plan reste modifiable manuellement (ex. suite à une négociation B2B)."""
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant introuvable")
    tenant.company_size = company_size  # validé par l'Enum SQLAlchemy à l'écriture
    await db.commit()
    await db.refresh(tenant)
    return tenant
