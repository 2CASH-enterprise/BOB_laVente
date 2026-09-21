from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.messaging_settings import KillSwitch, OutboundMode, TenantMessagingSettings
from app.models.tenant import Tenant

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


class TenantResponse(BaseModel):
    id: UUID
    name: str
    country: str
    currency: str
    company_size: str
    website_url: str | None
    active: bool

    model_config = ConfigDict(from_attributes=True)


class TenantProfileUpdate(BaseModel):
    name: str | None = None
    website_url: str | None = None


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


@router.put("/me/profile", response_model=TenantResponse, dependencies=[Depends(require_role("ADMIN"))])
async def update_tenant_profile(
    payload: TenantProfileUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant introuvable")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)
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


class MessagingSettingsResponse(BaseModel):
    outbound_mode: str
    kill_switch: str
    daily_outbound_limit: int
    templates_only: bool

    model_config = ConfigDict(from_attributes=True)


class MessagingSettingsUpdate(BaseModel):
    outbound_mode: OutboundMode | None = None
    daily_outbound_limit: int | None = None
    templates_only: bool | None = None
    # kill_switch volontairement absent ici : réservé à l'ADMIN plateforme (section 56.8), pas au tenant


@router.get("/me/messaging-settings", response_model=MessagingSettingsResponse)
async def get_messaging_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantMessagingSettings:
    stmt = select(TenantMessagingSettings).where(TenantMessagingSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        # Section 56.2 — comportement par défaut le plus restrictif tant que rien n'est configuré
        settings = TenantMessagingSettings(
            tenant_id=current_user.tenant_id,
            outbound_mode=OutboundMode.AI_ONLY,
            kill_switch=KillSwitch.ALLOWED,
        )
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.put(
    "/me/messaging-settings",
    response_model=MessagingSettingsResponse,
    dependencies=[Depends(require_role("ADMIN"))],
)
async def update_messaging_settings(
    payload: MessagingSettingsUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantMessagingSettings:
    stmt = select(TenantMessagingSettings).where(TenantMessagingSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = TenantMessagingSettings(tenant_id=current_user.tenant_id)
        db.add(settings)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)

    await db.commit()
    await db.refresh(settings)
    return settings


class FollowupSettingsResp(BaseModel):
    enabled: bool
    first_followup_hours: int
    first_message: str
    second_followup_hours: int
    second_message: str

    model_config = ConfigDict(from_attributes=True)


class FollowupSettingsReq(BaseModel):
    enabled: bool | None = None
    first_followup_hours: int | None = None
    first_message: str | None = None
    second_followup_hours: int | None = None
    second_message: str | None = None


@router.get("/me/followup-settings", response_model=FollowupSettingsResp)
async def get_followup_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.followup_settings import TenantFollowupSettings

    stmt = select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = TenantFollowupSettings(tenant_id=current_user.tenant_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.put(
    "/me/followup-settings", response_model=FollowupSettingsResp, dependencies=[Depends(require_role("ADMIN"))]
)
async def update_followup_settings(
    payload: FollowupSettingsReq,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.followup_settings import TenantFollowupSettings

    stmt = select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = TenantFollowupSettings(tenant_id=current_user.tenant_id)
        db.add(settings)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)

    await db.commit()
    await db.refresh(settings)
    return settings


class NegotiationSettingsResp(BaseModel):
    enabled: bool
    max_discount_pct: float
    max_rounds: int

    model_config = ConfigDict(from_attributes=True)


class NegotiationSettingsReq(BaseModel):
    enabled: bool | None = None
    max_discount_pct: float | None = None
    max_rounds: int | None = None


@router.get("/me/negotiation-settings", response_model=NegotiationSettingsResp)
async def get_negotiation_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.negotiation_settings import TenantNegotiationSettings

    stmt = select(TenantNegotiationSettings).where(TenantNegotiationSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = TenantNegotiationSettings(tenant_id=current_user.tenant_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.put(
    "/me/negotiation-settings", response_model=NegotiationSettingsResp, dependencies=[Depends(require_role("ADMIN"))]
)
async def update_negotiation_settings(
    payload: NegotiationSettingsReq,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.negotiation_settings import TenantNegotiationSettings

    stmt = select(TenantNegotiationSettings).where(TenantNegotiationSettings.tenant_id == current_user.tenant_id)
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = TenantNegotiationSettings(tenant_id=current_user.tenant_id)
        db.add(settings)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)

    await db.commit()
    await db.refresh(settings)
    return settings
