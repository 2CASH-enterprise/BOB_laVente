from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ShopifyConnectRequest(BaseModel):
    shop_domain: str = Field(min_length=1, description="ex. ma-boutique.myshopify.com")
    access_token: str = Field(min_length=1)


class MetaCatalogConnectRequest(BaseModel):
    catalog_id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)


class EcommerceConnectionResponse(BaseModel):
    platform: str
    shop_domain: str
    currency: str | None
    auto_sync_enabled: bool
    sync_interval_minutes: int
    last_synced_at: datetime | None
    last_sync_status: str

    model_config = ConfigDict(from_attributes=True)
