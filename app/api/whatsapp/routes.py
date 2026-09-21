import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.integrations.whatsapp.client import MetaOAuthClient
from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.models.whatsapp_account import WhatsAppAccount
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp"])


class EmbeddedSignupConfigResponse(BaseModel):
    """
    Section 59.6 — valeurs publiques (non secrètes) nécessaires au bouton Embedded Signup
    côté dashboard : App ID et config_id Facebook Login for Business. Aucune authentification
    requise : ce sont les mêmes valeurs que Meta expose dans n'importe quel code d'intégration.
    """

    app_id: str
    login_config_id: str
    graph_api_version: str


@router.get("/embedded-signup/config", response_model=EmbeddedSignupConfigResponse)
async def embedded_signup_config() -> EmbeddedSignupConfigResponse:
    settings = get_settings()
    return EmbeddedSignupConfigResponse(
        app_id=settings.whatsapp_app_id,
        login_config_id=settings.whatsapp_login_config_id,
        graph_api_version=settings.whatsapp_graph_api_version,
    )


class EmbeddedSignupCallback(BaseModel):
    """
    Section 59.2/59.6 — payload renvoyé par le SDK Embedded Signup une fois le commerce
    authentifié côté Meta : waba_id et phone_number_id viennent du message postMessage
    WA_EMBEDDED_SIGNUP côté front, code vient de la réponse FB.login elle-même.
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
    oauth_client: MetaOAuthClient = Depends(get_meta_oauth_client),
) -> WhatsAppAccount:
    existing_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Un compte WhatsApp est déjà connecté pour ce tenant")

    # Échange réel code -> access_token, puis abonnement de l'app au WABA du client
    # (section 59.5 — condition pour recevoir ses webhooks, sinon aucun message n'arrivera
    # jamais, comme on l'a découvert manuellement lors du test du tenant démo).
    try:
        access_token = await oauth_client.exchange_code_for_token(payload.code)
        await oauth_client.subscribe_app_to_waba(payload.waba_id, access_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Connexion WhatsApp impossible : {exc}") from exc

    account = WhatsAppAccount(
        tenant_id=current_user.tenant_id,
        waba_id=payload.waba_id,
        phone_number_id=payload.phone_number_id,
        business_id=payload.business_id,
        system_user_token=access_token,
    )
    db.add(account)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="WHATSAPP_CONNECTED",
        tenant_id=current_user.tenant_id,
        details={"waba_id": payload.waba_id, "phone_number_id": payload.phone_number_id},
    )
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/account", response_model=WhatsAppAccountResponse)
async def get_whatsapp_account(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Aucun compte WhatsApp connecté pour ce tenant")
    return account
