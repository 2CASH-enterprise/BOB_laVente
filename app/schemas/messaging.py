from uuid import UUID

from pydantic import BaseModel


class SendMessageRequest(BaseModel):
    conversation_id: UUID
    body: str
    is_proactive: bool = False  # False = réponse à une conversation engagée ; True = campagne/relance manuelle


class SendMessageResponse(BaseModel):
    status: str
