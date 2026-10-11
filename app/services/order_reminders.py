"""
Lot 63 — boutique en ligne : Bob relance les commandes restées non payées.

- Deux relances au plus : la première 3 h après la commande, la seconde le lendemain (24 h après la
  commande, et au moins 12 h après la première). Passé 48 h sans première relance, ou 72 h sans la
  seconde, Bob ne relance plus : une vieille commande se traite à la main.
- Canal : WhatsApp si le client a écrit dans les 20 dernières heures (règle unique du lot 49), sinon
  email s'il est connu (message de service lié à SA commande : il part même sans accord pour les
  offres, toujours avec un lien de désinscription ; jamais après une désinscription), sinon rien —
  l'étape est comptée et la commande reste en attente dans « Commandes », marquée « à appeler ».
- Bob s'arrête dès que la commande est payée ou annulée, dès que le client écrit (10 minutes après la
  commande : les « merci » qui suivent la confirmation ne comptent pas), quand la conversation attend
  un humain ou que le vendeur répond depuis son téléphone (lot 51).
- Rien pour une démo, pendant une pause de Bob, hors 9 h – 20 h (heure du pays), ou si la boutique a
  désactivé le réglage (actif par défaut).
- Textes fixes (jamais rédigés par l'IA) : récapitulatif, lien de paiement, « répondez si vous avez
  une question » ; tutoiement si la boutique l'a choisi.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.order import Order, OrderStatus
from app.services import wa_templates
from app.services.address_form import uses_tu
from app.services.order_emails import _money, _ref

logger = logging.getLogger(__name__)

FIRST_AFTER = timedelta(hours=3)
FIRST_UNTIL = timedelta(hours=48)
SECOND_AFTER = timedelta(hours=24)
SECOND_UNTIL = timedelta(hours=72)
MIN_GAP = timedelta(hours=12)
QUIET_AFTER_ORDER = timedelta(minutes=10)
REMINDER_HOURS = range(9, 20)  # entre 9 h et 20 h, heure du pays
MAX_STEPS = 2

WHATSAPP, EMAIL, NONE = "WHATSAPP", "EMAIL", "NONE"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def due_step(order: Order, now: datetime) -> int | None:
    """Étape à envoyer maintenant (1 ou 2), ou None."""
    if order.status != OrderStatus.PENDING or order.created_at is None:
        return None
    age = now - _aware(order.created_at)
    sent = order.reminders_sent or 0
    if sent == 0 and FIRST_AFTER <= age < FIRST_UNTIL:
        return 1
    if sent == 1 and SECOND_AFTER <= age < SECOND_UNTIL:
        last = _aware(order.last_reminder_at)
        if last is None or now - last >= MIN_GAP:
            return 2
    return None


def _lines(items: list[str]) -> list[str]:
    return [f"- {line}" for line in items]


def whatsapp_text(tenant, customer, order: Order, items: list[str], step: int) -> str:
    tu = uses_tu(tenant)
    first = (customer.first_name or "").strip()
    hello = f"Bonjour {first}" if first else "Bonjour"
    total = _money(order.total_amount, order.currency)
    if step == 1:
        intro = (f"{hello}, ici {tenant.name}. {'Ta' if tu else 'Votre'} commande {_ref(order)} ({total}) "
                 f"est bien enregistrée et attend {'ton' if tu else 'votre'} paiement.")
    else:
        intro = (f"{hello}, ici {tenant.name}. Petit rappel : {'ta' if tu else 'votre'} commande {_ref(order)} "
                 f"({total}) est toujours en attente de paiement.")
    lines = [intro, "", *_lines(items), ""]
    if tenant.payment_link:
        lines.append(f"Payer : {tenant.payment_link}")
    lines.append("Envoie ici la capture d'écran du paiement pour recevoir ton reçu." if tu
                 else "Envoyez ici la capture d'écran du paiement pour recevoir votre reçu.")
    lines.append("Une question ? Réponds simplement à ce message." if tu
                 else "Une question ? Répondez simplement à ce message.")
    return "\n".join(lines)


def email_message(tenant, customer, order: Order, items: list[str], step: int, unsubscribe_url: str) -> tuple[str, str]:
    from app.services.email_layout import customer_footer

    tu = uses_tu(tenant)
    first = (customer.first_name or "").strip()
    total = _money(order.total_amount, order.currency)
    subject = (f"{'Ta' if tu else 'Votre'} commande {_ref(order)} chez {tenant.name} attend "
               f"{'ton' if tu else 'votre'} paiement")
    if step == 2:
        subject = "Rappel : " + subject[0].lower() + subject[1:]
    lines = [
        f"Bonjour {first}," if first else "Bonjour,",
        "",
        (f"{'Ta' if tu else 'Votre'} commande est bien enregistrée, il ne manque que le paiement." if step == 1 else
         f"{'Ta' if tu else 'Votre'} commande est toujours en attente de paiement."),
        "",
        f"Référence : {_ref(order)}",
        f"Total : {total}",
        "",
        "Articles :",
        *_lines(items),
        "",
    ]
    if tenant.payment_link:
        lines += [f"Payer ma commande : {tenant.payment_link}", ""]
    lines.append("Une fois la capture d'écran du paiement envoyée sur WhatsApp, "
                 + ("tu recevras ton reçu." if tu else "vous recevrez votre reçu."))
    lines += ["", "Une question ? " + ("Réponds simplement à cet email ou écris-nous sur WhatsApp."
                                       if tu else "Répondez simplement à cet email ou écrivez-nous sur WhatsApp."),
              "", f"À bientôt,\n{tenant.name}"]
    footer = customer_footer(tenant.name, not tenant.is_paid, [
        f"{'Tu reçois' if tu else 'Vous recevez'} cet email au sujet de {'ta' if tu else 'votre'} commande chez {tenant.name}.",
        f"Ne plus recevoir d'email : {unsubscribe_url}",
    ])
    return subject, "\n".join(lines) + footer


async def stop_reason(db, tenant, order: Order, conversation, now: datetime) -> str | None:
    """Pourquoi Bob ne relance pas (ou plus) cette commande en attente ; None = il peut relancer."""
    from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender

    if conversation is not None and (conversation.status == ConversationStatus.WAITING_HUMAN
                                     or conversation.phone_reply_at is not None):
        return "human"
    since = _aware(order.created_at) + QUIET_AFTER_ORDER
    last = (await db.execute(select(func.max(Message.created_at)).join(
        Conversation, Conversation.id == Message.conversation_id).where(
        Message.tenant_id == tenant.id, Conversation.tenant_id == tenant.id,
        Conversation.customer_id == order.customer_id, Message.sender == MessageSender.CUSTOMER,
    ))).scalar_one_or_none()
    if last is not None and _aware(last) > since:
        return "customer_wrote"
    return None


async def _conversation(db, tenant, order: Order):
    from app.models.conversation import Conversation

    if order.conversation_id is not None:
        conversation = await db.get(Conversation, order.conversation_id)
        if conversation is not None and conversation.tenant_id == tenant.id:
            return conversation
    return (await db.execute(select(Conversation).where(
        Conversation.tenant_id == tenant.id, Conversation.customer_id == order.customer_id,
    ).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()


async def _send_whatsapp(db, tenant, conversation, customer, text) -> bool:
    from app.services.appointment_service import send_fixed_message

    sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, "BOB", "order_reminder")
    return sent


def active(tenant, now: datetime) -> bool:
    from app.services.bob_pause import is_paused
    from app.services.business_type import is_online_store

    return (tenant is not None and tenant.active and not tenant.is_demo and is_online_store(tenant)
            and tenant.order_reminders_enabled and not is_paused(tenant, now))


def template_values(tenant, customer, order: Order) -> list[str]:
    """Lot 68 — modèle « commande en attente de paiement » : prénom, référence, boutique, montant."""
    return [wa_templates.first_name(customer), _ref(order), tenant.name, _money(order.total_amount, order.currency)]


async def run_for_tenant(db, tenant, now: datetime | None = None, send_email=None, send_whatsapp=None,
                         send_template=None) -> dict:
    """Relances dues d'une boutique. Une commande en échec ne bloque jamais les autres."""
    from app.models.customer import Customer
    from app.services.human_reply import customer_window_open
    from app.services.local_time import tenant_zone
    from app.services.receipt_service import get_order_item_lines

    report = {"whatsapp": 0, "email": 0, "none": 0, "failed": 0}
    now = now or datetime.now(timezone.utc)
    if not active(tenant, now) or now.astimezone(tenant_zone(tenant)).hour not in REMINDER_HOURS:
        return report
    if send_email is None:
        from app.services.email_service import send_email
    send_whatsapp = send_whatsapp or _send_whatsapp
    send_template = send_template or wa_templates.send
    orders = (await db.execute(select(Order).where(
        Order.tenant_id == tenant.id, Order.status == OrderStatus.PENDING, Order.reminders_sent < MAX_STEPS,
        Order.created_at >= now - SECOND_UNTIL, Order.created_at <= now - FIRST_AFTER,
    ).order_by(Order.created_at))).scalars().all()
    for order in orders:
        step = due_step(order, now)
        if step is None:
            continue
        try:
            customer = await db.get(Customer, order.customer_id)
            if customer is None:
                continue
            conversation = await _conversation(db, tenant, order)
            if await stop_reason(db, tenant, order, conversation, now):
                continue
            items = await get_order_item_lines(db, order.id, order.currency)
            channel = None
            if conversation is not None and await customer_window_open(db, tenant.id, customer.id, now):
                if await send_whatsapp(db, tenant, conversation, customer, whatsapp_text(tenant, customer, order, items, step)):
                    channel = WHATSAPP
            elif await send_template(db, tenant, customer, conversation, "commande_paiement",
                                     template_values(tenant, customer, order),
                                     button=await wa_templates.ensure_pay_code(db, order), message_type="order_reminder"):
                channel = WHATSAPP  # lot 68 : fenêtre fermée, modèle approuvé avec le bouton « Payer ma commande »
            if channel is None and customer.email and customer.marketing_consent_withdrawn_at is None:
                mail = await reminder_email(tenant, customer, order, items, step)
                if not send_email(**mail):
                    report["failed"] += 1
                    continue  # nouvel essai à la prochaine heure
                channel = EMAIL
                if conversation is not None:
                    from app.models.conversation import Message, MessageSender

                    db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                                   message_type="order_reminder_email",
                                   content=f"Relance de commande envoyée par email : « {mail['subject']} »"))
            channel = channel or NONE
            order.reminders_sent = step
            order.last_reminder_at = now
            order.last_reminder_channel = channel
            if channel != NONE and order.first_reminder_at is None:
                order.first_reminder_at = now
            report[channel.lower()] += 1
            await db.commit()
        except Exception:  # noqa: BLE001 — une commande en échec ne bloque jamais les autres
            await db.rollback()
            report["failed"] += 1
            logger.warning("Relance de commande impossible (commande %s)", order.id)
    return report


async def reminder_email(tenant, customer, order: Order, items: list[str], step: int) -> dict:
    from app.services.campaign_service import _unsubscribe_headers
    from app.services.unsubscribe_service import build_unsubscribe_url

    unsubscribe_url = build_unsubscribe_url(customer.id)
    subject, body = email_message(tenant, customer, order, items, step, unsubscribe_url)
    return {"to": customer.email, "subject": subject, "body": body, "from_name": tenant.name,
            "reply_to": tenant.email, "extra_headers": _unsubscribe_headers(unsubscribe_url)}


async def in_progress(db, tenant, customer_id, now: datetime | None = None) -> bool:
    """Une relance de commande est en cours pour ce client (la relance « offres » du lot 49 s'efface)."""
    now = now or datetime.now(timezone.utc)
    if not active(tenant, now):
        return False
    found = (await db.execute(select(Order.id).where(
        Order.tenant_id == tenant.id, Order.customer_id == customer_id, Order.status == OrderStatus.PENDING,
        Order.created_at >= now - SECOND_UNTIL,
    ).limit(1))).scalar_one_or_none()
    return found is not None


def summary(orders: list, start: datetime, end: datetime) -> tuple[int, int, float]:
    """Rapport mensuel : commandes relancées sur la période, et celles payées ensuite (nombre, montant)."""
    relanced = [o for o in orders if o.first_reminder_at is not None and start <= _aware(o.first_reminder_at) < end]
    paid = [o for o in relanced if o.status == OrderStatus.PAID and o.paid_at is not None
            and _aware(o.paid_at) >= _aware(o.first_reminder_at)]
    return len(relanced), len(paid), float(sum(float(o.total_amount) for o in paid))
