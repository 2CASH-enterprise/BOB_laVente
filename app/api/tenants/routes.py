from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.business_type import only_online_store
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
    plan: str
    website_url: str | None
    company_profile: str | None
    payment_link: str | None
    active: bool

    model_config = ConfigDict(from_attributes=True)


class TenantProfileUpdate(BaseModel):
    name: str | None = None
    website_url: str | None = None
    company_profile: str | None = None
    payment_link: str | None = None


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte introuvable")
    return tenant


@router.put("/me/profile", response_model=TenantResponse, dependencies=[Depends(require_role("ADMIN"))])
async def update_tenant_profile(
    payload: TenantProfileUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte introuvable")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)
    return tenant


# Le changement de plan n'est volontairement PAS self-service : il passera par le
# paiement réel ou le dashboard Super Admin (à construire), jamais par le tenant
# lui-même — évite qu'un compte se passe payant sans jamais avoir payé.


class BusinessTypeOption(BaseModel):
    code: str
    label: str
    description: str


class BusinessTypeResponse(BaseModel):
    business_type: str
    chosen: bool
    options: list[BusinessTypeOption]


class BusinessTypeUpdate(BaseModel):
    business_type: str


def _business_type_response(tenant: Tenant) -> BusinessTypeResponse:
    from app.services.business_type import BUSINESS_TYPES, normalize

    return BusinessTypeResponse(
        business_type=normalize(tenant.business_type),
        chosen=tenant.business_type_chosen_at is not None,
        options=[BusinessTypeOption(code=code, **info) for code, info in BUSINESS_TYPES.items()],
    )


@router.get("/me/business-type", response_model=BusinessTypeResponse)
async def get_business_type(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BusinessTypeResponse:
    """Lot 24 — type d'activité de la boutique, et s'il a déjà été choisi (écran d'après inscription)."""
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte introuvable")
    return _business_type_response(tenant)


@router.put("/me/business-type", response_model=BusinessTypeResponse, dependencies=[Depends(require_role("ADMIN"))])
async def update_business_type(
    payload: BusinessTypeUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BusinessTypeResponse:
    from datetime import datetime, timezone

    from app.services.audit import log_audit_event
    from app.services.business_type import BUSINESS_TYPES

    if payload.business_type not in BUSINESS_TYPES:
        raise HTTPException(status_code=422, detail="Type d'activité inconnu")
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte introuvable")
    if tenant.business_type_chosen_at is not None:
        # Lot 32 — choisi une seule fois, à l'inscription. Un changement se demande au support Bob,
        # qui le fait depuis l'Admin (les fonctions et les données ne sont pas les mêmes).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le type d'activité a déjà été choisi. Pour le changer, contactez le support Bob.",
        )
    previous = tenant.business_type
    tenant.business_type = payload.business_type
    tenant.business_type_chosen_at = datetime.now(timezone.utc)
    await log_audit_event(
        db, actor=str(current_user.user_id), action="BUSINESS_TYPE_CHANGED", tenant_id=tenant.id,
        details={"from": previous, "to": payload.business_type},
    )
    await db.commit()
    await db.refresh(tenant)
    return _business_type_response(tenant)


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


@router.get("/me/negotiation-settings", response_model=NegotiationSettingsResp, dependencies=[Depends(only_online_store())])
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
    "/me/negotiation-settings", response_model=NegotiationSettingsResp,
    dependencies=[Depends(require_role("ADMIN")), Depends(only_online_store())],
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


# ---------------------------------------------------------------------------
# Règles de transmission à un humain (lot 13)
# ---------------------------------------------------------------------------

class HandoffSettingsResp(BaseModel):
    refund_transfer: bool
    complaint_policy: str
    discount_policy: str
    ai_outage_policy: str = "RETRY_LATER"
    human_request_transfer: bool = True  # non réglable : affiché pour information


class HandoffSettingsReq(BaseModel):
    refund_transfer: bool | None = None
    complaint_policy: Literal["TRY_FIRST", "TRANSFER"] | None = None
    discount_policy: Literal["FIXED_PRICES", "TRANSFER"] | None = None
    ai_outage_policy: Literal["RETRY_LATER", "CALLBACK"] | None = None


def _handoff_resp(row) -> HandoffSettingsResp:
    from app.services.handoff_rules import HandoffSettingsView

    view = row or HandoffSettingsView()
    return HandoffSettingsResp(
        refund_transfer=view.refund_transfer, complaint_policy=view.complaint_policy,
        discount_policy=view.discount_policy, ai_outage_policy=getattr(view, "ai_outage_policy", None) or "RETRY_LATER",
    )


@router.get("/me/handoff-settings", response_model=HandoffSettingsResp)
async def get_handoff_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sans réglage enregistré : valeurs par défaut (aucune ligne créée à la lecture)."""
    from app.models.handoff_settings import TenantHandoffSettings

    row = (await db.execute(
        select(TenantHandoffSettings).where(TenantHandoffSettings.tenant_id == current_user.tenant_id)
    )).scalar_one_or_none()
    return _handoff_resp(row)


@router.put("/me/handoff-settings", response_model=HandoffSettingsResp, dependencies=[Depends(require_role("ADMIN"))])
async def update_handoff_settings(
    payload: HandoffSettingsReq,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.handoff_settings import TenantHandoffSettings

    row = (await db.execute(
        select(TenantHandoffSettings).where(TenantHandoffSettings.tenant_id == current_user.tenant_id)
    )).scalar_one_or_none()
    if row is None:
        row = TenantHandoffSettings(
            tenant_id=current_user.tenant_id, refund_transfer=True, complaint_policy="TRY_FIRST",
            discount_policy="FIXED_PRICES", ai_outage_policy="RETRY_LATER",
        )
        db.add(row)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, field, value)
    from app.services.audit import log_audit_event

    await log_audit_event(
        db, actor=str(current_user.user_id), action="HANDOFF_SETTINGS_UPDATED", tenant_id=current_user.tenant_id,
        details=payload.model_dump(exclude_unset=True),
    )
    await db.commit()
    await db.refresh(row)
    return _handoff_resp(row)


# ---------------------------------------------------------------------------
# Stratégies de réponse aux objections (lot 15)
# ---------------------------------------------------------------------------

class StrategySettingsReq(BaseModel):
    disabled: list[str]


@router.get("/me/strategy-settings")
async def get_strategy_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bibliothèque groupée par objection, avec l'état activé/désactivé pour ce commerce."""
    from app.services.strategy_service import disabled_strategies, knowledge_categories, library_with_state

    known = await knowledge_categories(db, current_user.tenant_id)
    tenant = await db.get(Tenant, current_user.tenant_id)  # lot 45 : les stratégies de son activité
    return {"objections": library_with_state(await disabled_strategies(db, current_user.tenant_id), known, tenant.business_type)}


@router.put("/me/strategy-settings", dependencies=[Depends(require_role("ADMIN"))])
async def update_strategy_settings(
    payload: StrategySettingsReq,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.strategy_settings import TenantStrategySettings
    from app.services.audit import log_audit_event
    from app.services.strategy_service import knowledge_categories, library_with_state, validate_disabled

    disabled = sorted(set(payload.disabled))
    business_type = (await db.get(Tenant, current_user.tenant_id)).business_type
    error = validate_disabled(disabled, business_type)
    if error:
        raise HTTPException(status_code=422, detail=error)

    row = (await db.execute(
        select(TenantStrategySettings).where(TenantStrategySettings.tenant_id == current_user.tenant_id)
    )).scalar_one_or_none()
    if row is None:
        row = TenantStrategySettings(tenant_id=current_user.tenant_id, disabled_strategies=disabled)
        db.add(row)
    else:
        row.disabled_strategies = disabled
    await log_audit_event(
        db, actor=str(current_user.user_id), action="STRATEGY_SETTINGS_UPDATED", tenant_id=current_user.tenant_id,
        details={"disabled": disabled},
    )
    await db.commit()
    return {"objections": library_with_state(set(disabled), await knowledge_categories(db, current_user.tenant_id), business_type)}


# ---------------------------------------------------------------------------
# Lot 38 — tutoiement / vouvoiement des clients (boutique en ligne)
# ---------------------------------------------------------------------------

class AddressFormReq(BaseModel):
    address_form: Literal["VOUS", "TU"]


def _address_form_response(form: str) -> dict:
    from app.services.address_form import ADDRESS_FORMS, VOUS, preview

    return {
        "address_form": form,
        "options": [{"code": code, "label": label, "preview": preview(code)} for code, label in ADDRESS_FORMS.items()],
        "default": VOUS,
    }


@router.get("/me/address-form", dependencies=[Depends(only_online_store())])
async def get_address_form(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    return _address_form_response(tenant.address_form)


@router.put("/me/address-form", dependencies=[Depends(require_role("ADMIN")), Depends(only_online_store())])
async def update_address_form(
    payload: AddressFormReq,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Concession : refusé (403) — elle vouvoie toujours ses clients."""
    from app.services.audit import log_audit_event

    tenant = await db.get(Tenant, current_user.tenant_id)
    previous = tenant.address_form
    tenant.address_form = payload.address_form
    await log_audit_event(db, actor=str(current_user.user_id), action="ADDRESS_FORM_UPDATED", tenant_id=tenant.id,
                          details={"from": previous, "to": payload.address_form})
    await db.commit()
    return _address_form_response(tenant.address_form)


# ---------------------------------------------------------------------------
# Lot 41 — « Premiers pas » : ce qu'il reste à faire pour que Bob travaille
# ---------------------------------------------------------------------------

class OnboardingReq(BaseModel):
    hidden: bool


async def _onboarding(db: AsyncSession, tenant: Tenant) -> dict:
    """Chaque étape est cochée d'après les vraies données (jamais une case à cocher à la main)."""
    from sqlalchemy import func

    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.business_type import is_dealership

    dealership = is_dealership(tenant)
    has_whatsapp = (await db.execute(select(func.count(WhatsAppAccount.id)).where(
        WhatsAppAccount.tenant_id == tenant.id))).scalar_one() > 0
    has_products = (await db.execute(select(func.count(Product.id)).where(
        Product.tenant_id == tenant.id, Product.active.is_(True)))).scalar_one() > 0
    has_conversation = (await db.execute(
        select(func.count(Conversation.id)).join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.tenant_id == tenant.id, Customer.whatsapp_number != "demo-web-session")
    )).scalar_one() > 0
    if dealership:
        from app.services.booking import booking_enabled, load_settings

        fourth = {"key": "booking", "label": "Ouvrir vos créneaux de rendez-vous", "tab": "appointments",
                  "hint": "Vos horaires d'ouverture : Bob propose vos créneaux libres et confirme le rendez-vous.",
                  "done": booking_enabled(await load_settings(db, tenant.id))}
    else:
        fourth = {"key": "payment", "label": "Ajouter votre lien de paiement", "tab": "knowledge",
                  "hint": "Wave, Orange Money… Bob le donne au client au moment de payer.",
                  "done": bool(tenant.payment_link)}
    steps = [
        {"key": "whatsapp", "label": "Connecter votre WhatsApp", "tab": "integrations",
         "hint": "Bob répond aux messages reçus sur votre numéro WhatsApp Business.", "done": has_whatsapp},
        {"key": "products", "label": "Ajouter vos véhicules" if dealership else "Ajouter vos produits", "tab": "products",
         "hint": "Import d'un fichier, catalogue Facebook ou ajout un par un : Bob ne propose que ce qui existe.",
         "done": has_products},
        {"key": "profile", "label": "Présenter votre entreprise", "tab": "knowledge",
         "hint": "Quelques lignes sur votre activité, vos horaires, la livraison… Bob s'en sert pour répondre.",
         "done": bool((tenant.company_profile or "").strip())},
        fourth,
        {"key": "test", "label": "Tester Bob", "tab": "integrations",
         "hint": "Écrivez à votre numéro WhatsApp depuis un autre téléphone, comme un client.", "done": has_conversation},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "hidden": tenant.onboarding_hidden_at is not None,
            "show": tenant.onboarding_hidden_at is None and done < len(steps)}


@router.get("/me/onboarding")
async def get_onboarding(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte introuvable")
    return await _onboarding(db, tenant)


@router.put("/me/onboarding")
async def update_onboarding(payload: OnboardingReq, current_user: CurrentUser = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict:
    """Masquer (ou réafficher) la carte « Premiers pas »."""
    from datetime import datetime, timezone

    tenant = await db.get(Tenant, current_user.tenant_id)
    tenant.onboarding_hidden_at = datetime.now(timezone.utc) if payload.hidden else None
    await db.commit()
    return await _onboarding(db, tenant)
