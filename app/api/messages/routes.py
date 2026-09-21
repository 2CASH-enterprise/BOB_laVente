from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.models.conversation import Message, MessageSender
from app.repositories.base import TenantScopedRepository
from app.models.conversation import Conversation
from app.schemas.messaging import SendMessageRequest, SendMessageResponse
from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


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
    Note d'implémentation : la répartition des permissions CAN_REPLY_TO_CUSTOMER /
    CAN_SEND_PROACTIVE_MESSAGE par utilisateur (au-delà du rôle RBAC de la section 31)
    est prévue comme affinage ultérieur (table de permissions dédiée) ; pour ce sprint,
    CAN_SEND_PROACTIVE_MESSAGE est réservé aux rôles MANAGER et au-dessus.
    """
    permission = Permission.CAN_SEND_PROACTIVE_MESSAGE if payload.is_proactive else Permission.CAN_REPLY_TO_CUSTOMER

    if permission == Permission.CAN_SEND_PROACTIVE_MESSAGE and current_user.role not in ("MANAGER", "ADMIN", "OWNER"):
        raise HTTPException(status_code=403, detail="Rôle insuffisant pour un envoi proactif")

    try:
        await check_and_log_outbound(
            db,
            tenant_id=current_user.tenant_id,
            requested_by=str(current_user.user_id),
            permission=permission,
        )
    except OutboundDenied as exc:
        await db.commit()  # persiste le refus journalisé même en cas d'erreur
        raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc

    conv_repo = ConversationRepo(db)
    conversation = await conv_repo.get(tenant_id=current_user.tenant_id, record_id=payload.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    # Envoi réel via app.integrations.whatsapp.client.WhatsAppClient : câblage prévu au Sprint 3
    # (une fois le compte WhatsApp de chaque tenant systématiquement disponible en base).
    db.add(
        Message(
            tenant_id=current_user.tenant_id,
            conversation_id=conversation.id,
            sender=MessageSender.HUMAN,
            message_type="text",
            content=payload.body,
        )
    )
    await db.commit()

    return SendMessageResponse(status="sent")
