from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProductComplementCreate(BaseModel):
    product_id: UUID
    complement_product_id: UUID


class ProductComplementResponse(BaseModel):
    id: UUID
    product_id: UUID
    complement_product_id: UUID
    complement_name: str
    complement_price: float
    complement_currency: str

    model_config = ConfigDict(from_attributes=True)
