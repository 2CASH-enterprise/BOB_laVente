from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CustomerSummary(BaseModel):
    id: UUID
    display_name: str
    whatsapp_number: str
    acquisition_source: str | None
    commercial_status: str
    last_activity: datetime | None
    order_count: int
    total_spent: float
    tags: list[str]
    marketing_consent: bool


class CustomerDetail(BaseModel):
    id: UUID
    display_name: str
    first_name: str | None
    last_name: str | None
    whatsapp_number: str
    email: str | None
    city: str | None
    acquisition_source: str | None
    acquisition_detail: str | None
    commercial_status: str
    last_activity: datetime | None
    order_count: int
    total_spent: float
    products_viewed_count: int
    detected_preferences: dict
    tags: list[str]
    notes: str | None
    marketing_consent: bool
    marketing_consent_given_at: datetime | None
    marketing_consent_source: str | None
    marketing_consent_withdrawn_at: datetime | None
    marketing_consent_withdrawn_source: str | None = None
    latest_conversation_id: UUID | None


class CustomerUpdate(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None
    marketing_consent: bool | None = None
