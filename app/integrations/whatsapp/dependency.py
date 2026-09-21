from app.core.config import get_settings
from app.integrations.whatsapp.client import MetaOAuthClient


def get_meta_oauth_client() -> MetaOAuthClient:
    """Retourne le client OAuth configuré avec les identifiants de l'app Bob. Surchargeable en test."""
    settings = get_settings()
    return MetaOAuthClient(app_id=settings.whatsapp_app_id, app_secret=settings.whatsapp_app_secret)
