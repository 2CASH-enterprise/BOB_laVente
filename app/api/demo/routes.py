"""
Démo instantanée (« Instant AI Seller ») — le prospect importe son propre catalogue
et teste immédiatement son agent IA, sans créer de compte ni connecter WhatsApp.
Le tenant créé est marqué is_demo=True mais suit exactement le même chemin de code
que n'importe quel tenant réel (mêmes outils, mêmes garde-fous, section 33/50).
"""
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dependency import get_llm_client
from app.agents.llm_client import LLMClient
from app.agents.orchestrator import generate_ai_reply
from app.core.database import get_db
from app.core.security import CurrentUser, create_access_token, get_current_user, hash_password
from app.models.conversation import Message, MessageSender
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.user_repository import UserRepository
from app.schemas.demo import DemoChatRequest, DemoChatResponse, DemoCreateResponse, DemoPromoteRequest, DemoPromoteResponse
from app.services.audit import log_audit_event
from app.services.catalog_import import import_catalog_csv
from app.services.csv_column_mapper import map_csv_to_canonical_format

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

DEMO_CUSTOMER_HANDLE = "demo-web-session"
MAX_CSV_SIZE_BYTES = 10 * 1024 * 1024
# Lot 33 — créneaux par défaut d'une concession de démo (modifiables ensuite dans Rendez-vous).
DEMO_OPENING_HOURS = {str(day): [["09:00", "12:00"], ["14:00", "18:00"]] for day in range(6)}


@router.post("/create", response_model=DemoCreateResponse)
async def create_demo(
    company_name: str = Form(...),
    currency: str = Form("XOF"),
    country: str = Form("SN"),
    business_type: str = Form("ONLINE_STORE"),
    file: UploadFile = None,
    db: AsyncSession = Depends(get_db),
) -> DemoCreateResponse:
    """
    Aucune authentification requise : c'est le point d'entrée public de la démo,
    pensé pour la prospection (section « Instant AI Seller »). Un compte technique
    est créé en arrière-plan (identifiants aléatoires, jamais montrés au prospect).
    """
    from datetime import datetime, timezone

    from app.services.business_type import BUSINESS_TYPES, CAR_DEALERSHIP

    if not company_name.strip():
        raise HTTPException(status_code=400, detail="Le nom de l'entreprise est requis")
    if business_type not in BUSINESS_TYPES:
        raise HTTPException(status_code=400, detail="Secteur d'activité inconnu")
    country = (country or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise HTTPException(status_code=400, detail="Pays invalide")
    dealership = business_type == CAR_DEALERSHIP
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="Un fichier catalogue (CSV) est requis")

    raw = await file.read()
    if len(raw) > MAX_CSV_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 10 Mo)")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Encodage non supporté, utilisez UTF-8") from None

    try:
        mapped_csv = map_csv_to_canonical_format(content, default_currency=currency.upper()[:3] or "XOF",
                                                 keep_vehicle=dealership)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    tenant = Tenant(
        name=company_name.strip()[:255],
        country=country,
        currency=currency.upper()[:3] or "XOF",
        email=f"demo-{secrets.token_hex(8)}@bob-demo.internal",
        is_demo=True,
        # Lot 33 : le secteur choisi dans la démo est le type d'activité définitif du compte.
        business_type=business_type,
        business_type_chosen_at=datetime.now(timezone.utc),
    )
    db.add(tenant)
    await db.flush()

    owner = User(
        tenant_id=tenant.id,
        email=tenant.email,
        hashed_password=hash_password(secrets.token_urlsafe(24)),
        full_name="Compte démo",
        role=Role.OWNER,
    )
    db.add(owner)
    await db.flush()

    import_result = await import_catalog_csv(db, tenant.id, mapped_csv, with_vehicles=dealership)
    if dealership:
        from app.models.appointment_settings import TenantAppointmentSettings

        db.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True,
                                         opening_hours=DEMO_OPENING_HOURS, slot_minutes=60, capacity=1))
        await db.commit()

    demo_token = create_access_token(user_id=owner.id, tenant_id=tenant.id, role=owner.role.value)

    return DemoCreateResponse(
        tenant_id=str(tenant.id),
        demo_token=demo_token,
        imported=import_result.imported,
        updated=import_result.updated,
        failed=import_result.failed,
        available=import_result.available,
        unavailable=import_result.unavailable,
    )


@router.post("/chat", response_model=DemoChatResponse)
async def demo_chat(
    payload: DemoChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    llm_client: LLMClient | None = Depends(get_llm_client),
) -> DemoChatResponse:
    """
    Chat web direct (pas de WhatsApp) : le prospect discute avec SON agent, sur SON
    catalogue. Réutilise l'orchestrateur exact utilisé en production (mêmes outils,
    mêmes garde-fous) — la seule différence est le canal (web plutôt que WhatsApp).
    """
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message vide")

    if llm_client is None:
        return DemoChatResponse(reply="La démo n'est pas encore configurée (clé IA manquante). Réessayez plus tard.")

    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Session de démo introuvable")

    customer_repo = CustomerRepository(db)
    customer = await customer_repo.get_or_create(current_user.tenant_id, DEMO_CUSTOMER_HANDLE)

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create_active(current_user.tenant_id, customer.id)

    db.add(
        Message(
            tenant_id=current_user.tenant_id, conversation_id=conversation.id,
            sender=MessageSender.CUSTOMER, message_type="demo", content=payload.message,
        )
    )
    await db.commit()

    history_stmt = (
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )
    history = list(reversed((await db.execute(history_stmt)).scalars().all()))[:-1]  # exclut le message qu'on vient d'ajouter

    booking_outbox: list = []
    message_outbox: list[str] = []
    reply_text = await generate_ai_reply(
        db=db, tenant=tenant, conversation=conversation, history=history,
        incoming_text=payload.message, llm_client=llm_client, booking_outbox=booking_outbox,
        message_outbox=message_outbox,
    )
    from app.integrations.whatsapp.formatting import to_whatsapp
    from app.services import appointment_service
    from app.services.local_time import tenant_zone

    reply_text = to_whatsapp(reply_text)  # lot 31/33 : la démo montre exactement ce que WhatsApp afficherait
    # Lot 33 : la confirmation fixe du rendez-vous, comme sur WhatsApp.
    extra = [appointment_service.confirmation_message(a, tenant.name, tenant_zone(tenant)) for a in booking_outbox]
    extra += message_outbox  # lot 34b : demande d'email, comme sur WhatsApp

    db.add(
        Message(
            tenant_id=current_user.tenant_id, conversation_id=conversation.id,
            sender=MessageSender.AI, message_type="demo", content=reply_text,
        )
    )
    await db.commit()

    for text in extra:
        db.add(Message(tenant_id=current_user.tenant_id, conversation_id=conversation.id,
                       sender=MessageSender.SYSTEM, message_type="appointment_confirmed", content=text))
    await db.commit()

    return DemoChatResponse(reply=reply_text, extra_messages=extra)


@router.post("/promote", response_model=DemoPromoteResponse)
async def promote_demo(
    payload: DemoPromoteRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DemoPromoteResponse:
    """
    Transforme un tenant de démo en vrai compte freemium — SANS recréer le catalogue déjà
    importé. Le prospect passe de « je teste » à « c'est mon compte » sans rien retaper.
    """
    tenant = await db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Session de démo introuvable")
    if not tenant.is_demo:
        raise HTTPException(status_code=400, detail="Ce compte n'est plus un compte de démonstration")

    if not payload.email.strip() or "@" not in payload.email:
        raise HTTPException(status_code=400, detail="Email invalide")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 8 caractères")

    user_repo = UserRepository(db)
    existing = await user_repo.get_by_email(payload.email.strip().lower())
    if existing is not None and existing.tenant_id != tenant.id:
        raise HTTPException(status_code=409, detail="Cet email est déjà utilisé par un autre compte")

    # Lot 41 — comme l'inscription : le compte réel n'existe qu'avec une adresse vérifiée.
    from app.services import email_verification

    if not await email_verification.check_code(db, payload.email, payload.verification_code):
        await db.commit()  # l'essai raté est compté
        raise HTTPException(status_code=400, detail=email_verification.INVALID_CODE)

    owner = await db.get(User, current_user.user_id)
    owner.email = payload.email.strip().lower()
    owner.hashed_password = hash_password(payload.password)
    owner.full_name = payload.full_name.strip()

    # Lot 33 : les rendez-vous pris pendant la démo (client fictif) sont annulés, pour qu'aucun
    # rappel ne parte vers le nouveau compte.
    from app.models.appointment_request import AppointmentRequest
    from app.services import appointment_service

    demo_customer = (await db.execute(select(Customer).where(
        Customer.tenant_id == tenant.id, Customer.whatsapp_number == DEMO_CUSTOMER_HANDLE,
    ))).scalar_one_or_none()
    if demo_customer is not None:
        for appointment in (await db.execute(select(AppointmentRequest).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.customer_id == demo_customer.id,
        ))).scalars().all():
            appointment_service.cancel(appointment)

    tenant.is_demo = False
    tenant.email = owner.email

    await log_audit_event(
        db, actor=str(owner.id), action="DEMO_PROMOTED_TO_ACCOUNT", tenant_id=tenant.id, details={"email": owner.email}
    )
    await db.commit()

    new_token = create_access_token(user_id=owner.id, tenant_id=tenant.id, role=owner.role.value)
    return DemoPromoteResponse(access_token=new_token)
