from datetime import datetime, timezone

from sqlalchemy import select

from app.models.conversation import Conversation, ConversationStatus
from app.repositories.base import TenantScopedRepository


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation

    async def get_or_create_active(self, tenant_id, customer_id) -> Conversation:
        """Réutilise une conversation active existante plutôt que d'en ouvrir une nouvelle à chaque message."""
        stmt = select(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.customer_id == customer_id,
            Conversation.status != ConversationStatus.CLOSED,
        )
        result = await self.session.execute(stmt)
        conversation = result.scalar_one_or_none()
        if conversation is not None:
            return conversation

        conversation = Conversation(
            tenant_id=tenant_id,
            customer_id=customer_id,
            status=ConversationStatus.ACTIVE,
            last_message_at=datetime.now(timezone.utc),
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation
