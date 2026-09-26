"""
Accueil du tableau de bord (lot 19) : ce que Bob a fait, et ce qui attend le commerçant.

LECTURE SEULE : tout est calculé à partir des données existantes (messages, commandes,
opportunités, étiquettes), jamais inventé, toujours limité à la boutique demandée.
- une évolution n'est donnée que si la période précédente a des données (sinon None,
  affiché « pas de comparaison ») : jamais de « +100 % » trompeur ;
- les jours sont des jours UTC.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.message_signal import MessageSignal
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.handoff_rules import RULE_LABELS
from app.services.handoff_service import TRANSFER_MESSAGE_TYPES
from app.services.opportunity_service import sales_summary

STALE_ORDER_DAYS = 7          # commande non payée à relancer (même seuil que la carte Ventes)
CALLBACK_WINDOW_HOURS = 48    # un client « à rappeler » après une panne reste visible 48 h
ACTIVITY_DAYS = 7
ACTIVITY_LIMIT = 8
DETAIL_MAX_CHARS = 120

# Raison lisible d'une attente, d'après le dernier message de transfert (règles des lots 13 à 16).
_REASONS = [
    (RULE_LABELS["MISSING_CONDITIONS"], "Question transmise par Bob", "danger"),
    (RULE_LABELS["PROMISE_KEPT"], "Bob a promis un suivi", "warning"),
    (RULE_LABELS["HUMAN_REQUEST"], "Demande à parler à quelqu'un", "danger"),
    (RULE_LABELS["REFUND"], "Demande de remboursement", "danger"),
    ("réclamation", "Réclamation", "danger"),
    ("remise", "Demande de remise", "warning"),
    (RULE_LABELS["AI_LOOP"], "Bob n'a pas su répondre", "warning"),
]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _name(customer: Customer | None) -> str:
    if customer is None:
        return "Client"
    full = " ".join(p for p in (customer.first_name, customer.last_name) if p)
    return full or (f"+{customer.whatsapp_number}" if customer.whatsapp_number else "Client")


def _short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= DETAIL_MAX_CHARS else text[: DETAIL_MAX_CHARS - 1] + "…"


def _reason(transfer: Message | None) -> tuple[str, str]:
    if transfer is None:
        return "Attend une réponse", "warning"
    if transfer.message_type == "negotiation_escalated":
        return "Négociation sans accord", "warning"
    if transfer.message_type == "takeover":
        return "Conversation reprise par l'équipe", "info"
    content = (transfer.content or "").lower()
    for needle, label, tone in _REASONS:
        if needle.lower() in content:
            return label, tone
    return "Transmis par Bob", "warning"


def _delta_pct(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


async def home_summary(db: AsyncSession, tenant_id, user_id=None, days: int = 30, now: datetime | None = None) -> dict:
    now = _aware(now or datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)

    user = await db.get(User, user_id) if user_id else None
    first_name = (user.full_name or "").split()[0] if user and user.full_name and user.tenant_id == tenant_id else None

    customers = {c.id: c for c in (await db.execute(select(Customer).where(Customer.tenant_id == tenant_id))).scalars()}
    conversations = {c.id: c for c in (await db.execute(
        select(Conversation).where(Conversation.tenant_id == tenant_id)
    )).scalars()}
    messages = (await db.execute(
        select(Message).where(Message.tenant_id == tenant_id, Message.created_at >= prev_since).order_by(Message.created_at)
    )).scalars().all()
    orders = (await db.execute(select(Order).where(Order.tenant_id == tenant_id))).scalars().all()

    # --- Conversations actives par période (au moins un message du client) ------------------
    def active_conversations(start, end):
        return {m.conversation_id for m in messages
                if m.sender == MessageSender.CUSTOMER and start <= _aware(m.created_at) < end}

    current_convs = active_conversations(since, now + timedelta(seconds=1))
    previous_convs = active_conversations(prev_since, since)
    transferred_now = {m.conversation_id for m in messages
                       if m.message_type in TRANSFER_MESSAGE_TYPES and _aware(m.created_at) >= since}
    ai_messages = sum(1 for m in messages if m.sender == MessageSender.AI and _aware(m.created_at) >= since)

    # --- Trésorerie (même règle que la carte Ventes : date de paiement, à défaut de création) -
    def paid_moment(o):
        return _aware(o.paid_at) or _aware(o.created_at)

    def cash(start, end):
        paid = sum((Decimal(str(o.total_amount)) for o in orders
                    if o.status == OrderStatus.PAID and start <= paid_moment(o) < end), Decimal("0"))
        return paid

    paid_now = cash(since, now + timedelta(seconds=1))
    paid_prev = cash(prev_since, since)
    pending = [o for o in orders if o.status == OrderStatus.PENDING and _aware(o.created_at) >= since]
    pending_amount = sum((Decimal(str(o.total_amount)) for o in pending), Decimal("0"))

    sales = await sales_summary(db, tenant_id, days=days, now=now)
    prev_sales = await _conversion_between(db, tenant_id, prev_since, since)
    conversion = sales["conversion_rate_pct"]
    conversion_delta = (round(conversion - prev_sales, 1)
                        if conversion is not None and prev_sales is not None else None)

    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, tenant_id)
    currency = tenant.currency if tenant else ""

    kpis = {
        "paid": {"value": float(paid_now), "delta_pct": _delta_pct(float(paid_now), float(paid_prev))},
        "pending": {"value": float(pending_amount), "count": len(pending)},
        "conversion": {"value": conversion, "delta_points": conversion_delta,
                       "paid": sales["paid"], "terminated": sales["terminated"]},
        "conversations": {"value": len(current_convs), "delta_pct": _delta_pct(len(current_convs), len(previous_convs)),
                          "bob_only": len(current_convs - transferred_now)},
    }

    # --- À traiter maintenant --------------------------------------------------------------
    todo = []
    last_transfer: dict = {}
    last_customer_text: dict = {}
    last_human: dict = {}
    for m in messages:  # triés par date : le dernier écrase le précédent
        if m.message_type in TRANSFER_MESSAGE_TYPES:
            last_transfer[m.conversation_id] = m
        if m.sender == MessageSender.CUSTOMER:
            last_customer_text[m.conversation_id] = m.content
        if m.sender == MessageSender.HUMAN:
            last_human[m.conversation_id] = _aware(m.created_at)

    for conv in conversations.values():
        if conv.status != ConversationStatus.WAITING_HUMAN:
            continue
        transfer = last_transfer.get(conv.id)
        label, tone = _reason(transfer)
        waiting_since = _aware(transfer.created_at) if transfer else _aware(conv.updated_at)
        todo.append({
            "kind": "CONVERSATION", "conversation_id": str(conv.id), "customer": _name(customers.get(conv.customer_id)),
            "reason": label, "tone": tone, "detail": _short(last_customer_text.get(conv.id)),
            "since": waiting_since, "action": "Répondre",
        })

    callback_label = RULE_LABELS["AI_OUTAGE_CALLBACK"]
    seen_callbacks = set()
    for m in reversed(messages):
        if (m.message_type != "ai_outage" or callback_label not in (m.content or "")
                or m.conversation_id in seen_callbacks or _aware(m.created_at) < now - timedelta(hours=CALLBACK_WINDOW_HOURS)):
            continue
        seen_callbacks.add(m.conversation_id)
        conv = conversations.get(m.conversation_id)
        if conv is None or conv.status == ConversationStatus.WAITING_HUMAN:
            continue  # déjà listée ci-dessus
        if (last_human.get(m.conversation_id) or datetime.min.replace(tzinfo=timezone.utc)) > _aware(m.created_at):
            continue  # un humain a déjà répondu depuis
        customer = customers.get(conv.customer_id)
        todo.append({
            "kind": "CALLBACK", "conversation_id": str(conv.id), "customer": _name(customer),
            "reason": "À rappeler", "tone": "info",
            "detail": f"Bob était en panne : rappel promis au +{customer.whatsapp_number}" if customer else "Rappel promis",
            "since": _aware(m.created_at), "action": "Voir",
        })

    for o in orders:
        if o.status == OrderStatus.PENDING and _aware(o.created_at) <= now - timedelta(days=STALE_ORDER_DAYS):
            days_waiting = (now - _aware(o.created_at)).days
            todo.append({
                "kind": "ORDER", "order_id": str(o.id), "customer": _name(customers.get(o.customer_id)),
                "reason": f"Non payée depuis {days_waiting} jours", "tone": "warning",
                "detail": f"Commande de {float(o.total_amount):,.0f} {o.currency}".replace(",", " "),
                "since": _aware(o.created_at), "action": "Voir la commande",
            })
    order_of_kind = {"CONVERSATION": 0, "CALLBACK": 1, "ORDER": 2}
    todo.sort(key=lambda t: (order_of_kind[t["kind"]], t["since"]))

    # --- Entonnoir ---------------------------------------------------------------------------
    funnel = [
        {"key": "conversations", "label": "Conversations", "value": len(current_convs)},
        {"key": "opportunities", "label": "Opportunités de vente", "value": sales["total"]},
        {"key": "orders", "label": "Commandes", "value": sales["with_order"]},
        {"key": "paid", "label": "Payées", "value": sales["paid"]},
    ]

    # --- Par jour --------------------------------------------------------------------------
    daily = []
    start_day = (now - timedelta(days=days - 1)).date()
    per_day: dict = {}
    for m in messages:
        if m.sender == MessageSender.CUSTOMER and _aware(m.created_at) >= since:
            per_day.setdefault(_aware(m.created_at).date(), set()).add(m.conversation_id)
    paid_days = {paid_moment(o).date() for o in orders if o.status == OrderStatus.PAID and paid_moment(o) >= since}
    for i in range(days):
        day = start_day + timedelta(days=i)
        daily.append({"date": day.isoformat(), "conversations": len(per_day.get(day, ())), "sale": day in paid_days})

    # --- Activité récente ------------------------------------------------------------------
    activity = await _activity(db, tenant_id, now, customers, messages, orders)

    return {
        "first_name": first_name,
        "period_days": days,
        "currency": currency,
        "generated_at": now,
        "ai_messages": ai_messages,
        "waiting_humans": sum(1 for c in conversations.values() if c.status == ConversationStatus.WAITING_HUMAN),
        "kpis": kpis,
        "todo": todo,
        "funnel": funnel,
        "daily": daily,
        "activity": activity,
    }


async def _conversion_between(db: AsyncSession, tenant_id, start: datetime, end: datetime) -> float | None:
    """Taux de conversion des opportunités commencées dans [start, end) — None sans opportunité terminée."""
    from app.models.sales_opportunity import OpportunityOutcome, SalesOpportunity

    rows = (await db.execute(select(SalesOpportunity).where(
        SalesOpportunity.tenant_id == tenant_id, SalesOpportunity.started_at >= start, SalesOpportunity.started_at < end
    ))).scalars().all()
    terminated = [r for r in rows if r.outcome in OpportunityOutcome.TERMINATED]
    if not terminated:
        return None
    return round(sum(1 for r in terminated if r.outcome == OpportunityOutcome.PAID) / len(terminated) * 100, 1)


async def _activity(db, tenant_id, now, customers, messages, orders) -> list[dict]:
    from app.agents.strategies import STRATEGIES_BY_CODE
    from app.agents.taxonomy import objection_label

    since = now - timedelta(days=ACTIVITY_DAYS)
    events = []
    money = lambda o: f"{float(o.total_amount):,.0f} {o.currency}".replace(",", " ")  # noqa: E731

    for o in orders:
        name = _name(customers.get(o.customer_id))
        if o.status == OrderStatus.PAID and o.paid_at and _aware(o.paid_at) >= since:
            events.append({"kind": "PAYMENT", "title": "Paiement reçu", "detail": f"{name} · {money(o)}", "at": _aware(o.paid_at)})
        if _aware(o.created_at) >= since:
            title = "Commande créée par Bob" if o.created_by == "IA" else "Commande saisie"
            events.append({"kind": "ORDER", "title": title, "detail": f"{name} · {money(o)}", "at": _aware(o.created_at)})

    contact_points = dict((await db.execute(
        select(ContactPoint.id, ContactPoint.name).where(ContactPoint.tenant_id == tenant_id)
    )).all())
    for c in customers.values():
        if c.created_at and _aware(c.created_at) >= since:
            if c.acquisition_source == "LINK" and c.acquisition_contact_point_id in contact_points:
                how = f"Arrivé par le lien « {contact_points[c.acquisition_contact_point_id]} »"
            elif c.acquisition_source == "QR":
                how = "Arrivé en scannant un QR code"
            else:
                how = "Premier message"
            events.append({"kind": "CUSTOMER", "title": "Nouveau client", "detail": f"{_name(c)} · {how}", "at": _aware(c.created_at)})

    for m in messages:
        if m.message_type in TRANSFER_MESSAGE_TYPES and _aware(m.created_at) >= since and m.message_type != "takeover":
            label, _ = _reason(m)
            events.append({"kind": "TRANSFER", "title": "Transmis à un humain", "detail": label, "at": _aware(m.created_at)})

    signals = (await db.execute(select(MessageSignal).where(
        MessageSignal.tenant_id == tenant_id, MessageSignal.message_created_at >= since
    ))).scalars().all()
    for s in signals:
        for code in s.objections or []:
            strategy = STRATEGIES_BY_CODE.get(s.strategy) if s.strategy else None
            detail = f"Réponse de Bob : {strategy.label}" if strategy else "Détectée par Bob"
            events.append({"kind": "OBJECTION", "title": f"Objection : {objection_label(code).lower()}",
                           "detail": detail, "at": _aware(s.message_created_at)})
            break  # une ligne par message

    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:ACTIVITY_LIMIT]
