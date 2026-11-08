"""
Lot 116 — bouton « 📞 Appeler » sous le message de Bob quand le client est transmis à la boutique (« Parler au
restaurant », « Un conseiller », groupe de 10 ou plus, demande transmise…), tous secteurs.

WhatsApp n'accepte qu'une adresse web dans un bouton-lien (cta_url) : le bouton ouvre /appel/{jeton}, petite page de
Bob qui lance aussitôt le composeur du téléphone (tel:) avec le numéro d'appel de la boutique. Appel classique, au
prix du forfait du client ; aucun coût pour Bob. Le clic est noté dans la conversation (« 📞 Le client a ouvert le
bouton d'appel »), jamais pour un robot d'aperçu.

Numéro : tenants.call_phone (« Numéro pour les appels », Réglages de Bob), sinon le numéro WhatsApp de la boutique.
Heures : seulement quand on peut répondre — restaurant : cuisine ouverte ; secteurs à rendez-vous avec des horaires
réglés : dans ces horaires ; sinon toujours. En dehors : « le restaurant (la boutique…) vous répond ici », sans bouton.
"""
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from app.core.config import get_settings

LABEL = "📞 Appeler"
NOTE_EVERY = timedelta(minutes=10)


def clean_phone(raw: str | None) -> str | None:
    """« +225 07 07 00 00 99 » → « +2250707000099 » ; None si ce n'est pas un numéro plausible (8 à 15 chiffres)."""
    digits = re.sub(r"\D", "", raw or "")
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}" if (raw or "").strip().startswith("+") or len(digits) > 10 else digits


async def number_for(db, tenant) -> str | None:
    """Le numéro qui sonne : celui réglé pour les appels, sinon le numéro WhatsApp de la boutique."""
    own = clean_phone(getattr(tenant, "call_phone", None))
    if own:
        return own
    from sqlalchemy import select

    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    return clean_phone(account.display_phone_number) if account is not None else None


def _within(hours: dict | None, zone, now: datetime) -> bool | None:
    """Dans les horaires ? None s'il n'y a pas d'horaires réglés."""
    from app.services.booking import _minutes

    if not hours or not any(hours.values()):
        return None
    local = now.astimezone(zone)
    minute = local.hour * 60 + local.minute
    for start, end in hours.get(str(local.weekday())) or []:
        try:
            if _minutes(start) <= minute < _minutes(end):
                return True
        except ValueError:
            continue
    return False


async def open_now(db, tenant, now: datetime | None = None) -> bool:
    from app.services import booking, restaurant
    from app.services.business_type import is_restaurant
    from app.services.local_time import tenant_zone

    now = now or datetime.now(timezone.utc)
    if is_restaurant(tenant):
        return restaurant.kitchen_open(tenant, now)
    settings = await booking.load_settings(db, tenant.id)
    inside = _within(getattr(settings, "opening_hours", None), tenant_zone(tenant), now) if settings is not None else None
    return True if inside is None else inside


def token(conversation_id) -> str:
    """Jeton du lien : l'identifiant de la conversation et sa signature (personne ne peut viser une autre conversation)."""
    raw = uuid.UUID(str(conversation_id)).hex
    sig = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{raw}{sig}"


def conversation_of(value: str) -> uuid.UUID | None:
    if not re.fullmatch(r"[0-9a-f]{48}", value or ""):
        return None
    raw, sig = value[:32], value[32:]
    expected = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return uuid.UUID(raw) if hmac.compare_digest(sig, expected) else None


def link(conversation_id) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/appel/{token(conversation_id)}"


async def offer(db, tenant, conversation) -> str | None:
    """L'adresse du bouton, ou None (pas de numéro, ou en dehors des heures)."""
    if conversation is None or tenant is None or getattr(tenant, "is_demo", False):
        return None
    if not await number_for(db, tenant) or not await open_now(db, tenant):
        return None
    return link(conversation.id)


def closed_line(tenant) -> str:
    from app.services.business_type import is_restaurant

    who = "Le restaurant" if is_restaurant(tenant) else "L'équipe"
    return f"{who} vous répond ici dès que possible."


async def send(wa_client, to: str, body: str, url: str):
    """Le message de transfert avec le bouton ; si le texte est trop long, le texte puis un court message à bouton."""
    if len(body) <= 1000:
        return await wa_client.send_cta_url(to=to, body=body, display_text=LABEL, url=url)
    await wa_client.send_text_message(to=to, body=body)
    return await wa_client.send_cta_url(to=to, body="Vous pouvez aussi appeler directement.", display_text=LABEL, url=url)


def page(shop: str, number: str) -> str:
    target = f"tel:{number}"
    shown = escape(number)
    return (
        '<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>Appeler {escape(shop)}</title>"
        f'<meta http-equiv="refresh" content="0;url={escape(target, quote=True)}">'
        "<style>body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#F5F8F6;color:#13241A;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}"
        "a{display:inline-block;background:#1B4332;color:#fff;text-decoration:none;padding:14px 18px;border-radius:12px;"
        "font-weight:700}@media (prefers-color-scheme: dark){body{background:#0D1511;color:#E4EEE8}a{background:#52B788;color:#0D1511}}"
        "</style></head><body><main style=\"text-align:center;max-width:420px\">"
        f"<p style=\"font-weight:700\">{escape(shop)}</p><p>{shown}</p>"
        f'<a href="{escape(target, quote=True)}">📞 Appeler</a></main></body></html>'
    )


async def note_click(db, tenant_id, conversation_id) -> bool:
    """« 📞 Le client a ouvert le bouton d'appel » dans la conversation (au plus une fois toutes les 10 minutes)."""
    from sqlalchemy import select

    from app.models.conversation import Message, MessageSender

    last = (await db.execute(select(Message.created_at).where(
        Message.conversation_id == conversation_id, Message.message_type == "call_click").order_by(
        Message.created_at.desc()).limit(1))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if last is not None and (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) > now - NOTE_EVERY:
        return False
    db.add(Message(tenant_id=tenant_id, conversation_id=conversation_id, sender=MessageSender.SYSTEM, message_type="call_click",
                   content="📞 Le client a ouvert le bouton d'appel"))
    return True
