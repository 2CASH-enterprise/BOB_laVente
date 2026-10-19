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
    # Lot 82 — page Facebook de Bob, proposée en fin de démo et dans les emails de prospection (vide : rien n'est affiché).
    facebook_page_url: str = "https://www.facebook.com/share/1KPHXStciM/"
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
    # Lot 62 — emails de PROSPECTION : un compte d'envoi séparé (Gmail + mot de passe d'application), pour
    # que les réactions des prospects ne touchent jamais les emails de Bob aux boutiques. Vide = pas d'envoi.
    prospect_smtp_host: str = "smtp.gmail.com"
    prospect_smtp_port: int = 587
    prospect_smtp_username: str = ""   # l'adresse Gmail de prospection
    prospect_smtp_password: str = ""   # mot de passe d'application (jamais le vrai mot de passe)
    prospect_imap_host: str = "imap.gmail.com"
    prospect_from_name: str = "Bob de AgenC'AI"
    prospect_copy_to: str = ""         # copie des réponses des prospects (adresse habituelle)
    # Lot 83 — boîte Gmail DÉDIÉE aux demandes des agences (portails, formulaires) : reçues sur
    # LEADS_SMTP_USERNAME avec « +code » de l'agence, réponses envoyées au nom de l'agence. Mot de passe d'application.
    leads_smtp_username: str = ""
    leads_smtp_password: str = ""
    debug: bool = False
    # Lot 73 : politique de sécurité des contenus (CSP). False = « Report-Only » (signale sans bloquer, une semaine),
    # True = appliquée (bloque). Les signalements arrivent dans le Monitoring.
    csp_enforce: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://bob:bob@localhost:5432/bob"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Security
    secret_key: str  # obligatoire, jamais de valeur par défaut en dur
    access_token_expire_minutes: int = 60
    # Lot 70 — code à 6 chiffres par email à chaque connexion Super Admin. SUPERADMIN_MFA=false (puis redémarrer)
    # seulement en dépannage, si les emails ne partent plus.
    superadmin_mfa: bool = True
    algorithm: str = "HS256"
    # Lot 30 — clé de chiffrement des jetons en base (Fernet). Facultative : sans elle, une clé est
    # dérivée de SECRET_KEY. Si elle est ajoutée plus tard, les jetons déjà chiffrés restent lisibles.
    token_encryption_key: str | None = None
    # Lot 37b — notifications sur l'appareil du commerçant (clés VAPID, générées une fois avec
    # « python -m app.scripts.generate_vapid_keys »). Vides : pas de notification, la pastille
    # reste à jour quand Bob est ouvert.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""  # « mailto:… » ; à défaut, l'adresse d'envoi des emails

    # WhatsApp (section 59)
    whatsapp_app_id: str = ""
    whatsapp_app_secret: str = ""
    # Lot 70 — sans WHATSAPP_APP_SECRET, les messages reçus de Meta sont REFUSÉS (signature impossible à vérifier).
    # Mettre WHATSAPP_REQUIRE_SIGNATURE=false seulement en développement local.
    whatsapp_require_signature: bool = True
    whatsapp_webhook_verify_token: str = ""
    whatsapp_graph_api_version: str = "v25.0"
    # Lot 21 — configuration Facebook Login for Business dédiée au catalogue (catalog_management).
    meta_catalog_login_config_id: str = ""
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
    # Lot 81 — ventes réelles DVF (data.gouv.fr, licence ouverte) : un fichier par année et par département,
    # {dvf_base_url}/{année}/departements/{département}.csv.gz
    dvf_base_url: str = "https://files.data.gouv.fr/geo-dvf/latest/csv"
    classifier_timeout_seconds: float = 5.0
    max_tool_iterations: int = 5
    # Nouveaux essais du modèle principal sur erreur passagère (secondes d'attente avant chacun).
    llm_retry_delays: list[float] = [1.0, 3.0]
    # Lot 50 — alerte au Super Admin quand le coût IA estimé d'une boutique dépasse ce montant ($/mois).
    llm_monthly_alert_usd: float = 20.0
    # Lot 67 — monitoring technique : file d'attente des erreurs (« redis » en production, « memory » pour les tests)
    # et seuil d'un pic (occurrences d'une même erreur en une heure) qui déclenche un email aux Super Admins.
    monitoring_queue: str = "redis"
    monitoring_spike_per_hour: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
