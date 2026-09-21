from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DeliveryUpdate(BaseModel):
    provider: str | None = None
    tracking_number: str | None = None
    status: str | None = None
    address: str | None = None
    estimated_delivery: date | None = None


class DeliveryResponse(BaseModel):
    id: UUID
    order_id: UUID
    provider: str | None
    tracking_number: str | None
    status: str
    address: str | None
    estimated_delivery: date | None

    model_config = ConfigDict(from_attributes=True)
