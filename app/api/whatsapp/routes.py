import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.integrations.whatsapp.client import MetaOAuthClient
from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.models.whatsapp_account import DisplayNameStatus, WhatsAppAccount
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
    phone_number_id: str | None = None  # lot 52 : parfois absent du parcours coexistence (retrouvé ensuite)
    business_id: str | None = None
    # Lot 52 — numéro déjà utilisé dans l'application WhatsApp Business du téléphone (parcours coexistence).
    coexistence: bool = False
    # Lot 73 c — « Reconnecter ou changer de numéro » : remplace le numéro déjà connecté (jamais sans ce choix explicite).
    replace: bool = False


class WhatsAppAccountResponse(BaseModel):
    tenant_id: uuid.UUID
    waba_id: str
    phone_number_id: str
    display_name_status: str
    display_phone_number: str | None = None
    # Lot 52 — numéro partagé avec le téléphone, et reprise de l'historique (REQUESTED, IN_PROGRESS, DONE, DECLINED).
    coexistence_mode: bool = False
    history_sync_status: str | None = None
    history_progress: int | None = None

    model_config = ConfigDict(from_attributes=True)


class DisplayPhoneNumberUpdate(BaseModel):
    display_phone_number: str


@router.post(
    "/embedded-signup/callback",
    response_model=WhatsAppAccountResponse,
    dependencies=[Depends(require_role("ADMIN"))],
)
async def embedded_signup_callback(
    payload: EmbeddedSignupCallback,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    oauth_client: MetaOAuthClient = Depends(get_meta_oauth_client),
) -> WhatsAppAccount:
    existing_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None and not payload.replace:
        raise HTTPException(status_code=409, detail="Un numéro WhatsApp est déjà connecté à votre compte")

    # Échange réel code -> access_token, puis abonnement de l'app au WABA du client
    # (section 59.5 — condition pour recevoir ses webhooks, sinon aucun message n'arrivera
    # jamais, comme on l'a découvert manuellement lors du test du tenant démo).
    phone_number_id, display_phone_number = payload.phone_number_id, None
    try:
        access_token = await oauth_client.exchange_code_for_token(payload.code)
        await oauth_client.subscribe_app_to_waba(payload.waba_id, access_token)
        if not phone_number_id:
            numbers = await oauth_client.list_phone_numbers(payload.waba_id, access_token) if payload.coexistence else []
            if not numbers:
                raise HTTPException(status_code=400, detail="Connexion WhatsApp impossible : aucun numéro reçu de Meta, réessayez")
            phone_number_id = str(numbers[0].get("id"))
            display_phone_number = "".join(ch for ch in str(numbers[0].get("display_phone_number") or "") if ch.isdigit()) or None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        from app.integrations.whatsapp.client import MetaGraphError

        # Jamais le texte brut d'une erreur réseau : l'adresse de l'échange contient le secret de l'app.
        reason = str(exc) if isinstance(exc, MetaGraphError) else "Meta est injoignable pour le moment"
        raise HTTPException(status_code=400, detail=f"Connexion WhatsApp impossible : {reason}") from exc

    taken = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id))).scalar_one_or_none()
    if taken is not None and (existing is None or taken.id != existing.id):
        raise HTTPException(status_code=409, detail="Ce numéro WhatsApp est déjà connecté à un autre compte Bob")

    details = {"waba_id": payload.waba_id, "phone_number_id": phone_number_id, "coexistence": payload.coexistence}
    if existing is not None:
        # Lot 73 c — même ligne mise à jour (même numéro : nouveau jeton ; autre numéro : tout est remplacé).
        same = existing.phone_number_id == phone_number_id
        details["previous_phone_number_id"] = existing.phone_number_id
        account = existing
        account.waba_id, account.phone_number_id, account.business_id = payload.waba_id, phone_number_id, payload.business_id
        account.system_user_token = access_token
        account.coexistence_mode = payload.coexistence
        if not same:
            account.display_phone_number = display_phone_number
            account.display_name, account.display_name_status = None, DisplayNameStatus.PENDING
            account.contacts_sync_requested_at = account.history_sync_requested_at = None
            account.history_sync_status = account.history_progress = None
        elif display_phone_number:
            account.display_phone_number = display_phone_number
        action = "WHATSAPP_RECONNECTED"
    else:
        account = WhatsAppAccount(
            tenant_id=current_user.tenant_id,
            waba_id=payload.waba_id,
            phone_number_id=phone_number_id,
            business_id=payload.business_id,
            system_user_token=access_token,
            display_phone_number=display_phone_number,
            coexistence_mode=payload.coexistence,
        )
        db.add(account)
        action = "WHATSAPP_CONNECTED"

    await log_audit_event(db, actor=str(current_user.user_id), action=action, tenant_id=current_user.tenant_id, details=details)
    await db.commit()
    await db.refresh(account)
    # Lot 68 — modèles de message créés chez Meta juste après la connexion (si le Super Admin les a allumés).
    from app.models.tenant import Tenant
    from app.services import wa_templates

    if await wa_templates.active_for(db, await db.get(Tenant, current_user.tenant_id)):
        background_tasks.add_task(wa_templates.sync_in_background, current_user.tenant_id)
    if account.coexistence_mode:
        # Lot 52 — contacts et historique du téléphone : demandés tout de suite (Meta laisse 24 h) ;
        # en cas d'échec, la tâche planifiée réessaie.
        from app.services.coexistence import request_syncs

        await request_syncs(db, account, oauth_client)
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
        raise HTTPException(status_code=404, detail="Aucun numéro WhatsApp n'est connecté à votre compte")
    return account


@router.put(
    "/account/display-phone-number", response_model=WhatsAppAccountResponse, dependencies=[Depends(require_role("ADMIN"))]
)
async def update_display_phone_number(
    payload: DisplayPhoneNumberUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppAccount:
    """Numéro affiché (format humain) — utilisé pour construire les liens wa.me des QR codes."""
    digits = "".join(ch for ch in payload.display_phone_number if ch.isdigit() or ch == "+")
    if len(digits) < 8:
        raise HTTPException(status_code=400, detail="Numéro invalide")

    stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    account = (await db.execute(stmt)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Aucun numéro WhatsApp n'est connecté à votre compte")

    account.display_phone_number = digits
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/templates")
async def whatsapp_templates(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Lot 68 — modèles de message de la boutique et leur statut chez Meta (page Intégrations)."""
    from app.models.tenant import Tenant
    from app.services import wa_templates

    return await wa_templates.overview(db, await db.get(Tenant, current_user.tenant_id))
