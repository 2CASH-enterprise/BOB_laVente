"""
Lot 30 — chiffrement au repos des jetons d'accès (WhatsApp, catalogue Meta, Shopify).

Les jetons sont chiffrés (Fernet : AES-128-CBC + HMAC-SHA256) AVANT d'être écrits en base et
déchiffrés à la lecture, de façon transparente pour le reste du code (type SQLAlchemy
`EncryptedString`). Une copie de la base (sauvegarde, fuite) ne donne donc aucun jeton utilisable
sans la clé, qui reste dans l'environnement du serveur.

Clé : TOKEN_ENCRYPTION_KEY si elle est définie, sinon dérivée de SECRET_KEY (HKDF-SHA256). Les
deux restent acceptées en lecture : ajouter TOKEN_ENCRYPTION_KEY plus tard ne casse rien.
Une valeur sans préfixe `enc:v1:` est un ancien jeton en clair (avant la migration) : lue telle
quelle. Un jeton est ainsi toujours lisible, et n'est jamais journalisé.
"""
import base64
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


def _derived_key(secret_key: str) -> bytes:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"bob-at-rest", info=b"bob-token-encryption-v1").derive(
        secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


@lru_cache(maxsize=8)
def _cipher(secret_key: str, explicit_key: str | None) -> MultiFernet:
    keys = []
    if explicit_key:
        keys.append(Fernet(explicit_key.encode("utf-8")))  # clé invalide : erreur au démarrage, jamais silencieuse
    keys.append(Fernet(_derived_key(secret_key)))
    return MultiFernet(keys)  # chiffre avec la première, déchiffre avec toutes


def cipher() -> MultiFernet:
    from app.core.config import get_settings

    settings = get_settings()
    return _cipher(settings.secret_key, settings.token_encryption_key or None)


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: str | None) -> str | None:
    if value is None or is_encrypted(value):
        return value
    return PREFIX + cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if value is None or not is_encrypted(value):
        return value  # ancien jeton en clair (avant la migration du lot 30)
    try:
        return cipher().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Mauvaise clé (SECRET_KEY changée ?) : le jeton est inutilisable. Jamais sa valeur dans le
        # journal ; les appels à Meta échoueront et la boutique devra reconnecter son compte.
        logger.error("Jeton chiffré illisible : la clé de chiffrement a-t-elle changé ?")
        return ""


class EncryptedString(TypeDecorator):
    """Colonne texte chiffrée au repos, lue et écrite en clair par l'application."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
