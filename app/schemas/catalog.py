from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: UUID | None = None


class CategoryResponse(BaseModel):
    id: UUID
    name: str
    parent_id: UUID | None

    model_config = ConfigDict(from_attributes=True)


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    price: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    stock_quantity: int = Field(ge=0, default=0)
    category_id: UUID | None = None
    image_url: str | None = None
    active: bool = True


class ProductUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    category_id: UUID | None = None
    image_url: str | None = None
    active: bool | None = None


class ProductResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    description: str | None
    price: Decimal
    currency: str
    stock_quantity: int
    category_id: UUID | None
    image_url: str | None
    active: bool

    model_config = ConfigDict(from_attributes=True)


class CsvImportResponse(BaseModel):
    """Section 24 — reflète l'interface décrite : total / disponibles / indisponibles."""

    total_rows: int
    imported: int
    updated: int
    failed: int
    available: int
    unavailable: int
    errors: list[str] = []
