"""Relances marketing par email (point 3) — réservé aux plans payants."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.email_campaign import EmailCampaign
from app.models.tenant import Tenant
from app.schemas.campaign import (
    CampaignHistoryItem,
    CampaignSendRequest,
    CampaignSendResponse,
    EligibleCountResponse,
)
from app.services.campaign_service import get_eligible_customers, send_campaign

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])


async def _require_paid_tenant(db: AsyncSession, tenant_id) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_paid:
        raise HTTPException(status_code=403, detail="Les campagnes email nécessitent un plan payant")
    return tenant


@router.get("/eligible-count", response_model=EligibleCountResponse)
async def eligible_count(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aperçu avant envoi — jamais de surprise sur le nombre de destinataires réellement joignables."""
    await _require_paid_tenant(db, current_user.tenant_id)
    customers = await get_eligible_customers(db, current_user.tenant_id)
    return EligibleCountResponse(eligible_count=len(customers))


@router.post("/send", response_model=CampaignSendResponse, dependencies=[Depends(require_role("MANAGER"))])
async def send_campaign_endpoint(
    payload: CampaignSendRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_paid_tenant(db, current_user.tenant_id)

    result = await send_campaign(
        db, current_user.tenant_id, current_user.user_id,
        payload.subject, payload.body_text, payload.whatsapp_cta_message,
    )
    await db.commit()
    return CampaignSendResponse(**result)


@router.get("/history", response_model=list[CampaignHistoryItem])
async def campaign_history(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmailCampaign).where(EmailCampaign.tenant_id == current_user.tenant_id).order_by(EmailCampaign.created_at.desc())
    campaigns = (await db.execute(stmt)).scalars().all()
    return [CampaignHistoryItem(id=c.id, subject=c.subject, recipient_count=c.recipient_count, created_at=c.created_at) for c in campaigns]
