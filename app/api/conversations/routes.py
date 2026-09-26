from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.conversation import ConversationDetailResponse, ConversationResponse
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ConversationRepository(db)
    return await repo.list(tenant_id=current_user.tenant_id, limit=100)


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ConversationRepository(db)
    conversation = await repo.get(tenant_id=current_user.tenant_id, record_id=conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    messages_stmt = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
    messages = (await db.execute(messages_stmt)).scalars().all()

    from app.schemas.conversation import MessageResponse
    from app.services.signal_service import signals_by_message

    signals = await signals_by_message(db, current_user.tenant_id, [m.id for m in messages])
    message_payload = [
        MessageResponse.model_validate(m).model_copy(update={"signals": signals.get(m.id)}) for m in messages
    ]

    return ConversationDetailResponse(
        id=conversation.id,
        customer_id=conversation.customer_id,
        status=conversation.status.value,
        assigned_agent=conversation.assigned_agent,
        last_message_at=conversation.last_message_at,
        messages=message_payload,
    )


@router.post(
    "/{conversation_id}/takeover", response_model=ConversationResponse, dependencies=[Depends(require_role("AGENT"))]
)
async def takeover_conversation(
    conversation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Section 28 — « PRENDRE LE CONTRÔLE ». L'IA arrête automatiquement de répondre : le
    webhook (section 9) ne génère de réponse IA que si conversation.status == ACTIVE,
    donc ce simple changement de statut suffit à la faire taire, sans code supplémentaire.
    """
    repo = ConversationRepository(db)
    conversation = await repo.get(tenant_id=current_user.tenant_id, record_id=conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    conversation.status = ConversationStatus.WAITING_HUMAN
    conversation.assigned_agent = current_user.user_id
    db.add(
        Message(
            tenant_id=current_user.tenant_id,
            conversation_id=conversation.id,
            sender=MessageSender.SYSTEM,
            message_type="takeover",
            content="Un conseiller a pris le contrôle de la conversation.",
        )
    )
    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="CONVERSATION_TAKEOVER",
        tenant_id=current_user.tenant_id,
        details={"conversation_id": str(conversation.id)},
    )
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.post(
    "/{conversation_id}/release", response_model=ConversationResponse, dependencies=[Depends(require_role("AGENT"))]
)
async def release_conversation(
    conversation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Section 28 — « RENDRE À L'IA »."""
    repo = ConversationRepository(db)
    conversation = await repo.get(tenant_id=current_user.tenant_id, record_id=conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    # Seul l'agent qui a pris la main (ou un rôle supérieur) peut la relâcher.
    if conversation.assigned_agent != current_user.user_id and current_user.role not in ("MANAGER", "ADMIN", "OWNER"):
        raise HTTPException(status_code=403, detail="Seul l'agent en charge peut rendre la conversation à l'IA")

    conversation.status = ConversationStatus.ACTIVE
    conversation.assigned_agent = None
    db.add(
        Message(
            tenant_id=current_user.tenant_id,
            conversation_id=conversation.id,
            sender=MessageSender.SYSTEM,
            message_type="release",
            content="La conversation a été rendue à l'IA.",
        )
    )
    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="CONVERSATION_RELEASE",
        tenant_id=current_user.tenant_id,
        details={"conversation_id": str(conversation.id)},
    )
    await db.commit()
    await db.refresh(conversation)
    return conversation
