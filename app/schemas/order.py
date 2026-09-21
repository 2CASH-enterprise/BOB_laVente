from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderItemCreate(BaseModel):
    product_id: UUID
    quantity: int = Field(gt=0)


class OrderCreateRequest(BaseModel):
    customer_id: UUID
    items: list[OrderItemCreate] = Field(min_length=1)
    delivery_address: str | None = None
    payment_method: str | None = None


class OrderItemResponse(BaseModel):
    product_id: UUID
    quantity: int
    unit_price: Decimal
    subtotal: Decimal

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    status: str
    total_amount: Decimal
    currency: str
    delivery_address: str | None
    payment_method: str | None
    created_by: str

    model_config = ConfigDict(from_attributes=True)


class OrderDetailResponse(OrderResponse):
    items: list[OrderItemResponse]
    delivery_status: str | None = None
    tracking_number: str | None = None
