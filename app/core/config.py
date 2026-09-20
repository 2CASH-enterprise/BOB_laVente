"""
Configuration centrale de l'application.
Toutes les valeurs sensibles viennent de variables d'environnement (jamais en dur),
conformément à la section 32 du cahier des charges (secrets dans variables d'environnement).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "Bob - Agent vendeur IA"
    environment: str = "development"  # development | staging | production
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://bob:bob@localhost:5432/bob"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Security
    secret_key: str  # obligatoire, jamais de valeur par défaut en dur
    access_token_expire_minutes: int = 60
    algorithm: str = "HS256"

    # WhatsApp (section 59)
    whatsapp_app_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_webhook_verify_token: str = ""
    whatsapp_graph_api_version: str = "v25.0"

    # Commission B2B (section 53) — bornes strictes, jamais dépassées côté code
    commission_floor_pct: float = 0.0
    commission_ceiling_pct: float = 10.0

    # Négociation B2C par défaut (section 52) — surchargée par tenant en base
    default_max_discount_pct: float = 10.0
    default_max_negotiation_rounds: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
