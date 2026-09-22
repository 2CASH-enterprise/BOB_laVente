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


@router.post("/create", response_model=DemoCreateResponse)
async def create_demo(
    company_name: str = Form(...),
    currency: str = Form("XOF"),
    file: UploadFile = None,
    db: AsyncSession = Depends(get_db),
) -> DemoCreateResponse:
    """
    Aucune authentification requise : c'est le point d'entrée public de la démo,
    pensé pour la prospection (section « Instant AI Seller »). Un compte technique
    est créé en arrière-plan (identifiants aléatoires, jamais montrés au prospect).
    """
    if not company_name.strip():
        raise HTTPException(status_code=400, detail="Le nom de l'entreprise est requis")
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
        mapped_csv = map_csv_to_canonical_format(content, default_currency=currency.upper()[:3] or "XOF")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    tenant = Tenant(
        name=company_name.strip(),
        country="SN",
        currency=currency.upper()[:3] or "XOF",
        email=f"demo-{secrets.token_hex(8)}@bob-demo.internal",
        is_demo=True,
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

    import_result = await import_catalog_csv(db, tenant.id, mapped_csv)

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

    reply_text = await generate_ai_reply(
        db=db, tenant=tenant, conversation=conversation, history=history,
        incoming_text=payload.message, llm_client=llm_client,
    )

    db.add(
        Message(
            tenant_id=current_user.tenant_id, conversation_id=conversation.id,
            sender=MessageSender.AI, message_type="demo", content=reply_text,
        )
    )
    await db.commit()

    return DemoChatResponse(reply=reply_text)


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
        raise HTTPException(status_code=400, detail="Ce tenant est déjà un compte réel")

    if not payload.email.strip() or "@" not in payload.email:
        raise HTTPException(status_code=400, detail="Email invalide")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 8 caractères")

    user_repo = UserRepository(db)
    existing = await user_repo.get_by_email(payload.email.strip().lower())
    if existing is not None and existing.tenant_id != tenant.id:
        raise HTTPException(status_code=409, detail="Cet email est déjà utilisé par un autre compte")

    owner = await db.get(User, current_user.user_id)
    owner.email = payload.email.strip().lower()
    owner.hashed_password = hash_password(payload.password)

    tenant.is_demo = False
    tenant.email = owner.email

    await log_audit_event(
        db, actor=str(owner.id), action="DEMO_PROMOTED_TO_ACCOUNT", tenant_id=tenant.id, details={"email": owner.email}
    )
    await db.commit()

    new_token = create_access_token(user_id=owner.id, tenant_id=tenant.id, role=owner.role.value)
    return DemoPromoteResponse(access_token=new_token)
