from datetime import datetime

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Lot 73 — Bob appelle l'adresse de la boutique : seulement une vraie boutique Shopify, jamais un service interne
# du serveur ni une autre adresse (falsification de requête côté serveur).
SHOPIFY_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.myshopify\.com$")
CATALOG_ID = re.compile(r"^\d{5,25}$")
SHOPIFY_ERROR = "Adresse Shopify invalide : indiquez l'adresse en .myshopify.com (ex. ma-boutique.myshopify.com)"
CATALOG_ERROR = "Identifiant de catalogue invalide : uniquement des chiffres (Gestionnaire de commerce → Paramètres du catalogue)"


def clean_shop_domain(value: str) -> str:
    """« https://Ma-Boutique.myshopify.com/admin » → « ma-boutique.myshopify.com » ; ValueError sinon."""
    text = (value or "").strip().lower()
    text = re.sub(r"^https?://", "", text).split("/", 1)[0].split("?", 1)[0]
    if not SHOPIFY_DOMAIN.match(text):
        raise ValueError(SHOPIFY_ERROR)
    return text


def clean_catalog_id(value: str) -> str:
    text = (value or "").strip()
    if not CATALOG_ID.match(text):
        raise ValueError(CATALOG_ERROR)
    return text


class ShopifyConnectRequest(BaseModel):
    shop_domain: str = Field(min_length=1, max_length=255, description="ex. ma-boutique.myshopify.com")
    access_token: str = Field(min_length=1)

    @field_validator("shop_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return clean_shop_domain(value)


class MetaCatalogConnectRequest(BaseModel):
    catalog_id: str = Field(min_length=1, max_length=64)
    access_token: str = Field(min_length=1)

    @field_validator("catalog_id")
    @classmethod
    def _catalog(cls, value: str) -> str:
        return clean_catalog_id(value)


class EcommerceConnectionResponse(BaseModel):
    platform: str
    shop_domain: str
    currency: str | None
    auto_sync_enabled: bool
    sync_interval_minutes: int
    last_synced_at: datetime | None
    last_sync_status: str

    model_config = ConfigDict(from_attributes=True)
