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
    public_base_url: str = "https://agenc-ai.com/bob"  # préfixe absolu — nécessaire pour les URLs d'images envoyées via WhatsApp

    # SMTP — envoi des codes de double authentification (section 2FA). Si non configuré,
    # le code est journalisé côté serveur (utile en développement), jamais bloquant.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "no-reply@agenc-ai.com"
    smtp_from_name: str = "Bob AI"  # nom affiché pour les emails transactionnels (2FA, mot de passe)
    smtp_use_tls: bool = True
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
    whatsapp_login_config_id: str = ""  # section 59.6 — config "Facebook Login for Business"

    # Commission B2B (section 53) — bornes strictes, jamais dépassées côté code
    commission_floor_pct: float = 0.0
    commission_ceiling_pct: float = 10.0

    # Négociation B2C par défaut (section 52) — surchargée par tenant en base
    default_max_discount_pct: float = 10.0
    default_max_negotiation_rounds: int = 3

    # Agent IA (section 13, 20) — moteur LLM
    llm_provider: str = "anthropic"  # "anthropic" ou "mistral"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    mistral_api_key: str = ""
    mistral_model: str = "mistral-large-latest"
    # Classificateur de messages (phase 1) : petit modèle rapide, appel séparé, jamais bloquant.
    mistral_classifier_model: str = "mistral-small-latest"
    classifier_timeout_seconds: float = 5.0
    max_tool_iterations: int = 5
    # Nouveaux essais du modèle principal sur erreur passagère (secondes d'attente avant chacun).
    llm_retry_delays: list[float] = [1.0, 3.0]


@lru_cache
def get_settings() -> Settings:
    return Settings()
