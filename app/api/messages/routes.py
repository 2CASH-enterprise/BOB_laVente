import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.integrations.whatsapp.client import WhatsAppClient, WhatsAppSendError
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.whatsapp_account import WhatsAppAccount
from app.repositories.base import TenantScopedRepository
from app.schemas.messaging import SendMessageRequest, SendMessageResponse
from app.services.human_reply import HumanReplyError, ensure_reply_window_open
from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])

MAX_BODY_LENGTH = 4096  # limite de Meta pour un message texte


class ConversationRepo(TenantScopedRepository[Conversation]):
    model = Conversation


@router.post("/send", response_model=SendMessageResponse)
async def send_message(
    payload: SendMessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    """
    Section 56.5 — point d'entrée UNIQUE pour tout envoi sortant depuis le dashboard.
    Lot 23b : le message part réellement sur WhatsApp, et n'est enregistré que si Meta l'accepte.

    Ordre des contrôles : conversation de la boutique (404), état et fenêtre de 20 h (409),
    compte WhatsApp (409), puis le garde-fou d'envoi (403/429, journalisé), puis Meta (502).
    Les refus « de forme » passent avant le garde-fou pour ne pas compter d'envoi fantôme.
    """
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=422, detail="Le message est vide.")
    if len(body) > MAX_BODY_LENGTH:
        raise HTTPException(status_code=422, detail=f"Le message dépasse {MAX_BODY_LENGTH} caractères.")

    permission = Permission.CAN_SEND_PROACTIVE_MESSAGE if payload.is_proactive else Permission.CAN_REPLY_TO_CUSTOMER
    if permission == Permission.CAN_SEND_PROACTIVE_MESSAGE and current_user.role not in ("MANAGER", "ADMIN", "OWNER"):
        raise HTTPException(status_code=403, detail="Rôle insuffisant pour un envoi proactif")

    conversation = await ConversationRepo(db).get(tenant_id=current_user.tenant_id, record_id=payload.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    human_in_control = conversation.status == ConversationStatus.WAITING_HUMAN
    if permission == Permission.CAN_REPLY_TO_CUSTOMER and not human_in_control:
        # Jamais l'IA et un humain en même temps dans la même conversation.
        raise HTTPException(
            status_code=409,
            detail="Prenez d'abord le contrôle de la conversation : Bob y répond encore.",
        )

    try:
        await ensure_reply_window_open(db, conversation)
    except HumanReplyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    account = (await db.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    )).scalar_one_or_none()
    customer = await db.get(Customer, conversation.customer_id)
    if account is None or customer is None or customer.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=409, detail="Aucun compte WhatsApp n'est connecté à votre boutique.")

    try:
        await check_and_log_outbound(
            db,
            tenant_id=current_user.tenant_id,
            requested_by=str(current_user.user_id),
            permission=permission,
            human_in_control=human_in_control,
        )
    except OutboundDenied as exc:
        await db.commit()  # persiste le refus journalisé même en cas d'erreur
        raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc

    try:
        wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
        result = await wa_client.send_text_message(to=customer.whatsapp_number, body=body)
    except WhatsAppSendError as exc:
        await db.commit()  # le journal du garde-fou reste, le message n'est PAS enregistré
        raise HTTPException(status_code=502, detail=f"WhatsApp a refusé le message : {exc.details}") from exc
    except Exception as exc:  # noqa: BLE001 — réseau, délai : jamais de faux « envoyé »
        logger.warning("Envoi d'un message humain impossible (conversation %s) : %s", conversation.id, type(exc).__name__)
        await db.commit()
        raise HTTPException(status_code=502, detail="Envoi impossible pour le moment, réessayez dans un instant.") from exc

    wa_message_id = None
    try:
        wa_message_id = (result.get("messages") or [{}])[0].get("id")
    except (AttributeError, IndexError, TypeError):
        pass

    message = Message(
        tenant_id=current_user.tenant_id,
        conversation_id=conversation.id,
        sender=MessageSender.HUMAN,
        message_type="text",
        content=body,
        message_metadata={"sent_by": str(current_user.user_id), "wa_message_id": wa_message_id},
    )
    db.add(message)
    conversation.last_message_at = datetime.now(timezone.utc)
    await db.commit()

    return SendMessageResponse(status="sent", message_id=message.id)
