"""
Lot 92 — « Bob, votre assistant » : le commerçant parle à Bob sur le numéro AgenC'AI (contrat Bob ↔ Naomy 1.3.1,
section 4 ter ; décisions de Thérèse du 27/10).

Naomy reçoit les messages du numéro AgenC'AI ; son aiguilleur transmet à Bob ceux qui sont pour Bob
(POST /api/v1/naomy/assistant/messages). Bob répond, et envoie ses alertes, par Naomy (webhook « assistantEnvoi »,
signé avec le secret de la boutique). Rien d'autre ne change : sans interrupteur allumé (général ET boutique),
la route répond 409 et aucune alerte n'est préparée.

Règles :
- aucun nouveau coût d'IA : commandes reconnues par mots fixes, photo + légende lue sans IA, notes vocales non
  transcrites (« Écrivez-moi pour l'instant ») ;
- sur WhatsApp, seulement les alertes (vente à confirmer, client qui attend) et les réponses au commerçant ;
  le bilan du soir part par e-mail ;
- « Vendu », « Annuler » et « Bob continue » font exactement ce que font les boutons du tableau de bord (mêmes
  fonctions) ; « annuler » n'annule jamais rien sans une confirmation explicite ;
- 30 messages par boutique et par jour au plus ; une alerte qui ne peut pas partir sur WhatsApp (plafond, fenêtre
  de 24 h fermée, Naomy injoignable) part par e-mail ;
- le texte reçu est effacé dès qu'il est traité ; une image n'est gardée que si elle devient la photo d'un produit.

Lot 92b (pilote du 27/10) : un produit peut s'ajouter en deux messages. Photo seule (ou sans prix) : Bob la garde et
demande « le nom et le prix » ; texte seul avec un nom et un prix : Bob demande la photo. Le produit n'est créé qu'avec
sa photo ET son prix. Une photo jamais complétée est effacée au bout de 2 heures.
"""
import base64
import binascii
import logging
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, or_, select, update

from app.services.naomy_bridge import NaomyError, iso, sign

logger = logging.getLogger(__name__)

KINDS = ("texte", "image", "audio", "bouton")
IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
AUDIO_MIMES = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr"}
MAX_IMAGE = 5 * 1024 * 1024
MAX_AUDIO = 16 * 1024 * 1024
DAILY_CAP = 30
RETRY_MINUTES = [1, 5, 30]
AWAIT_MINUTES = 30
ALERT_WINDOW = timedelta(minutes=30)       # messages des clients regardés pour une alerte
PROOF_ORDER_DAYS = 7                       # une image vaut « preuve » pour une commande de moins de 7 jours
WAITING_REPEAT = timedelta(hours=12)       # une seule alerte « vous attend » par conversation et par 12 h
SUMMARY_HOUR = 20                          # bilan du soir par e-mail, heure du pays de la boutique
LIST_LINES = 5
STORE = Path(__file__).resolve().parents[1] / "var" / "assistant"
STORE_MAX_AGE = timedelta(hours=2)        # lot 92b : photo gardée en attendant son nom et son prix
PRODUCT_UPLOADS = Path(__file__).resolve().parents[1] / "static" / "uploads" / "products"

# Commandes connues (liste x-commandes-bob du contrat 1.3.1), comparées sans accents ni majuscules, sur le message
# entier ou son début (« stock robe wax »). Les plus longues d'abord.
COMMANDS = {
    "ventes a confirmer": "to_confirm", "nouveau produit": "add", "mes commandes": "to_confirm", "mes ventes": "sales",
    "qui attend": "waiting", "mon stock": "stock", "aide bob": "help", "commandes": "to_confirm", "reprendre": "resume",
    "annuler": "cancel", "ajouter": "add", "clients": "waiting", "ventes": "sales", "vendu": "sold", "stock": "stock",
    "pause": "pause", "aide": "help", "menu": "help",
}

MENU = ("Je suis Bob, votre vendeur. Écrivez par exemple :\n"
        "• mes ventes : vos ventes du jour, de la semaine et du mois\n"
        "• ventes à confirmer : les ventes qui attendent votre « Vendu »\n"
        "• vendu : confirmer une vente\n"
        "• qui attend : les clients qui vous attendent\n"
        "• stock : les produits épuisés (ou « stock robe » pour un produit)\n"
        "• ajouter : ajouter un produit en photo\n\n"
        "Pour ajouter un produit : envoyez sa photo, puis son nom et son prix (par exemple : Sac cabas 12000).")
ADD_HELP = ("Pour ajouter un produit : envoyez sa photo, puis son nom et son prix, par exemple : Sac cabas 12000.\n"
            "Vous pouvez aussi tout envoyer d'un coup, en légende de la photo.")
VOICE_REPLY = "Je ne sais pas encore écouter les notes vocales : écrivez-moi pour l'instant (par exemple « mes ventes »)."
RETURN_REPLY = "C'est noté, rien n'a changé."


# --- Petits outils ---------------------------------------------------------------------------------------------------

def assistant_on(connection) -> bool:
    from app.core.config import get_settings

    return bool(get_settings().assistant_enabled and connection is not None and connection.status == "ACTIVE"
                and connection.assistant_enabled)


def normalize(text: str | None) -> str:
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def without_bob(norm: str) -> str:
    return norm[4:] if norm.startswith("bob ") else ("" if norm == "bob" else norm)


def match_command(text: str | None) -> tuple[str | None, str]:
    norm = without_bob(normalize(text))
    for phrase in sorted(COMMANDS, key=len, reverse=True):
        if norm == phrase:
            return COMMANDS[phrase], ""
        if norm.startswith(phrase + " "):
            return COMMANDS[phrase], norm[len(phrase) + 1:]
    return None, norm


def short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def amount(value) -> str:
    from app.services.simple_mode import _amount

    return _amount(value)


def assistant_url() -> str:
    from app.core.config import get_settings

    settings = get_settings()
    return f"https://{settings.naomy_allowed_host}{settings.naomy_allowed_path.rstrip('/')}/assistant/envoyer"


def dashboard_link(path: str = "") -> str:
    from app.core.config import get_settings

    return f"{get_settings().public_base_url.rstrip('/')}/dashboard/{path}"


def parse_caption(caption: str | None) -> tuple[str | None, Decimal | None]:
    """« Bob, Sac cabas en raphia 12000 » → (« Sac cabas en raphia », 12000). Sans IA : le dernier nombre est le prix."""
    text = " ".join((caption or "").split())
    text = re.sub(r"^\s*bob\b[\s,:;.!-]*", "", text, flags=re.IGNORECASE)
    found = re.search(r"(\d[\d\s.,]*\d|\d)\s*(?:f|fr|fcfa|cfa|xof|€|eur|euros?)?\s*\.?\s*$", text, flags=re.IGNORECASE)
    if not found:
        return (text.strip(" -:,") or None), None
    digits = found.group(1)
    if re.fullmatch(r"\d{1,3}([ .,]\d{3})+", digits):  # « 12 000 », « 12.000 », « 1,500,000 »
        digits = re.sub(r"[ .,]", "", digits)
    else:
        digits = digits.replace(" ", "").replace(",", ".")
    try:
        price = Decimal(digits)
    except InvalidOperation:
        price = None
    name = text[:found.start()].strip(" -:,")
    return (name[:255] or None), (price if price is not None and price > 0 else None)


# --- Réception (route POST /assistant/messages) ----------------------------------------------------------------------

def _decode(media: dict, kind: str) -> tuple[bytes, str]:
    mime = media.get("mime")
    raw = media.get("base64")
    allowed = IMAGE_MIMES if kind == "image" else AUDIO_MIMES
    if mime not in allowed or not isinstance(raw, str):
        raise NaomyError(400, "requete_invalide", "media : type ou contenu invalide.")
    limit = MAX_IMAGE if kind == "image" else MAX_AUDIO
    if len(raw) > (limit * 4) // 3 + 8:
        raise NaomyError(413, "media_trop_lourd", "Média trop lourd (image 5 Mo, note vocale 16 Mo).")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NaomyError(400, "requete_invalide", "media.base64 invalide.") from exc
    if len(data) > limit:
        raise NaomyError(413, "media_trop_lourd", "Média trop lourd (image 5 Mo, note vocale 16 Mo).")
    return data, mime


async def receive(db, connection, tenant, payload: dict, now: datetime | None = None) -> dict:
    from sqlalchemy.exc import IntegrityError

    from app.models.naomy import AssistantMessage

    now = now or datetime.now(timezone.utc)
    if not assistant_on(connection):
        raise NaomyError(409, "assistant_eteint", "L'assistant de Bob n'est pas activé pour cette boutique.")
    allowed = {"id_message", "recu_le", "type", "texte", "bouton_id", "reponse_a", "media"}
    wa_id, kind, text, button = payload.get("id_message"), payload.get("type"), payload.get("texte"), payload.get("bouton_id")
    if not (set(payload) <= allowed and isinstance(wa_id, str) and 0 < len(wa_id) <= 128 and kind in KINDS
            and isinstance(payload.get("recu_le"), str) and (text is None or (isinstance(text, str) and len(text) <= 4096))
            and (button is None or (isinstance(button, str) and button.startswith("bob:") and len(button) <= 256))
            and (payload.get("reponse_a") is None or (isinstance(payload.get("reponse_a"), str) and len(payload["reponse_a"]) <= 128))
            and (payload.get("media") is None or isinstance(payload.get("media"), dict))):
        raise NaomyError(400, "requete_invalide", "Message attendu au format MessageCommercant (contrat 1.3.1).")
    if kind == "bouton" and not button:
        raise NaomyError(400, "requete_invalide", "bouton_id attendu (préfixe bob:).")
    if kind == "texte" and not (text or "").strip():
        raise NaomyError(400, "requete_invalide", "texte attendu.")
    if kind in ("image", "audio") and not payload.get("media"):
        raise NaomyError(400, "requete_invalide", "media attendu.")
    existing = (await db.execute(select(AssistantMessage.id).where(AssistantMessage.connection_id == connection.id,
                                                                   AssistantMessage.wa_message_id == wa_id))).first()
    if existing:
        return {"id_message": wa_id, "statut": "deja_recu"}
    media_path = None
    if kind in ("image", "audio"):
        data, mime = _decode(payload["media"], kind)
        if kind == "image":  # gardée hors du site, seulement le temps du traitement
            STORE.mkdir(parents=True, exist_ok=True)
            target = STORE / f"{uuid.uuid4().hex}.{IMAGE_MIMES[mime]}"
            target.write_bytes(data)
            media_path = target.name
        del data
    message = AssistantMessage(id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id, wa_message_id=wa_id,
                               kind=kind, text=text, button_id=button, media_path=media_path, status="RECEIVED",
                               received_at=now)
    db.add(message)
    try:
        await db.commit()
    except IntegrityError:  # même message reçu deux fois en même temps
        await db.rollback()
        _remove(media_path)
        return {"id_message": wa_id, "statut": "deja_recu"}
    schedule(message.id)
    return {"id_message": wa_id, "statut": "recu"}


def schedule(message_id) -> None:
    """Traitement tout de suite par le worker ; à défaut, la tâche de chaque minute s'en charge."""
    try:
        from app.workers.assistant import process_assistant_message_task

        process_assistant_message_task.delay(str(message_id))
    except Exception:  # noqa: BLE001
        logger.info("Message de l'assistant laissé à la tâche de chaque minute")


def _remove(name: str | None) -> None:
    if name:
        try:
            (STORE / Path(name).name).unlink(missing_ok=True)
        except OSError:
            pass


# --- Réponses et alertes : file d'envoi ------------------------------------------------------------------------------

def queue(db, connection, text: str, buttons: list[tuple[str, str]] | None = None, kind: str = "REPLY",
          ref: str | None = None, awaiting: str | None = None, fallback_subject: str | None = None,
          now: datetime | None = None):
    from app.models.naomy import AssistantOutbox

    now = now or datetime.now(timezone.utc)
    message_id = "bob_" + secrets.token_hex(12)
    text = text.strip()
    if len(text) > 1024:
        text = text[:1023].rstrip() + "…"
    payload = {"boutique_id": str(connection.tenant_id), "id_message_bob": message_id, "texte": text}
    if buttons:
        payload["boutons"] = [{"id": bid[:256], "titre": short(title, 20)} for bid, title in buttons[:3]]
    if awaiting:
        payload["attend_reponse"] = True
        connection.awaiting, connection.awaiting_until = awaiting[:400], now + timedelta(minutes=AWAIT_MINUTES)
    item = AssistantOutbox(id=uuid.uuid4(), tenant_id=connection.tenant_id, connection_id=connection.id,
                           message_id=message_id, kind=kind, ref=ref, payload=payload, fallback_subject=fallback_subject,
                           status="PENDING", attempts=0, created_at=now)
    db.add(item)
    return item


async def http_post(url: str, body: bytes, headers: dict) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code


async def _sent_today(db, tenant, now) -> int:
    from app.models.naomy import AssistantOutbox
    from app.services.local_time import tenant_zone

    zone = tenant_zone(tenant)
    start = datetime.combine(now.astimezone(zone).date(), time.min, zone).astimezone(timezone.utc)
    return int((await db.execute(select(func.count(AssistantOutbox.id)).where(
        AssistantOutbox.tenant_id == tenant.id, AssistantOutbox.status == "SENT", AssistantOutbox.sent_at >= start))).scalar_one())


async def _email_fallback(item, tenant, mailer) -> bool:
    import asyncio

    if not (tenant.email or "").strip():
        return False
    subject = item.fallback_subject or "Bob : une alerte pour vous"
    body = (f"Bonjour,\n\n{item.payload.get('texte', '')}\n\nOuvrir Bob : {dashboard_link()}\n\n"
            "Ce message vous arrive par e-mail parce qu'il n'a pas pu partir sur WhatsApp.")
    try:
        return bool(await asyncio.to_thread(mailer, to=tenant.email, subject=subject, body=body))
    except Exception:  # noqa: BLE001
        return False


async def send_outbox(db, now: datetime | None = None, sender=None, mailer=None, tenant_id=None) -> dict:
    import json

    from app.models.naomy import AssistantOutbox, NaomyConnection
    from app.models.tenant import Tenant

    now = now or datetime.now(timezone.utc)
    sender = sender or http_post
    if mailer is None:
        from app.services.email_service import send_email as mailer
    report = {"sent": 0, "emailed": 0, "failed": 0, "retry": 0, "skipped": 0}
    stmt = select(AssistantOutbox).where(AssistantOutbox.status == "PENDING", or_(
        AssistantOutbox.next_attempt_at.is_(None), AssistantOutbox.next_attempt_at <= now))
    if tenant_id is not None:
        stmt = stmt.where(AssistantOutbox.tenant_id == tenant_id)
    due = (await db.execute(stmt.order_by(AssistantOutbox.created_at).limit(200))).scalars().all()
    url = assistant_url()
    for item in due:
        connection = await db.get(NaomyConnection, item.connection_id)
        tenant = await db.get(Tenant, item.tenant_id)

        async def give_up(error: str):
            item.last_error = error[:300]
            if item.kind == "ALERT" and tenant is not None and await _email_fallback(item, tenant, mailer):
                item.status = "EMAILED"
                report["emailed"] += 1
            else:
                item.status = "FAILED"
                report["failed"] += 1

        if tenant is None or not assistant_on(connection):
            item.status, item.last_error = "SKIPPED", "Assistant éteint"
            report["skipped"] += 1
            continue
        if await _sent_today(db, tenant, now) >= DAILY_CAP:
            if item.kind == "ALERT":
                await give_up("Plafond de 30 messages atteint aujourd'hui")
            else:
                item.status, item.last_error = "SKIPPED", "Plafond de 30 messages atteint aujourd'hui"
                report["skipped"] += 1
            continue
        body = json.dumps(item.payload, ensure_ascii=False, separators=(",", ":")).encode()
        stamp = str(int(now.timestamp()))
        headers = {"Content-Type": "application/json", "X-Bob-Timestamp": stamp,
                   "X-Bob-Signature": sign(connection.events_secret, stamp, body), "User-Agent": "Bob-Naomy/1"}
        try:
            status = await sender(url, body, headers)
        except Exception as exc:  # noqa: BLE001
            status = None
            error = f"Naomy injoignable ({type(exc).__name__})"
        else:
            error = None if 200 <= status < 300 else f"Naomy a répondu {status}"
        item.attempts = (item.attempts or 0) + 1
        if error is None:
            item.status, item.sent_at, item.last_error = "SENT", now, None
            report["sent"] += 1
        elif status in (400, 401, 409, 413, 429) or item.attempts > len(RETRY_MINUTES):
            await give_up(error)  # 409 : fenêtre de 24 h fermée ou assistant éteint chez Naomy ; 429 : plafond de Naomy
        else:
            item.next_attempt_at, item.last_error = now + timedelta(minutes=RETRY_MINUTES[item.attempts - 1]), error
            report["retry"] += 1
    await db.commit()
    return report


# --- Actions du commerçant (mêmes fonctions que le tableau de bord) ------------------------------------------------

async def acting_user(db, tenant):
    """Le propriétaire de la boutique (à défaut un administrateur) : c'est lui qui écrit depuis son WhatsApp."""
    from sqlalchemy import case

    from app.core.security import CurrentUser
    from app.models.user import User

    user = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.active.is_(True),
                                                User.role.in_(("OWNER", "ADMIN")))
                             .order_by(case((User.role == "OWNER", 0), else_=1), User.created_at))).scalars().first()
    if user is None:
        return None
    return CurrentUser(user_id=user.id, tenant_id=tenant.id, role=str(getattr(user.role, "value", user.role)))


async def _order(db, tenant, raw_id):
    from app.models.order import Order

    try:
        oid = uuid.UUID(str(raw_id))
    except ValueError:
        return None
    order = await db.get(Order, oid)
    return order if order is not None and order.tenant_id == tenant.id else None


async def _order_label(db, tenant, order) -> str:
    from app.models.customer import Customer
    from app.services.simple_mode import customer_label

    customer = await db.get(Customer, order.customer_id)
    return f"{customer_label(customer)} ({amount(order.total_amount)} {order.currency})"


async def confirm_sale(db, tenant, order_id) -> str:
    from fastapi import BackgroundTasks, HTTPException

    from app.api.orders.routes import mark_order_paid
    from app.models.order import OrderStatus

    order = await _order(db, tenant, order_id)
    if order is None:
        return "Je ne trouve pas cette vente."
    if order.status == OrderStatus.PAID:
        return "Cette vente est déjà confirmée."
    if order.status != OrderStatus.PENDING:
        return "Cette vente a été annulée : elle ne peut plus être confirmée."
    label = await _order_label(db, tenant, order)
    user = await acting_user(db, tenant)
    if user is None:
        return "Je ne trouve pas le compte du propriétaire de la boutique : confirmez la vente dans Bob."
    tasks = BackgroundTasks()
    try:
        await mark_order_paid(order.id, tasks, user, db)
    except HTTPException as exc:
        return str(exc.detail)
    await tasks()
    return f"Vente confirmée : {label}. Le reçu part chez le client."


async def cancel_sale(db, tenant, order_id) -> str:
    from fastapi import HTTPException

    from app.api.orders.routes import cancel_order_route
    from app.models.order import OrderStatus
    from app.schemas.order import OrderCancelRequest

    order = await _order(db, tenant, order_id)
    if order is None:
        return "Je ne trouve pas cette vente."
    if order.status != OrderStatus.PENDING:
        return "Cette vente n'est plus à confirmer : rien n'a changé."
    label = await _order_label(db, tenant, order)
    user = await acting_user(db, tenant)
    if user is None:
        return "Je ne trouve pas le compte du propriétaire de la boutique : annulez la vente dans Bob."
    try:
        await cancel_order_route(order.id, OrderCancelRequest(reason="Annulée depuis WhatsApp (assistant)",
                                                              notify_customer=False), user, db)
    except HTTPException as exc:
        return str(exc.detail)
    return f"Vente annulée : {label}. Les articles sont remis en stock."


async def bob_continues(db, tenant, conversation_id) -> str:
    from fastapi import HTTPException

    from app.api.conversations.routes import release_conversation
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.customer import Customer
    from app.services.simple_mode import customer_label

    try:
        cid = uuid.UUID(str(conversation_id))
    except ValueError:
        return "Je ne trouve pas cette conversation."
    conversation = await db.get(Conversation, cid)
    if conversation is None or conversation.tenant_id != tenant.id:
        return "Je ne trouve pas cette conversation."
    name = customer_label(await db.get(Customer, conversation.customer_id))
    if conversation.status != ConversationStatus.WAITING_HUMAN and conversation.phone_reply_at is None:
        return f"Bob s'occupe déjà de {name}."
    user = await acting_user(db, tenant)
    if user is None:
        return "Je ne trouve pas le compte du propriétaire de la boutique : rendez la conversation à Bob dans Bob."
    try:
        await release_conversation(conversation.id, user, db)
    except HTTPException as exc:
        return str(exc.detail)
    return f"Bob reprend la conversation avec {name}."


# --- Commandes écrites -----------------------------------------------------------------------------------------------

async def _sales_text(db, tenant, now=None) -> str:
    from app.services.simple_mode import sales_summary

    s = await sales_summary(db, tenant, now=now)
    cur = s["currency"]

    def line(label, p):
        return f"{label} : {p['revenue']} {cur} ({p['sales']} vente{'s' if p['sales'] > 1 else ''})"

    text = "Vos ventes confirmées :\n" + "\n".join([line("Aujourd'hui", s["today"]), line("7 jours", s["week"]),
                                                    line("30 jours", s["month"])])
    if s["top"]:
        text += "\n\nCe qui se vend le mieux (30 jours) :\n" + "\n".join(
            f"{i}. {short(t['name'], 40)} ({t['quantity']})" for i, t in enumerate(s["top"][:3], 1))
    return text


async def _pending(db, tenant) -> list[dict]:
    from app.services.simple_mode import to_confirm_list

    return await to_confirm_list(db, tenant)


def _sale_line(o: dict) -> str:
    items = f" · {short(o['items'], 60)}" if o["items"] else ""
    return f"• {o['customer']}{items} · {o['total']} {o['currency']}"


async def handle_command(db, connection, tenant, key: str, rest: str, now) -> None:
    from app.models.product import Product
    from app.services.business_type import is_insurance
    from app.services.simple_mode import waiting_list

    if key == "sales":
        queue(db, connection, await _sales_text(db, tenant, now), now=now)
    elif key == "to_confirm":
        items = await _pending(db, tenant)
        if not items:
            queue(db, connection, "Aucune vente à confirmer pour l'instant.", now=now)
        else:
            more = f"\n… et {len(items) - LIST_LINES} autre(s) dans Bob." if len(items) > LIST_LINES else ""
            queue(db, connection, f"Ventes à confirmer ({len(items)}) :\n" + "\n".join(_sale_line(o) for o in items[:LIST_LINES])
                  + more + "\n\nÉcrivez « vendu » pour en confirmer une.", now=now)
    elif key in ("sold", "cancel"):
        items = await _pending(db, tenant)
        verb = "vendu" if key == "sold" else "annuler"
        if not items:
            queue(db, connection, "Aucune vente à confirmer pour l'instant.", now=now)
        elif len(items) == 1:
            o = items[0]
            if key == "sold":
                queue(db, connection, f"Confirmer cette vente ?\n{_sale_line(o)}",
                      [(f"bob:vendu:{o['order_id']}", "Vendu"), ("bob:retour", "Retour")], now=now)
            else:
                queue(db, connection, f"Annuler cette vente ? Les articles reviendront en stock.\n{_sale_line(o)}",
                      [(f"bob:annuler-oui:{o['order_id']}", "Oui, annuler"), ("bob:retour", "Retour")], now=now)
        else:
            question = "Quelle vente est confirmée ?" if key == "sold" else "Quelle vente faut-il annuler ?"
            more = "\nLes autres sont dans Bob." if len(items) > 3 else ""
            queue(db, connection, question + "\n" + "\n".join(_sale_line(o) for o in items[:3]) + more,
                  [(f"bob:{verb}:{o['order_id']}", f"{o['customer']} {o['total']}") for o in items[:3]], now=now)
    elif key == "stock":
        if is_insurance(tenant):
            queue(db, connection, "Le stock ne concerne pas votre activité.", now=now)
        elif rest:
            like = f"%{rest}%"
            found = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True),
                                                            func.lower(Product.name).like(like))
                                      .order_by(Product.name).limit(5))).scalars().all()
            if not found:  # comparaison sans accents, côté Python, sur les produits actifs
                every = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True))
                                          .order_by(Product.name).limit(2000))).scalars().all()
                found = [p for p in every if rest in normalize(p.name)][:5]
            if not found:
                queue(db, connection, f"Aucun produit ne correspond à « {short(rest, 40)} ».", now=now)
            else:
                queue(db, connection, "\n".join(f"• {short(p.name, 50)} : {int(p.stock_quantity or 0)} en stock"
                                                for p in found), now=now)
        else:
            out = (await db.execute(select(Product.name).where(Product.tenant_id == tenant.id, Product.active.is_(True),
                                                               Product.stock_quantity <= 0).order_by(Product.name).limit(10))).scalars().all()
            low = (await db.execute(select(Product.name, Product.stock_quantity).where(
                Product.tenant_id == tenant.id, Product.active.is_(True), Product.stock_quantity.between(1, 2))
                .order_by(Product.stock_quantity, Product.name).limit(10))).all()
            if not out and not low:
                queue(db, connection, "Tous vos produits sont en stock.", now=now)
            else:
                parts = []
                if out:
                    parts.append("Épuisés (Bob ne les propose plus) :\n" + "\n".join(f"• {short(n, 50)}" for n in out))
                if low:
                    parts.append("Bientôt épuisés :\n" + "\n".join(f"• {short(n, 50)} : {q}" for n, q in low))
                queue(db, connection, "\n\n".join(parts) + "\n\nPour un produit : « stock robe ».", now=now)
    elif key in ("waiting", "resume"):
        items = await waiting_list(db, tenant)
        if not items:
            queue(db, connection, "Personne ne vous attend : Bob s'occupe de tous vos clients.", now=now)
        else:
            lines = "\n".join(f"• {w['name']} : « {short(w['last_message'], 80)} »" if w["last_message"] else f"• {w['name']}"
                              for w in items[:LIST_LINES])
            buttons = [(f"bob:continue:{w['conversation_id']}", f"Bob : {w['name']}") for w in items[:3]]
            queue(db, connection, f"Clients qui vous attendent ({len(items)}) :\n{lines}\n\n"
                  f"Répondez-leur dans Bob : {dashboard_link('?tab=conversations')}\n"
                  "Ou laissez Bob continuer avec l'un d'eux :", buttons, now=now)
    elif key == "add":
        queue(db, connection, ADD_HELP, now=now)
    elif key == "pause":
        queue(db, connection, "Pour arrêter Bob avec un client, répondez-lui vous-même : Bob s'arrête sur cette "
                              "conversation. Écrivez « reprendre » pour lui rendre la main.", now=now)
    else:
        queue(db, connection, MENU, now=now)


# --- Photo avec légende : nouveau produit, sans IA ---------------------------------------------------------------------

PHOTO_ASK = "J'ai votre photo. Quel est le nom et le prix ? Par exemple : Sac cabas 12000"
PRICE_MISSING = "Il me manque le prix : écrivez le nom puis le prix, par exemple : Sac cabas 12000"


def _awaiting(connection, now) -> str | None:
    until = connection.awaiting_until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return connection.awaiting if connection.awaiting and until is not None and until >= now else None


def _kept_photo(awaiting: str | None) -> tuple[str | None, str | None]:
    """« photo:<fichier>|<nom> » → (fichier, nom)."""
    if not awaiting or not awaiting.startswith("photo:"):
        return None, None
    name_part = awaiting[6:].split("|", 1)
    return Path(name_part[0]).name or None, (name_part[1] if len(name_part) > 1 and name_part[1] else None)


def _draft(awaiting: str | None) -> tuple[str | None, Decimal | None]:
    """« draft:<prix>:<nom> » → (nom, prix)."""
    if not awaiting or not awaiting.startswith("draft:"):
        return None, None
    price, _, name = awaiting[6:].partition(":")
    try:
        return (name or None), Decimal(price)
    except InvalidOperation:
        return None, None


async def create_product(db, connection, tenant, name: str, price: Decimal, photo: str | None, now) -> None:
    """Crée le produit avec sa photo (gardée hors du site), puis demande le stock."""
    from app.core.config import get_settings
    from app.models.product import Product
    from app.services.photo_studio import reset_for_new_source
    from app.services.plan_limits import FREEMIUM_MAX_PRODUCTS, remaining_product_slots

    connection.awaiting = connection.awaiting_until = None
    remaining = await remaining_product_slots(db, tenant.id, tenant.is_paid)
    if remaining is not None and remaining <= 0:
        _remove(photo)
        queue(db, connection, f"La formule gratuite est limitée à {FREEMIUM_MAX_PRODUCTS} produits : passez à une "
                              f"formule payante pour en ajouter. {dashboard_link('?tab=settings')}", now=now)
        return
    source = STORE / Path(photo or "").name
    if not photo or not source.is_file():
        queue(db, connection, "La photo n'est plus là : renvoyez-la, s'il vous plaît.", now=now)
        return
    base = re.sub(r"[^A-Z0-9]+", "-", normalize(name).upper()).strip("-")[:40] or "PRODUIT"
    sku = f"{base}-{secrets.token_hex(2).upper()}"
    product = Product(id=uuid.uuid4(), tenant_id=tenant.id, sku=sku, name=name, price=price,
                      currency=tenant.currency or "XOF", stock_quantity=1, active=True)
    PRODUCT_UPLOADS.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{source.suffix}"
    (PRODUCT_UPLOADS / filename).write_bytes(source.read_bytes())
    _remove(photo)
    reset_for_new_source(product, f"{get_settings().public_base_url}/uploads/products/{filename}")
    product.image_status = "PENDING"  # lot 87 : fond blanc studio en arrière-plan
    db.add(product)
    await db.flush()
    queue(db, connection, f"C'est ajouté : {short(name, 60)}, {amount(price)} {product.currency}. Je prépare la photo "
                          "sur fond blanc. Il est en vente avec 1 en stock : combien en avez-vous ?",
          awaiting=f"stock:{product.id}", now=now)


async def add_product_from_photo(db, connection, tenant, message, now) -> None:
    from app.services.business_type import is_insurance

    if is_insurance(tenant):
        queue(db, connection, "L'ajout d'un produit en photo est réservé aux boutiques.", now=now)
        return
    photo = message.media_path
    message.media_path = None  # la photo suit le produit, ou reste gardée en attendant son nom et son prix
    awaiting = _awaiting(connection, now)
    name, price = parse_caption(message.text)
    if not (name and price is not None):
        draft_name, draft_price = _draft(awaiting)
        if draft_name and draft_price is not None:  # lot 92b : le nom et le prix sont arrivés avant la photo
            name, price = draft_name, draft_price
    if name and price is not None:
        old, _ = _kept_photo(awaiting)
        if old and old != photo:
            _remove(old)
        await create_product(db, connection, tenant, name, price, photo, now)
        return
    old, _ = _kept_photo(awaiting)  # une nouvelle photo remplace celle qui attendait
    if old and old != photo:
        _remove(old)
    keep = f"photo:{photo}|{name[:200]}" if name else f"photo:{photo}"
    queue(db, connection, f"J'ai la photo de « {short(name, 60)} ». Quel est son prix ? Par exemple : 12000" if name
          else PHOTO_ASK, awaiting=keep, now=now)


async def handle_awaited(db, connection, tenant, text: str | None, now) -> bool:
    """Réponse attendue (30 minutes) : stock d'un produit ajouté, ou nom et prix d'une photo gardée."""
    from app.models.product import Product

    awaiting = _awaiting(connection, now)
    if not awaiting:
        return False
    norm = without_bob(normalize(text))
    number = re.fullmatch(r"(\d{1,6})( .*)?", norm)
    if awaiting.startswith("stock:") and number:
        try:
            product = await db.get(Product, uuid.UUID(awaiting[6:]))
        except ValueError:
            product = None
        connection.awaiting = connection.awaiting_until = None
        if product is None or product.tenant_id != tenant.id:
            queue(db, connection, "Je ne retrouve pas ce produit : modifiez son stock dans Bob.", now=now)
            return True
        product.stock_quantity = int(number.group(1))
        queue(db, connection, f"C'est noté : {product.stock_quantity} en stock pour {short(product.name, 60)}.", now=now)
        return True
    if awaiting.startswith("photo:"):
        if match_command(text)[0]:
            return False  # une commande passe toujours avant ; la photo reste gardée
        photo, kept_name = _kept_photo(awaiting)
        name, price = parse_caption(text)
        name = name or kept_name
        if name and price is not None:
            await create_product(db, connection, tenant, name, price, photo, now)
        elif price is None:
            keep = f"photo:{photo}|{name[:200]}" if name else f"photo:{photo}"
            queue(db, connection, PRICE_MISSING, awaiting=keep, now=now)
        else:
            queue(db, connection, PHOTO_ASK, awaiting=awaiting, now=now)
        return True
    return False


async def offer_draft(db, connection, tenant, text: str | None, now) -> bool:
    """Lot 92b : texte seul avec un nom et un prix → Bob demande la photo (rien n'est créé sans elle)."""
    from app.services.business_type import is_insurance

    if is_insurance(tenant):
        return False
    name, price = parse_caption(text)
    if not name or price is None or not re.search(r"[a-zA-Z]{2}", normalize(name)):
        return False
    queue(db, connection, f"Ajouter « {short(name, 60)} » à {amount(price)} {tenant.currency or 'XOF'} ? "
                          "Envoyez sa photo.", awaiting=f"draft:{price}:{name[:300]}", now=now)
    return True


def clean_store(now: datetime | None = None) -> int:
    """Photos gardées et jamais complétées : effacées au bout de 2 heures."""
    now = now or datetime.now(timezone.utc)
    removed = 0
    if not STORE.is_dir():
        return 0
    for path in STORE.iterdir():
        try:
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < now - STORE_MAX_AGE:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


async def handle_button(db, connection, tenant, button: str, now) -> None:
    parts = button.split(":", 2)
    action, ref = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
    if action == "vendu" and ref:
        queue(db, connection, await confirm_sale(db, tenant, ref), now=now)
    elif action == "annuler" and ref:  # toujours une confirmation avant d'annuler
        order = await _order(db, tenant, ref)
        from app.models.order import OrderStatus

        if order is None or order.status != OrderStatus.PENDING:
            queue(db, connection, "Cette vente n'est plus à confirmer : rien n'a changé.", now=now)
        else:
            label = await _order_label(db, tenant, order)
            queue(db, connection, f"Annuler la vente de {label} ? Les articles reviendront en stock.",
                  [(f"bob:annuler-oui:{order.id}", "Oui, annuler"), ("bob:retour", "Retour")], now=now)
    elif action == "annuler-oui" and ref:
        queue(db, connection, await cancel_sale(db, tenant, ref), now=now)
    elif action == "continue" and ref:
        queue(db, connection, await bob_continues(db, tenant, ref), now=now)
    elif action == "retour":
        queue(db, connection, RETURN_REPLY, now=now)
    else:
        queue(db, connection, MENU, now=now)


async def process(db, message_id, now: datetime | None = None) -> str:
    """Traite un message reçu (une seule fois), puis efface son texte et son image."""
    from app.models.naomy import AssistantMessage, NaomyConnection
    from app.models.tenant import Tenant

    now = now or datetime.now(timezone.utc)
    claimed = await db.execute(update(AssistantMessage).where(AssistantMessage.id == message_id,
                                                              AssistantMessage.status == "RECEIVED")
                               .values(status="WORKING"))
    await db.commit()
    if not claimed.rowcount:
        return "deja_traite"
    message = await db.get(AssistantMessage, message_id)
    await db.refresh(message)
    connection = await db.get(NaomyConnection, message.connection_id)
    tenant = await db.get(Tenant, message.tenant_id)
    outcome = "DONE"
    try:
        if tenant is None or not assistant_on(connection):
            outcome = "FAILED"
        elif message.kind == "bouton":
            await handle_button(db, connection, tenant, message.button_id or "", now)
        elif message.kind == "audio":
            queue(db, connection, VOICE_REPLY, now=now)
        elif message.kind == "image":
            await add_product_from_photo(db, connection, tenant, message, now)
        elif not await handle_awaited(db, connection, tenant, message.text, now):
            key, rest = match_command(message.text)
            if key or not await offer_draft(db, connection, tenant, message.text, now):
                await handle_command(db, connection, tenant, key or "help", rest, now)
        await db.commit()
    except Exception:  # noqa: BLE001 — un message en échec ne bloque jamais les suivants
        await db.rollback()
        logger.exception("Message de l'assistant non traité (boutique %s)", getattr(tenant, "id", None))
        outcome = "FAILED"
        message = await db.get(AssistantMessage, message_id)
        connection = await db.get(NaomyConnection, message.connection_id)
        if tenant is not None and assistant_on(connection):
            queue(db, connection, "Je n'ai pas pu traiter ce message : réessayez, ou faites-le dans Bob.", now=now)
    _remove(message.media_path)
    message.status, message.processed_at = outcome, now
    message.text = message.button_id = message.media_path = None  # rien n'est gardé
    await db.commit()
    if tenant is not None:
        await send_outbox(db, now, tenant_id=tenant.id)
    return outcome


# --- Alertes : vente à confirmer, client qui attend --------------------------------------------------------------------

async def _alert_exists(db, tenant_id, ref: str, since: datetime | None = None) -> bool:
    from app.models.naomy import AssistantOutbox

    stmt = select(AssistantOutbox.id).where(AssistantOutbox.tenant_id == tenant_id, AssistantOutbox.kind == "ALERT",
                                            AssistantOutbox.ref == ref)
    if since is not None:
        stmt = stmt.where(AssistantOutbox.created_at >= since)
    return (await db.execute(stmt.limit(1))).first() is not None


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def scan_alerts(db, now: datetime | None = None) -> dict:
    from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
    from app.models.customer import Customer
    from app.models.naomy import NaomyConnection
    from app.models.order import Order, OrderItem, OrderStatus
    from app.models.product import Product
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.handoff_service import conversation_link
    from app.services.simple_mode import customer_label

    now = now or datetime.now(timezone.utc)
    report = {"sales": 0, "waiting": 0}
    from app.core.config import get_settings

    if not get_settings().assistant_enabled:
        return report
    connections = (await db.execute(select(NaomyConnection).where(NaomyConnection.status == "ACTIVE",
                                                                  NaomyConnection.assistant_enabled.is_(True)))).scalars().all()
    for connection in connections:
        tenant = await db.get(Tenant, connection.tenant_id)
        if tenant is None or not tenant.active:
            continue
        since = max(now - ALERT_WINDOW, _aware(connection.assistant_enabled_at) or now - ALERT_WINDOW)
        # Vente à confirmer : un client qui a une commande en attente envoie une image (sa preuve, le plus souvent).
        images = (await db.execute(select(Message, Conversation).join(Conversation, Conversation.id == Message.conversation_id)
                                   .where(Message.tenant_id == tenant.id, Message.sender == MessageSender.CUSTOMER,
                                          Message.message_type == "image", Message.created_at >= since)
                                   .order_by(Message.created_at))).all()
        for _msg, conversation in images:
            order = (await db.execute(select(Order).where(
                Order.tenant_id == tenant.id, Order.customer_id == conversation.customer_id,
                Order.status == OrderStatus.PENDING, Order.created_at >= now - timedelta(days=PROOF_ORDER_DAYS))
                .order_by(Order.created_at.desc()))).scalars().first()
            if order is None or await _alert_exists(db, tenant.id, f"vente:{order.id}"):
                continue
            customer = await db.get(Customer, order.customer_id)
            items = (await db.execute(select(OrderItem.quantity, Product.name).join(Product, Product.id == OrderItem.product_id)
                                      .where(OrderItem.order_id == order.id))).all()
            what = ", ".join(f"{q} × {short(n, 30)}" for q, n in items[:3])
            name = customer_label(customer)
            queue(db, connection,
                  f"{name} a envoyé une image pour sa commande de {amount(order.total_amount)} {order.currency}"
                  f"{f' ({what})' if what else ''} : est-ce la preuve de vente ?\nVoir : {conversation_link(conversation)}"
                  "\n\nVente confirmée ?",
                  [(f"bob:vendu:{order.id}", "Vendu"), (f"bob:annuler:{order.id}", "Annuler")], kind="ALERT",
                  ref=f"vente:{order.id}", fallback_subject=f"Vente à confirmer : {name}", now=now)
            report["sales"] += 1
        # Client qui vous attend : Bob a passé la main.
        account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalars().first()
        waiting = (await db.execute(select(Conversation).where(
            Conversation.tenant_id == tenant.id, Conversation.status == ConversationStatus.WAITING_HUMAN,
            func.coalesce(Conversation.last_message_at, Conversation.updated_at) >= since))).scalars().all()
        for conversation in waiting:
            ref = f"attente:{conversation.id}"
            if await _alert_exists(db, tenant.id, ref, now - WAITING_REPEAT):
                continue
            customer = await db.get(Customer, conversation.customer_id)
            last = (await db.execute(select(Message.content).where(
                Message.conversation_id == conversation.id, Message.sender == MessageSender.CUSTOMER)
                .order_by(Message.created_at.desc()).limit(1))).scalar_one_or_none()
            name = customer_label(customer)
            digits = re.sub(r"\D", "", customer.whatsapp_number or "") if customer else ""
            answer = (f"https://wa.me/{digits}" if account is not None and account.coexistence_mode and digits
                      else conversation_link(conversation))
            said = f" : « {short(last, 150)} »" if last else ""
            queue(db, connection, f"{name} vous attend{said}\nRépondez-lui : {answer}\nOu laissez Bob continuer.",
                  [(f"bob:continue:{conversation.id}", "Bob continue")], kind="ALERT", ref=ref,
                  fallback_subject=f"Un client vous attend : {name}", now=now)
            report["waiting"] += 1
    await db.commit()
    return report


# --- Bilan du soir, par e-mail -----------------------------------------------------------------------------------------

def summary_email(tenant, home: dict, day_label: str) -> tuple[str, str]:
    t = home["today"]
    cur = home["currency"]
    sales = f"{t['sales']} vente{'s' if t['sales'] > 1 else ''}"
    lines = [f"- {t['customers']} client{'s' if t['customers'] > 1 else ''} servi{'s' if t['customers'] > 1 else ''} par Bob",
             f"- {sales} confirmée{'s' if t['sales'] > 1 else ''} : {t['revenue']} {cur}",
             f"- {home['to_confirm']} vente{'s' if home['to_confirm'] > 1 else ''} à confirmer",
             f"- {home['waiting']} client{'s' if home['waiting'] > 1 else ''} qui vous attend{'ent' if home['waiting'] > 1 else ''}"]
    if home["out_of_stock"]:
        lines.append("- Plus en stock : " + ", ".join(home["out_of_stock"]))
    body = (f"Bonsoir,\n\nVotre journée avec Bob, {day_label} :\n" + "\n".join(lines)
            + f"\n\nOuvrir Bob : {dashboard_link()}\n\n"
            "Vous recevez ce bilan parce que l'assistant de Bob est activé. Pour l'arrêter : Bob, onglet Intégrations, "
            "carte « Naomy ».")
    return f"Votre journée avec Bob : {sales}, {t['revenue']} {cur}", body


async def send_summaries(db, now: datetime | None = None, mailer=None) -> dict:
    import asyncio

    from app.core.config import get_settings
    from app.models.naomy import NaomyConnection
    from app.models.tenant import Tenant
    from app.services.local_time import tenant_zone
    from app.services.simple_mode import home

    now = now or datetime.now(timezone.utc)
    report = {"sent": 0}
    if not get_settings().assistant_enabled:
        return report
    if mailer is None:
        from app.services.email_service import send_email as mailer
    connections = (await db.execute(select(NaomyConnection).where(
        NaomyConnection.status == "ACTIVE", NaomyConnection.assistant_enabled.is_(True),
        NaomyConnection.summary_enabled.is_(True)))).scalars().all()
    for connection in connections:
        tenant = await db.get(Tenant, connection.tenant_id)
        if tenant is None or not tenant.active or not (tenant.email or "").strip():
            continue
        local = now.astimezone(tenant_zone(tenant))
        if local.hour < SUMMARY_HOUR or connection.last_summary_on == local.date():
            continue
        data = await home(db, tenant, None, now)
        subject, body = summary_email(tenant, data, local.strftime("%d/%m/%Y"))
        connection.last_summary_on = local.date()  # une fois par jour, même si l'envoi échoue
        await db.commit()
        try:
            if await asyncio.to_thread(mailer, to=tenant.email, subject=subject, body=body):
                report["sent"] += 1
        except Exception:  # noqa: BLE001
            logger.warning("Bilan du soir non envoyé (boutique %s)", tenant.id)
    return report


async def tick(db, now: datetime | None = None, sender=None, mailer=None) -> dict:
    """Chaque minute : messages en retard, alertes, envois (avec renvois et repli par e-mail), bilans du soir."""
    from app.core.config import get_settings
    from app.models.naomy import AssistantMessage

    now = now or datetime.now(timezone.utc)
    report = {"processed": 0}
    stale = (await db.execute(select(AssistantMessage.id).where(
        AssistantMessage.status == "RECEIVED", AssistantMessage.received_at <= now - timedelta(seconds=20))
        .order_by(AssistantMessage.received_at).limit(50))).scalars().all()
    for mid in stale:
        await process(db, mid, now)
        report["processed"] += 1
    # Messages bloqués (arrêt du worker pendant le traitement) : repris après 10 minutes.
    await db.execute(update(AssistantMessage).where(AssistantMessage.status == "WORKING",
                                                    AssistantMessage.received_at <= now - timedelta(minutes=10))
                     .values(status="RECEIVED"))
    await db.commit()
    if get_settings().assistant_enabled:
        report["alerts"] = await scan_alerts(db, now)
    report["outbox"] = await send_outbox(db, now, sender=sender, mailer=mailer)
    report["summaries"] = await send_summaries(db, now, mailer=mailer)
    report["photos_removed"] = clean_store(now)  # lot 92b
    return report


def view_outbox_item(item) -> dict:  # pour les tests et le monitoring
    return {"id": item.message_id, "kind": item.kind, "ref": item.ref, "status": item.status, "texte": item.payload.get("texte"),
            "boutons": item.payload.get("boutons"), "attend_reponse": item.payload.get("attend_reponse", False),
            "sent_at": iso(item.sent_at)}
