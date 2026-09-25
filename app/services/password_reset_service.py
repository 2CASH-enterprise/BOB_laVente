"""
Mot de passe oublié.

Principes :
- le token est aléatoire (256 bits), envoyé UNE fois par email, stocké uniquement haché (SHA-256) ;
- validité courte (30 min), usage unique, une nouvelle demande invalide la précédente ;
- la réinitialisation ne contourne jamais la 2FA : elle change le mot de passe, point.
  La connexion suivante suit le chemin normal (code 2FA exigé si activée).
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User

RESET_TOKEN_TTL_MINUTES = 30


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset_token(user: User) -> str:
    """Génère un nouveau token (écrase tout token précédent) et renvoie la valeur en clair — à envoyer, jamais à stocker."""
    token = secrets.token_urlsafe(32)
    user.password_reset_token_hash = _hash_token(token)
    user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    return token


def build_reset_link(token: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/dashboard/?reset_token={token}"


def build_reset_email(full_name: str, link: str) -> tuple[str, str]:
    subject = "Réinitialisation de votre mot de passe Bob"
    body = (
        f"Bonjour {full_name},\n\n"
        "Vous avez demandé à réinitialiser le mot de passe de votre compte Bob.\n"
        "Cliquez sur le lien ci-dessous pour choisir un nouveau mot de passe :\n\n"
        f"{link}\n\n"
        f"Ce lien est valable {RESET_TOKEN_TTL_MINUTES} minutes et ne peut être utilisé qu'une seule fois.\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet email : "
        "votre mot de passe actuel reste inchangé."
    )
    return subject, body


def build_password_changed_email(full_name: str) -> tuple[str, str]:
    subject = "Votre mot de passe Bob a été modifié"
    body = (
        f"Bonjour {full_name},\n\n"
        "Le mot de passe de votre compte Bob vient d'être modifié.\n\n"
        "Si vous êtes à l'origine de ce changement, aucune action n'est nécessaire.\n"
        "Sinon, contactez-nous immédiatement : quelqu'un a peut-être accès à votre boîte email."
    )
    return subject, body


async def find_user_by_valid_token(db: AsyncSession, token: str) -> User | None:
    """Renvoie l'utilisateur actif associé à un token non expiré, sinon None (sans jamais dire pourquoi)."""
    if not token:
        return None
    stmt = select(User).where(
        User.password_reset_token_hash == _hash_token(token),
        User.active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.password_reset_expires_at is None:
        return None

    expires_at = user.password_reset_expires_at
    if expires_at.tzinfo is None:  # SQLite (tests) renvoie parfois une valeur naïve
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None
    return user


def apply_new_password(user: User, new_password: str) -> None:
    """Change le mot de passe et consomme le token (usage unique)."""
    user.hashed_password = hash_password(new_password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    # Un éventuel code 2FA en attente, émis avec l'ancien mot de passe, n'a plus lieu d'être.
    user.mfa_otp_code_hash = None
    user.mfa_otp_expires_at = None
