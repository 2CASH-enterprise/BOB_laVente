import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.whatsapp_account import WhatsAppAccount

router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp"])


class EmbeddedSignupCallback(BaseModel):
    """
    Section 59.2/59.6 — payload renvoyé par le SDK Embedded Signup une fois le commerce
    authentifié côté Meta. `code` est échangé côté serveur contre l'access token
    (appel réel à Meta prévu Sprint 3 ; ici la structure est posée et testable).
    """

    code: str
    waba_id: str
    phone_number_id: str
    business_id: str | None = None


class WhatsAppAccountResponse(BaseModel):
    tenant_id: uuid.UUID
    waba_id: str
    phone_number_id: str
    display_name_status: str

    model_config = ConfigDict(from_attributes=True)


@router.post(
    "/embedded-signup/callback",
    response_model=WhatsAppAccountResponse,
    dependencies=[Depends(require_role("ADMIN"))],
)
async def embedded_signup_callback(
    payload: EmbeddedSignupCallback,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    from sqlalchemy import select

    existing_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Un compte WhatsApp est déjà connecté pour ce tenant")

    # Échange réel code -> access_token via Meta Graph API : câblé au Sprint 3.
    placeholder_token = f"PENDING_EXCHANGE::{payload.code}"

    account = WhatsAppAccount(
        tenant_id=current_user.tenant_id,
        waba_id=payload.waba_id,
        phone_number_id=payload.phone_number_id,
        business_id=payload.business_id,
        system_user_token=placeholder_token,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/account", response_model=WhatsAppAccountResponse)
async def get_whatsapp_account(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    from sqlalchemy import select

    stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Aucun compte WhatsApp connecté pour ce tenant")
    return account
