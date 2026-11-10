"""
Lot 118 — lien de connexion à usage unique, valable 24 h, pour un commerçant venu de Naomy (pas de mot de passe au
départ). Il ouvre le tableau de bord sur l'étape « Relier WhatsApp ». Seule l'empreinte du jeton est gardée ; refusé
pour un compte qui a activé la double vérification (il se connecte normalement) ou désactivé.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

VALID = timedelta(hours=24)
PER_DAY = 5


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issued_today(db, tenant_id, now) -> int:
    from app.models.magic_login import MagicLogin

    return (await db.execute(select(func.count(MagicLogin.id)).where(
        MagicLogin.tenant_id == tenant_id, MagicLogin.created_at >= now - timedelta(hours=24)))).scalar_one()


async def create(db, user, now: datetime | None = None, purpose: str = "WHATSAPP") -> dict:
    from app.core.config import get_settings
    from app.models.magic_login import MagicLogin

    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    db.add(MagicLogin(tenant_id=user.tenant_id, user_id=user.id, token_hash=_digest(token), purpose=purpose,
                      expires_at=now + VALID, created_at=now))
    url = f"{get_settings().public_base_url.rstrip('/')}/dashboard/?connexion={token}"
    return {"url": url, "expire_le": (now + VALID).isoformat()}


async def consume(db, token: str, now: datetime | None = None):
    """L'utilisateur du lien (et le lien marqué utilisé), ou None."""
    from app.models.magic_login import MagicLogin
    from app.models.user import User

    if not token or len(token) > 100:
        return None
    now = now or datetime.now(timezone.utc)
    row = (await db.execute(select(MagicLogin).where(MagicLogin.token_hash == _digest(token)))).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.active or getattr(user, "mfa_enabled", False):
        return None
    row.used_at = now
    return user
