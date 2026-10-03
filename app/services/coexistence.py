"""
Lot 51 — coexistence : le commerçant garde son numéro dans l'application WhatsApp Business de son
téléphone ET Bob y est branché. Meta envoie à Bob une copie (« écho », champ de webhook
`smb_message_echoes`) de chaque message que le vendeur tape sur son téléphone.

- L'écho est enregistré dans la conversation comme une réponse du vendeur (jamais renvoyé au client).
- Bob se met en pause sur CETTE conversation (jamais toute la boutique) : il ne répond pas par-dessus
  le vendeur.
- Il reprend si le client réécrit 2 h après la dernière réponse du vendeur sans nouvelle réponse de sa
  part — sauf si un conseiller a pris le contrôle depuis le tableau de bord (reprise à la main).
- Les modifications et suppressions de messages faites sur le téléphone sont ignorées.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender

PHONE_ECHO_TYPE = "phone_echo"
PHONE_PAUSE_TYPE = "phone_pause"
PHONE_RESUME_TYPE = "phone_resume"
RESUME_AFTER = timedelta(hours=2)

PAUSE_TEXT = ("Le vendeur a répondu depuis son téléphone : Bob se met en pause sur cette conversation. "
              "Il reprendra si le client réécrit après 2 h sans nouvelle réponse du vendeur.")
RESUME_TEXT = "Pas de réponse du vendeur depuis 2 h : Bob reprend la conversation."

IGNORED_TYPES = frozenset({"edit", "revoke", "reaction", "unsupported"})
MEDIA_LABELS = {
    "image": "📷 Photo", "video": "🎬 Vidéo", "audio": "🎤 Message vocal", "document": "📄 Document",
    "sticker": "Autocollant", "location": "📍 Position", "contacts": "👤 Contact",
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def echo_text(echo: dict) -> str:
    kind = echo.get("type") or "text"
    if kind == "text":
        return ((echo.get("text") or {}).get("body") or "").strip()
    body = echo.get(kind) if isinstance(echo.get(kind), dict) else {}
    caption = (body.get("caption") or "").strip() if body else ""
    label = MEDIA_LABELS.get(kind, "Message")
    return f"{label} : {caption}" if caption else label


def parse_echoes(payload: dict) -> list[dict]:
    """Tous les échos d'un webhook Meta ; liste vide si ce n'est pas un webhook d'échos."""
    echoes = []
    try:
        entries = payload.get("entry") or []
    except AttributeError:
        return []
    for entry in entries:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            for echo in value.get("message_echoes") or []:
                kind = (echo or {}).get("type") or "text"
                if kind in IGNORED_TYPES or not echo.get("to") or not phone_number_id:
                    continue
                text = echo_text(echo)
                if not text:
                    continue
                echoes.append({"phone_number_id": phone_number_id, "to": echo["to"], "wa_message_id": echo.get("id"),
                               "type": kind, "text": text})
    return echoes


def pause_for_phone_reply(db, conversation: Conversation, now: datetime) -> bool:
    """Le vendeur vient de répondre depuis son téléphone. True si Bob vient de se mettre en pause."""
    conversation.phone_reply_at = now
    if conversation.status not in (ConversationStatus.ACTIVE, ConversationStatus.WAITING_CUSTOMER):
        return False  # déjà en attente d'un humain (transfert, prise de contrôle) : rien à annoncer
    conversation.status = ConversationStatus.WAITING_HUMAN
    db.add(Message(tenant_id=conversation.tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                   message_type=PHONE_PAUSE_TYPE, content=PAUSE_TEXT))
    return True


def resume_due(conversation: Conversation, now: datetime) -> bool:
    return (conversation.status == ConversationStatus.WAITING_HUMAN and conversation.assigned_agent is None
            and conversation.phone_reply_at is not None
            and _as_utc(now) - _as_utc(conversation.phone_reply_at) >= RESUME_AFTER)


def resume(db, conversation: Conversation) -> None:
    conversation.status = ConversationStatus.ACTIVE
    conversation.phone_reply_at = None
    conversation.human_alert_sent_at = None
    db.add(Message(tenant_id=conversation.tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                   message_type=PHONE_RESUME_TYPE, content=RESUME_TEXT))


async def _already_recorded(db, conversation_id, wa_message_id: str | None) -> bool:
    """Meta peut renvoyer le même webhook : un écho n'est jamais enregistré deux fois."""
    if not wa_message_id:
        return False
    rows = (await db.execute(select(Message.message_metadata).where(
        Message.conversation_id == conversation_id, Message.sender == MessageSender.HUMAN,
        Message.message_type == PHONE_ECHO_TYPE,
    ))).scalars().all()
    return any((meta or {}).get("wa_message_id") == wa_message_id for meta in rows)


async def record_echoes(db, echoes: list[dict], now: datetime | None = None) -> list:
    """Enregistre les échos de numéros connus. Renvoie la boutique de chaque message enregistré."""
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.customer_repository import CustomerRepository
    from app.repositories.whatsapp_account_repository import WhatsAppAccountRepository

    now = now or datetime.now(timezone.utc)
    recorded = []
    for echo in echoes:
        account = await WhatsAppAccountRepository(db).find_tenant_by_phone_number_id(echo["phone_number_id"])
        if account is None:
            continue  # numéro inconnu : jamais d'erreur (Meta réessaierait en boucle)
        account.coexistence_mode = True  # le numéro est bien partagé avec l'application du téléphone
        tenant_id = account.tenant_id
        customer, _ = await CustomerRepository(db).get_or_create_with_created_flag(tenant_id, echo["to"])
        conversation = await ConversationRepository(db).get_or_create_active(tenant_id, customer.id)
        if await _already_recorded(db, conversation.id, echo["wa_message_id"]):
            continue
        db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.HUMAN,
                       message_type=PHONE_ECHO_TYPE, content=echo["text"],
                       message_metadata={"wa_message_id": echo["wa_message_id"], "source": "WHATSAPP_BUSINESS_APP"}))
        conversation.last_message_at = now
        pause_for_phone_reply(db, conversation, now)
        await db.flush()
        recorded.append(tenant_id)
    await db.commit()
    return recorded
