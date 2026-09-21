from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.conversation import Conversation, ConversationStatus, Message
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.conversation import ConversationDetailResponse, ConversationResponse

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

    return ConversationDetailResponse(
        id=conversation.id,
        customer_id=conversation.customer_id,
        status=conversation.status.value,
        assigned_agent=conversation.assigned_agent,
        last_message_at=conversation.last_message_at,
        messages=messages,
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
    await db.commit()
    await db.refresh(conversation)
    return conversation
