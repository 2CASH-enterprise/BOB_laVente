from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ConversationResponse(BaseModel):
    id: UUID
    customer_id: UUID
    status: str
    assigned_agent: UUID | None
    last_message_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    id: UUID
    sender: str
    message_type: str
    content: str
    created_at: datetime
    # Étiquettes déduites (messages du client uniquement) : {intents, objections, offered_amount}
    signals: dict | None = None

    model_config = ConfigDict(from_attributes=True)


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]
