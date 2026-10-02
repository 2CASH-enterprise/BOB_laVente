"""
Lot 37 — « Rester connecté sur cet appareil » (15 jours, renouvelés à chaque utilisation).

- La clé de session est aléatoire (256 bits), rangée dans un cookie HttpOnly + SameSite=Strict
  (+ Secure en HTTPS) : le code de la page ne peut jamais la lire, un autre site ne peut pas
  l'utiliser. En base, seule son empreinte SHA-256.
- Chaque utilisation la remplace (rotation) ; l'ancienne reste acceptée 60 s pour deux onglets
  qui se rafraîchissent en même temps, sans prolonger quoi que ce soit.
- Sessions révoquées : déconnexion, « Déconnecter tous mes appareils », mot de passe réinitialisé,
  compte suspendu.
- Le code à 6 chiffres (2FA) n'est demandé qu'à la connexion : une session longue ne s'ouvre
  qu'après lui.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from app.models.user_session import UserSession

SESSION_DAYS = 15
ROTATION_GRACE = timedelta(seconds=60)
COOKIE_NAME = "bob_session"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def create_session(db, user, user_agent: str | None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    raw = secrets.token_urlsafe(32)
    db.add(UserSession(user_id=user.id, token_hash=_hash(raw), user_agent=(user_agent or "")[:255] or None,
                       expires_at=now + timedelta(days=SESSION_DAYS), last_used_at=now))
    return raw


async def use_session(db, raw: str | None, now: datetime | None = None) -> tuple[UserSession | None, str | None]:
    """
    Renvoie (session, nouvelle clé) si la clé est valide ; nouvelle clé None pendant la courte
    tolérance après une rotation (le navigateur a déjà la bonne). (None, None) sinon.
    """
    if not raw or len(raw) > 200:
        return None, None
    now = now or datetime.now(timezone.utc)
    digest = _hash(raw)
    session = (await db.execute(select(UserSession).where(
        or_(UserSession.token_hash == digest, UserSession.previous_token_hash == digest)
    ))).scalar_one_or_none()
    if session is None or session.revoked_at is not None or _aware(session.expires_at) <= now:
        return None, None
    if session.token_hash != digest:  # ancienne clé : seulement juste après la rotation
        if session.rotated_at is None or now - _aware(session.rotated_at) > ROTATION_GRACE:
            return None, None
        return session, None
    new_raw = secrets.token_urlsafe(32)
    session.previous_token_hash, session.token_hash = session.token_hash, _hash(new_raw)
    session.rotated_at = session.last_used_at = now
    session.expires_at = now + timedelta(days=SESSION_DAYS)  # glissant : 15 jours après la dernière utilisation
    return session, new_raw


async def revoke(db, raw: str | None, now: datetime | None = None) -> None:
    if not raw:
        return
    digest = _hash(raw)
    await db.execute(update(UserSession).where(
        or_(UserSession.token_hash == digest, UserSession.previous_token_hash == digest),
        UserSession.revoked_at.is_(None),
    ).values(revoked_at=now or datetime.now(timezone.utc)))


async def revoke_all(db, user_id, now: datetime | None = None) -> None:
    await db.execute(update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
    ).values(revoked_at=now or datetime.now(timezone.utc)))


def set_cookie(response, raw: str) -> None:
    from app.core.config import get_settings

    response.set_cookie(
        COOKIE_NAME, raw, max_age=SESSION_DAYS * 24 * 3600, httponly=True, samesite="strict", path="/",
        secure=get_settings().public_base_url.startswith("https://"),
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
