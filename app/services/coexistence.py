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

Lot 52 — connexion d'un numéro déjà utilisé dans l'application : dans les 24 h, Bob demande à Meta les contacts
(webhook `smb_app_state_sync`, servent seulement à nommer les clients) et l'historique des 6 derniers mois
(webhook `history`, rangé à part : visible par le commerçant et mémoire de Bob, jamais une conversation).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender

logger = logging.getLogger(__name__)

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


# --- Lot 52 : connexion d'un numéro déjà sur WhatsApp Business (contacts et historique du téléphone) -------------


SYNC_WINDOW = timedelta(hours=24)  # Meta : contacts et historique à demander dans les 24 h, sinon c'est trop tard
HISTORY_DECLINED_CODE = 2593109    # « History sync is turned off by the business from the WhatsApp Business App »
MEMORY_DATE_FORMAT = "%d/%m/%Y"


def digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _values(payload: dict, key: str):
    """Les `value` d'un webhook Meta qui contiennent `key`, avec le numéro de la boutique."""
    try:
        entries = payload.get("entry") or []
    except AttributeError:
        return
    for entry in entries:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            if key in value:
                yield (value.get("metadata") or {}).get("phone_number_id"), value[key]


def parse_contacts(payload: dict) -> list[dict]:
    """Webhook `smb_app_state_sync` : contacts ajoutés, modifiés ou supprimés sur le téléphone."""
    items = []
    for phone_number_id, state_sync in _values(payload, "state_sync"):
        for item in state_sync or []:
            if (item or {}).get("type") != "contact" or not phone_number_id:
                continue
            contact = item.get("contact") or {}
            number = digits(contact.get("phone_number"))
            if not number:
                continue
            items.append({"phone_number_id": phone_number_id, "action": item.get("action") or "add", "wa_number": number,
                          "full_name": (contact.get("full_name") or "").strip() or None,
                          "first_name": (contact.get("first_name") or "").strip() or None})
    return items


def _sent_at(timestamp) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_history(payload: dict) -> list[dict]:
    """Webhook `history` : un morceau de l'historique du téléphone (ou le refus du commerçant)."""
    chunks = []
    for phone_number_id, history in _values(payload, "history"):
        if not phone_number_id:
            continue
        for chunk in history or []:
            chunk = chunk or {}
            errors = chunk.get("errors") or []
            declined = any((e or {}).get("code") == HISTORY_DECLINED_CODE for e in errors)
            progress = (chunk.get("metadata") or {}).get("progress")
            messages = []
            for thread in chunk.get("threads") or []:
                number = digits((thread or {}).get("id"))
                if not number:
                    continue
                for message in thread.get("messages") or []:
                    kind = (message or {}).get("type") or "text"
                    sent_at = _sent_at(message.get("timestamp"))
                    text = echo_text(message) if kind not in IGNORED_TYPES else ""
                    if not text or not message.get("id") or sent_at is None:
                        continue
                    messages.append({"wa_number": number, "from_business": digits(message.get("from")) != number,
                                     "content": text, "wa_message_id": message["id"], "sent_at": sent_at})
            chunks.append({"phone_number_id": phone_number_id, "declined": declined, "errors": bool(errors),
                           "progress": progress if isinstance(progress, int) else None, "messages": messages})
    return chunks


def _split_name(full_name: str | None, first_name: str | None) -> tuple[str | None, str | None]:
    first = first_name or ((full_name or "").split()[0] if full_name else None)
    last = None
    if full_name and first and full_name.startswith(first) and full_name[len(first):].strip():
        last = full_name[len(first):].strip()
    return first, last


def apply_contact_name(customer, contact) -> bool:
    """Nomme un client encore sans nom ; ne remplace jamais un nom déjà connu."""
    if customer is None or contact is None or customer.first_name:
        return False
    first, last = _split_name(contact.full_name, contact.first_name)
    if not first:
        return False
    customer.first_name = first
    if last and not customer.last_name:
        customer.last_name = last
    return True


async def record_contacts(db, items: list[dict]) -> int:
    """Enregistre les contacts du téléphone (jamais de fiche client créée). Renvoie le nombre traité."""
    from app.models.customer import Customer
    from app.models.phone_history import PhoneContact
    from app.repositories.whatsapp_account_repository import WhatsAppAccountRepository

    done = 0
    for item in items:
        account = await WhatsAppAccountRepository(db).find_tenant_by_phone_number_id(item["phone_number_id"])
        if account is None:
            continue
        tenant_id = account.tenant_id
        contact = (await db.execute(select(PhoneContact).where(
            PhoneContact.tenant_id == tenant_id, PhoneContact.wa_number == item["wa_number"]))).scalar_one_or_none()
        if item["action"] == "remove":
            if contact is not None:
                await db.delete(contact)
            done += 1
            continue
        if not (item["full_name"] or item["first_name"]):
            continue
        if contact is None:
            contact = PhoneContact(tenant_id=tenant_id, wa_number=item["wa_number"])
            db.add(contact)
        contact.full_name, contact.first_name = item["full_name"], item["first_name"]
        customer = (await db.execute(select(Customer).where(
            Customer.tenant_id == tenant_id, Customer.whatsapp_number == item["wa_number"]))).scalar_one_or_none()
        apply_contact_name(customer, contact)
        await db.flush()
        done += 1
    await db.commit()
    return done


async def name_from_contacts(db, tenant_id, customer) -> bool:
    """Le client qui écrit n'a pas de nom : celui du téléphone du commerçant, s'il l'a enregistré."""
    from app.models.phone_history import PhoneContact

    if customer is None or customer.first_name:
        return False
    contact = (await db.execute(select(PhoneContact).where(
        PhoneContact.tenant_id == tenant_id, PhoneContact.wa_number == digits(customer.whatsapp_number)))).scalar_one_or_none()
    return apply_contact_name(customer, contact)


async def record_history(db, chunks: list[dict]) -> int:
    """Enregistre l'historique du téléphone (jamais deux fois le même message). Renvoie le nombre ajouté."""
    from app.models.phone_history import PhoneHistoryMessage
    from app.repositories.whatsapp_account_repository import WhatsAppAccountRepository

    added = 0
    for chunk in chunks:
        account = await WhatsAppAccountRepository(db).find_tenant_by_phone_number_id(chunk["phone_number_id"])
        if account is None:
            continue
        if chunk["declined"]:
            account.history_sync_status = "DECLINED"
            continue
        ids = [m["wa_message_id"] for m in chunk["messages"]]
        known = set((await db.execute(select(PhoneHistoryMessage.wa_message_id).where(
            PhoneHistoryMessage.tenant_id == account.tenant_id, PhoneHistoryMessage.wa_message_id.in_(ids)))).scalars().all()) if ids else set()
        for message in chunk["messages"]:
            if message["wa_message_id"] in known:
                continue
            known.add(message["wa_message_id"])
            db.add(PhoneHistoryMessage(tenant_id=account.tenant_id, **message))
            added += 1
        if chunk["progress"] is not None:
            account.history_progress = max(account.history_progress or 0, chunk["progress"])
            account.history_sync_status = "DONE" if account.history_progress >= 100 else "IN_PROGRESS"
    await db.commit()
    return added


async def phone_history(db, tenant_id, wa_number: str, limit: int = 50, before: datetime | None = None) -> list:
    """Les derniers messages repris du téléphone pour ce numéro, du plus ancien au plus récent."""
    from app.models.phone_history import PhoneHistoryMessage

    stmt = select(PhoneHistoryMessage).where(PhoneHistoryMessage.tenant_id == tenant_id,
                                             PhoneHistoryMessage.wa_number == digits(wa_number))
    if before is not None:
        stmt = stmt.where(PhoneHistoryMessage.sent_at < before)
    rows = (await db.execute(stmt.order_by(PhoneHistoryMessage.sent_at.desc()).limit(limit))).scalars().all()
    return list(reversed(rows))


async def memory_messages(db, tenant_id, wa_number: str, before: datetime | None, limit: int) -> list[Message]:
    """
    Mémoire de Bob : les derniers échanges repris du téléphone, datés, pour qu'il connaisse le client sans
    croire que la conversation continue. Messages en mémoire seulement (jamais enregistrés), commençant
    toujours par un message du client.
    """
    if limit <= 0:
        return []
    rows = await phone_history(db, tenant_id, wa_number, limit=limit, before=before)
    while rows and rows[0].from_business:
        rows = rows[1:]
    return [Message(tenant_id=tenant_id, sender=MessageSender.HUMAN if row.from_business else MessageSender.CUSTOMER,
                    message_type="phone_history",
                    content=f"[Échange du {_as_utc(row.sent_at).strftime(MEMORY_DATE_FORMAT)}, avant Bob] {row.content}")
            for row in rows]


async def request_syncs(db, account, oauth_client, now: datetime | None = None) -> list[str]:
    """
    Demande à Meta les contacts puis l'historique du téléphone (dans les 24 h qui suivent la connexion).
    Une demande refusée sera retentée par la tâche planifiée ; jamais d'erreur pour le commerçant.
    """
    now = now or datetime.now(timezone.utc)
    connected = _as_utc(account.connected_at) if account.connected_at else now
    if not account.coexistence_mode or now - connected > SYNC_WINDOW:
        return []
    done = []
    for sync_type, field in (("smb_app_state_sync", "contacts_sync_requested_at"), ("history", "history_sync_requested_at")):
        if getattr(account, field) is not None:
            continue
        try:
            await oauth_client.request_smb_sync(account.phone_number_id, sync_type, account.system_user_token)
        except Exception:  # noqa: BLE001 — retentée plus tard ; jamais le jeton dans les journaux
            logger.warning("Synchronisation %s non demandée (numéro %s)", sync_type, account.phone_number_id)
            continue
        setattr(account, field, now)
        if sync_type == "history" and account.history_sync_status is None:
            account.history_sync_status = "REQUESTED"
        done.append(sync_type)
    await db.commit()
    return done
