from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DeliveryUpdate(BaseModel):
    provider: str | None = None
    tracking_number: str | None = None
    status: str | None = None
    address: str | None = None
    estimated_delivery: date | None = None
    notify_customer: bool = True  # lot 68 : prévenir le client (en route, puis livrée), une fois par étape


class DeliveryResponse(BaseModel):
    id: UUID
    order_id: UUID
    provider: str | None
    tracking_number: str | None
    status: str
    address: str | None
    estimated_delivery: date | None
    # Lot 68 — client prévenu à cet enregistrement : étape (SHIPPED / DELIVERED) et canal (WHATSAPP / EMAIL), ou None.
    notified_step: str | None = None
    notified_by: str | None = None

    model_config = ConfigDict(from_attributes=True)
