from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CampaignSendRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    body_text: str = Field(min_length=1)
    whatsapp_cta_message: str = Field(min_length=1, max_length=500)


class CampaignSendResponse(BaseModel):
    campaign_id: UUID
    eligible_count: int
    sent_count: int


class EligibleCountResponse(BaseModel):
    eligible_count: int


class CampaignHistoryItem(BaseModel):
    id: UUID
    subject: str
    recipient_count: int
    created_at: datetime
