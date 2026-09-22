from uuid import UUID

from pydantic import BaseModel


class QrCodeCreate(BaseModel):
    product_id: UUID


class QrCodeResponse(BaseModel):
    id: UUID
    code: str
    product_id: UUID
    product_name: str
    product_image_url: str | None = None
    product_price: float | None = None
    product_currency: str | None = None
    scan_count: int
    short_path: str  # ex. "/qr/ab12cd34" — à préfixer par le domaine public côté client
