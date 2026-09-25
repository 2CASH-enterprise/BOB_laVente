"""
Désinscription par email (lien présent dans chaque campagne).

Le lien est SIGNÉ (HMAC-SHA256, clé dérivée de SECRET_KEY) plutôt que stocké en base :
- aucune colonne ni reprise de données ; le lien n'expire jamais (un client doit pouvoir se
  désinscrire d'un email reçu il y a six mois) ;
- impossible de fabriquer le lien d'un autre client sans la clé (l'identifiant seul ne suffit pas).
Contrepartie assumée : changer SECRET_KEY invalide les anciens liens.
"""
import base64
import hashlib
import hmac
import uuid

from app.core.config import get_settings

_SIGNATURE_LENGTH = 22  # 128 bits en base64url — largement suffisant contre la falsification


def _key() -> bytes:
    # Clé dédiée à cet usage : une signature de désinscription ne peut servir à rien d'autre.
    return hmac.new(get_settings().secret_key.encode(), b"bob:unsubscribe:v1", hashlib.sha256).digest()


def _sign(customer_id_hex: str) -> str:
    digest = hmac.new(_key(), customer_id_hex.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:_SIGNATURE_LENGTH]


def make_unsubscribe_token(customer_id: uuid.UUID) -> str:
    customer_hex = customer_id.hex
    return f"{customer_hex}.{_sign(customer_hex)}"


def read_unsubscribe_token(token: str) -> uuid.UUID | None:
    """Renvoie l'identifiant client si la signature est valide, sinon None (sans jamais dire pourquoi)."""
    if not token or token.count(".") != 1:
        return None
    customer_hex, signature = token.split(".")
    try:
        customer_id = uuid.UUID(hex=customer_hex)
    except ValueError:
        return None
    if customer_id.hex != customer_hex:  # forme canonique uniquement
        return None
    if not hmac.compare_digest(signature, _sign(customer_hex)):
        return None
    return customer_id


def build_unsubscribe_url(customer_id: uuid.UUID) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/unsubscribe/{make_unsubscribe_token(customer_id)}"
