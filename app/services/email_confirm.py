"""
Lot 134 (choix B de Thérèse, 27/11) — comptes créés par Naomy : le numéro WhatsApp est l'identité, l'e-mail est
facultatif. Une adresse donnée attend sa confirmation : un seul e-mail part (« Confirmer mon adresse ») ; d'ici là,
l'adresse du compte est « wa<numéro>@attente.invalid », à laquelle rien n'est jamais envoyé, et l'adresse en attente
ne bloque personne (son vrai propriétaire peut créer son compte avec).
"""
import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

DOMAIN = "attente.invalid"  # domaine réservé (RFC 2606) : jamais d'envoi
VALID = timedelta(days=7)


def placeholder(digits: str) -> str:
    return f"wa{digits}@{DOMAIN}"


def is_placeholder(address: str | None) -> bool:
    return bool(address) and address.strip().lower().endswith(".invalid")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def confirm_url(token: str) -> str:
    from app.core.config import get_settings

    return f"{get_settings().public_base_url.rstrip('/')}/email/confirmer?jeton={token}"


async def start(db, tenant, email: str, now: datetime | None = None, mailer=None) -> None:
    """Garde l'adresse en attente et envoie le seul e-mail permis : le lien de confirmation (7 jours)."""
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    tenant.email_pending, tenant.email_confirm_hash, tenant.email_confirm_sent_at = email, _digest(token), now
    await db.commit()
    if mailer is None:
        from app.services.email_service import send_email as mailer
    body = (f"Bonjour,\n\nVotre boutique {tenant.name} vient d'être créée avec Naomy et Bob, sur WhatsApp.\n\n"
            f"Pour recevoir vos rapports et pouvoir vous connecter avec cette adresse, confirmez-la :\n{confirm_url(token)}\n\n"
            "Le lien est valable 7 jours. Si vous n'êtes pas à l'origine de cette inscription, ignorez cet e-mail : "
            "nous ne vous écrirons plus.\n\n— AgenC'AI")
    try:
        await asyncio.to_thread(mailer, to=email, subject="Confirmez votre adresse pour Bob", body=body)
    except Exception:  # noqa: BLE001 — l'inscription ne dépend pas de l'e-mail
        logger.warning("E-mail de confirmation non envoyé (boutique %s)", tenant.id)


async def address_taken(db, email: str, tenant_id) -> bool:
    from app.models.tenant import Tenant
    from app.models.user import User

    user = (await db.execute(select(User.id).where(func.lower(User.email) == email, User.tenant_id != tenant_id)
                             .limit(1))).scalar_one_or_none()
    shop = (await db.execute(select(Tenant.id).where(func.lower(Tenant.email) == email, Tenant.id != tenant_id)
                             .limit(1))).scalar_one_or_none()
    return user is not None or shop is not None


async def confirm(db, token: str, now: datetime | None = None) -> str:
    """« ok », « inconnu » (lien faux, déjà servi ou expiré) ou « prise » (adresse d'un autre compte Bob)."""
    from app.models.tenant import Tenant
    from app.models.user import Role, User
    from app.services.audit import log_audit_event

    now = now or datetime.now(timezone.utc)
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return "inconnu"
    tenant = (await db.execute(select(Tenant).where(Tenant.email_confirm_hash == _digest(token)))).scalar_one_or_none()
    if tenant is None or not tenant.email_pending or tenant.email_confirm_sent_at is None \
            or (tenant.email_confirm_sent_at.replace(tzinfo=tenant.email_confirm_sent_at.tzinfo or timezone.utc) + VALID) < now:
        return "inconnu"
    email = tenant.email_pending.strip().lower()
    tenant.email_confirm_hash = None  # usage unique
    if await address_taken(db, email, tenant.id):
        await db.commit()
        return "prise"
    owner = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.role == Role.OWNER)
                              .order_by(User.created_at).limit(1))).scalar_one_or_none()
    if owner is not None and is_placeholder(owner.email):
        owner.email = email
    if is_placeholder(tenant.email):
        tenant.email = email
    tenant.email_pending = None
    await log_audit_event(db, actor="OWNER", action="EMAIL_CONFIRMED", tenant_id=tenant.id, details={})
    await db.commit()
    if tenant.prereg_at is not None and owner is not None:  # le message de bienvenue des pré-inscrits, maintenant possible
        from app.services import preregistration

        await preregistration.send_signup_emails(db, tenant, owner)
    return "ok"
