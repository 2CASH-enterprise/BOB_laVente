from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.integrations.whatsapp.client import parse_whatsapp_message
from app.models.conversation import Message, MessageSender
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.whatsapp_account_repository import WhatsAppAccountRepository

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])
settings = get_settings()


@router.get("")
async def verify_webhook(request: Request):
    """
    Section 7 — vérification du webhook exigée par Meta lors de sa configuration.
    Un seul verify_token pour toute la plateforme (l'app Meta du Tech Provider, section 59.3).
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_webhook_verify_token:
        return int(challenge) if challenge and challenge.isdigit() else challenge

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Vérification du webhook échouée")


@router.post("", status_code=status.HTTP_200_OK)
async def receive_webhook(payload: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Section 7 — doit répondre rapidement (< 1s, section 42) pour éviter les timeouts Meta.
    Le traitement IA réel (section 9, pipeline complet) est délégué à un traitement asynchrone
    ultérieur (section 3, Message Queue) — ici on se limite à la réception, l'identification
    du tenant/client, et la persistance, condition préalable à tout le reste.
    """
    parsed = parse_whatsapp_message(payload)
    if parsed is None:
        # Accusé de statut (delivered/read) ou payload non pertinent : on accuse réception sans traiter.
        return {"status": "ignored"}

    # Section 59.5 — routage par phone_number_id, AVANT tout traitement.
    wa_repo = WhatsAppAccountRepository(db)
    account = await wa_repo.find_tenant_by_phone_number_id(parsed["phone_number_id"])
    if account is None:
        # Un numéro inconnu de la plateforme ne doit jamais planter le webhook (Meta réessaierait en boucle).
        return {"status": "unknown_phone_number_id"}

    tenant_id = account.tenant_id

    customer_repo = CustomerRepository(db)
    customer = await customer_repo.get_or_create(tenant_id, parsed["from"])

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create_active(tenant_id, customer.id)

    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        sender=MessageSender.CUSTOMER,
        message_type=parsed["type"],
        content=parsed.get("text") or "",
        message_metadata={"wa_message_id": parsed["wa_message_id"]},
    )
    db.add(message)
    await db.commit()

    # À ce stade (section 9) : publication vers la file de tâches pour classification d'intention
    # + agent IA, non implémentée dans ce Sprint 2 (prévu Sprint 4 — agents/orchestrator.py).

    return {"status": "received"}
