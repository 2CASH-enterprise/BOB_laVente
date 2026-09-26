"""
Phase 0 de l'apprentissage — mesurer les issues.

Découpe chaque conversation en OPPORTUNITÉS (épisodes d'achat) et calcule l'issue de
chacune, uniquement à partir de faits enregistrés (messages, commandes, confirmations de
paiement). Aucun effet sur le comportement de Bob : ce module observe, il n'agit pas.

Règles :
- une nouvelle opportunité commence quand le client écrit après au moins
  OPPORTUNITY_GAP de silence (de sa part) ;
- chaque événement (commande, transfert, relance, message) appartient à l'opportunité
  dont la fenêtre [début, début de la suivante[ le contient ;
- l'issue suit un ordre de priorité fixe : payée > commande non payée > annulée >
  transférée > abandonnée > en cours.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.sales_opportunity import OpportunityOutcome, SalesOpportunity

OPPORTUNITY_GAP = timedelta(days=7)

# Transferts faits PAR BOB (une prise de contrôle manuelle n'est pas un « transfert » :
# c'est le commerçant qui a choisi d'intervenir).
BOB_TRANSFER_TYPES = frozenset({"handoff", "negotiation_escalated"})

_ID_NAMESPACE = uuid.UUID("5f0b0b1e-0b0b-4b0b-8b0b-0b0b0b0b0b0b")


def _aware(dt: datetime) -> datetime:
    """SQLite (tests) renvoie parfois des dates naïves ; PostgreSQL, jamais."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def opportunity_id(conversation_id: uuid.UUID, started_at: datetime) -> uuid.UUID:
    """Identifiant déterministe : un recalcul redonne toujours le même identifiant."""
    return uuid.uuid5(_ID_NAMESPACE, f"{conversation_id}:{_aware(started_at).isoformat()}")


@dataclass
class _Episode:
    started_at: datetime
    last_customer_message_at: datetime
    last_activity_at: datetime
    customer_message_count: int = 0
    message_count: int = 0
    followup_sent: bool = False
    transfer_reason: str | None = None
    transferred: bool = False
    orders: list = field(default_factory=list)


def split_into_episodes(customer_message_times: list[datetime]) -> list[datetime]:
    """Renvoie les dates de début d'épisode (liste triée des messages client en entrée)."""
    starts: list[datetime] = []
    previous: datetime | None = None
    for moment in customer_message_times:
        if previous is None or moment - previous >= OPPORTUNITY_GAP:
            starts.append(moment)
        previous = moment
    return starts


def _episode_index(starts: list[datetime], moment: datetime) -> int:
    """Index de l'épisode dont la fenêtre contient `moment` (avant le 1er début → 1er épisode)."""
    index = 0
    for i, start in enumerate(starts):
        if moment >= start:
            index = i
        else:
            break
    return index


def episode_for_manual_order(episodes: list, order_time: datetime):
    """
    Commande saisie à la main (sans conversation) : rattachée à l'épisode du client dont
    le dernier message date de MOINS de OPPORTUNITY_GAP avant la commande, jamais à un
    épisode ancien (ce qui le ferait passer rétroactivement en « payée »).
    `episodes` : objets ayant started_at et last_customer_message_at (épisodes de calcul ou
    lignes SalesOpportunity). Renvoie l'épisode le plus récent qui convient, ou None.
    """
    best = None
    for episode in episodes:
        start = _aware(episode.started_at)
        last = _aware(episode.last_customer_message_at)
        if start <= order_time < last + OPPORTUNITY_GAP:
            if best is None or start > _aware(best.started_at):
                best = episode
    return best


def compute_outcome(episode: _Episode, now: datetime) -> str:
    statuses = {order.status for order in episode.orders}
    if OrderStatus.PAID in statuses:
        return OpportunityOutcome.PAID
    if OrderStatus.PENDING in statuses:
        return OpportunityOutcome.ORDER_UNPAID
    if OrderStatus.CANCELLED in statuses:
        return OpportunityOutcome.CANCELLED
    if episode.transferred:
        return OpportunityOutcome.TRANSFERRED
    if now - episode.last_customer_message_at >= OPPORTUNITY_GAP:
        return OpportunityOutcome.ABANDONED
    return OpportunityOutcome.IN_PROGRESS


def _transfer_reason(message: Message) -> str:
    text = message.content or ""
    if message.message_type == "handoff" and " : " in text:
        text = text.split(" : ", 1)[1]  # « Transfert vers un humain : <motif> »
    return text[:300]


async def recompute_tenant_opportunities(db: AsyncSession, tenant_id, now: datetime | None = None) -> int:
    """
    Recalcule entièrement les opportunités d'un tenant (idempotent : mêmes données en
    entrée → mêmes lignes, mêmes identifiants). Renvoie le nombre d'opportunités.
    """
    now = _aware(now or datetime.now(timezone.utc))

    conversations = (await db.execute(
        select(Conversation).where(Conversation.tenant_id == tenant_id)
    )).scalars().all()
    customers = {c.id: c for c in (await db.execute(
        select(Customer).where(Customer.tenant_id == tenant_id)
    )).scalars().all()}
    messages = (await db.execute(
        select(Message).where(Message.tenant_id == tenant_id).order_by(Message.created_at)
    )).scalars().all()
    orders = (await db.execute(
        select(Order).where(Order.tenant_id == tenant_id).order_by(Order.created_at)
    )).scalars().all()

    messages_by_conversation: dict = {}
    for message in messages:
        messages_by_conversation.setdefault(message.conversation_id, []).append(message)
    orders_by_conversation: dict = {}
    for order in orders:
        if order.conversation_id is not None:
            orders_by_conversation.setdefault(order.conversation_id, []).append(order)

    # Dates de première commande payée par client : « client ayant déjà payé » au début d'un épisode.
    paid_order_dates: dict = {}
    for order in orders:
        if order.status == OrderStatus.PAID:
            paid_order_dates.setdefault(order.customer_id, []).append(_aware(order.created_at))

    built: list[tuple] = []  # (conversation, customer, episodes)
    episodes_by_customer: dict = {}
    for conversation in conversations:
        conv_messages = messages_by_conversation.get(conversation.id, [])
        customer_times = [_aware(m.created_at) for m in conv_messages if m.sender == MessageSender.CUSTOMER]
        if not customer_times:
            continue  # aucune opportunité sans message du client

        starts = split_into_episodes(customer_times)
        episodes = [_Episode(started_at=s, last_customer_message_at=s, last_activity_at=s) for s in starts]

        for message in conv_messages:
            moment = _aware(message.created_at)
            episode = episodes[_episode_index(starts, moment)]
            episode.last_activity_at = max(episode.last_activity_at, moment)
            if message.sender == MessageSender.CUSTOMER:
                episode.customer_message_count += 1
                episode.last_customer_message_at = max(episode.last_customer_message_at, moment)
            if message.sender != MessageSender.SYSTEM:
                episode.message_count += 1
            if message.message_type == "followup":
                episode.followup_sent = True
            if message.sender == MessageSender.SYSTEM and message.message_type in BOB_TRANSFER_TYPES:
                episode.transferred = True
                episode.transfer_reason = _transfer_reason(message)

        for order in orders_by_conversation.get(conversation.id, []):
            episodes[_episode_index(starts, _aware(order.created_at))].orders.append(order)

        built.append((conversation, customers.get(conversation.customer_id), episodes))
        episodes_by_customer.setdefault(conversation.customer_id, []).extend(episodes)

    # Commandes saisies à la main (sans conversation) : rattachées si le client écrivait encore
    # à ce moment-là ; sinon elles restent « hors conversation » (comptées dans le résumé).
    for order in orders:
        if order.conversation_id is None:
            episode = episode_for_manual_order(episodes_by_customer.get(order.customer_id, []), _aware(order.created_at))
            if episode is not None:
                episode.orders.append(order)

    rows: list[SalesOpportunity] = []
    for conversation, customer, episodes in built:
        rows.extend(_build_rows(tenant_id, conversation, customer, episodes, paid_order_dates, now))

    # « Première opportunité du client » : sur l'ensemble de ses conversations (un client peut
    # en avoir eu plusieurs si une conversation a été clôturée).
    first_start_by_customer: dict = {}
    for row in rows:
        current = first_start_by_customer.get(row.customer_id)
        if current is None or row.started_at < current:
            first_start_by_customer[row.customer_id] = row.started_at
    for row in rows:
        row.is_first_opportunity = row.started_at == first_start_by_customer[row.customer_id]
        if not row.is_first_opportunity:
            row.source = None
            row.contact_point_id = None

    await db.execute(delete(SalesOpportunity).where(SalesOpportunity.tenant_id == tenant_id))
    db.add_all(rows)
    await db.commit()
    return len(rows)


def _build_rows(tenant_id, conversation, customer, episodes, paid_order_dates, now) -> list[SalesOpportunity]:
    rows = []
    for episode in episodes:
        active_orders = [o for o in episode.orders if o.status != OrderStatus.CANCELLED]
        paid_orders = [o for o in episode.orders if o.status == OrderStatus.PAID]
        previous_paid = [d for d in paid_order_dates.get(conversation.customer_id, []) if d < episode.started_at]
        rows.append(SalesOpportunity(
            id=opportunity_id(conversation.id, episode.started_at),
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            customer_id=conversation.customer_id,
            started_at=episode.started_at,
            last_customer_message_at=episode.last_customer_message_at,
            last_activity_at=episode.last_activity_at,
            outcome=compute_outcome(episode, now),
            is_first_opportunity=True,  # ajusté ensuite sur l'ensemble des conversations du client
            source=customer.acquisition_source if customer else None,
            contact_point_id=customer.acquisition_contact_point_id if customer else None,
            is_returning_buyer=bool(previous_paid),
            city=customer.city if customer else None,
            followup_sent=episode.followup_sent,
            customer_message_count=episode.customer_message_count,
            message_count=episode.message_count,
            order_count=len(episode.orders),
            order_amount=sum((Decimal(str(o.total_amount)) for o in active_orders), Decimal("0")),
            paid_amount=sum((Decimal(str(o.total_amount)) for o in paid_orders), Decimal("0")),
            transfer_reason=episode.transfer_reason,
            computed_at=now,
        ))
    return rows


# ---------------------------------------------------------------------------
# Résumé pour le dashboard — chiffres CALCULÉS ici, jamais par un LLM.
# ---------------------------------------------------------------------------

SOURCE_LABELS = {"QR": "QR code", "LINK": "Lien / widget", "IMPORT": "Import", "ORGANIC": "Direct"}
RETURNING_LABEL = "Clients déjà venus"
DIRECT_LABEL = "Direct"


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


async def sales_summary(db: AsyncSession, tenant_id, days: int = 30, now: datetime | None = None) -> dict:
    from app.models.contact_point import ContactPoint
    from app.models.tenant import Tenant

    now = _aware(now or datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    rows = (await db.execute(
        select(SalesOpportunity).where(SalesOpportunity.tenant_id == tenant_id, SalesOpportunity.started_at >= since)
    )).scalars().all()
    any_row = (await db.execute(
        select(SalesOpportunity.computed_at).where(SalesOpportunity.tenant_id == tenant_id).limit(1)
    )).scalar_one_or_none()
    tenant = await db.get(Tenant, tenant_id)
    contact_point_names = dict((await db.execute(
        select(ContactPoint.id, ContactPoint.name).where(ContactPoint.tenant_id == tenant_id)
    )).all())

    outcomes = {key: 0 for key in (
        OpportunityOutcome.PAID, OpportunityOutcome.ORDER_UNPAID, OpportunityOutcome.CANCELLED,
        OpportunityOutcome.TRANSFERRED, OpportunityOutcome.ABANDONED, OpportunityOutcome.IN_PROGRESS,
    )}
    by_source: dict[str, dict] = {}
    for row in rows:
        outcomes[row.outcome] += 1

        if not row.is_first_opportunity:
            label = RETURNING_LABEL
        elif row.source == "LINK" and row.contact_point_id in contact_point_names:
            label = f"Lien : {contact_point_names[row.contact_point_id]}"
        else:
            label = SOURCE_LABELS.get(row.source, DIRECT_LABEL) if row.source else DIRECT_LABEL
        bucket = by_source.setdefault(label, {"label": label, "opportunities": 0, "terminated": 0, "paid": 0})
        bucket["opportunities"] += 1
        bucket["terminated"] += row.outcome in OpportunityOutcome.TERMINATED
        bucket["paid"] += row.outcome == OpportunityOutcome.PAID

    cash = await _cash_summary(db, tenant_id, since)

    terminated = sum(outcomes[o] for o in OpportunityOutcome.TERMINATED)
    with_order = outcomes[OpportunityOutcome.PAID] + outcomes[OpportunityOutcome.ORDER_UNPAID] + outcomes[OpportunityOutcome.CANCELLED]
    sources = sorted(by_source.values(), key=lambda b: (-b["opportunities"], b["label"]))
    for bucket in sources:
        bucket["conversion_rate_pct"] = _rate(bucket["paid"], bucket["terminated"])

    return {
        "period_days": days,
        "computed_at": any_row,
        "currency": tenant.currency if tenant else None,
        "total": len(rows),
        "outcomes": outcomes,
        "with_order": with_order,
        "paid": outcomes[OpportunityOutcome.PAID],
        "terminated": terminated,
        "conversion_rate_pct": _rate(outcomes[OpportunityOutcome.PAID], terminated),
        **cash,
        "by_source": sources,
    }


async def _cash_summary(db: AsyncSession, tenant_id, since: datetime) -> dict:
    """
    Trésorerie de la période, sur TOUTES les commandes (conversations et saisies à la main) :
    - encaissé : commandes payées dont le paiement date de la période (à défaut de date de
      paiement, pour les commandes anciennes, la date de création) ;
    - en attente : commandes non payées créées pendant la période ;
    - dont en attente dans des opportunités déjà payées (pour lire la carte sans contresens) ;
    - ventes hors conversation : commandes manuelles qu'aucune opportunité ne peut porter.
    """
    orders = (await db.execute(select(Order).where(Order.tenant_id == tenant_id))).scalars().all()
    opportunities = (await db.execute(
        select(SalesOpportunity).where(SalesOpportunity.tenant_id == tenant_id)
    )).scalars().all()
    by_conversation: dict = {}
    by_customer: dict = {}
    for opp in opportunities:
        by_conversation.setdefault(opp.conversation_id, []).append(opp)
        by_customer.setdefault(opp.customer_id, []).append(opp)

    def opportunity_of(order):
        moment = _aware(order.created_at)
        if order.conversation_id is not None:
            candidates = sorted(by_conversation.get(order.conversation_id, []), key=lambda o: _aware(o.started_at))
            chosen = None
            for opp in candidates:  # même règle que le calcul : le dernier épisode commencé avant la commande
                if _aware(opp.started_at) <= moment:
                    chosen = opp
            return chosen or (candidates[0] if candidates else None)
        return episode_for_manual_order(by_customer.get(order.customer_id, []), moment)

    paid = Decimal("0")
    pending = Decimal("0")
    pending_in_paid = Decimal("0")
    outside_count = 0
    outside_paid = Decimal("0")
    for order in orders:
        amount = Decimal(str(order.total_amount))
        created = _aware(order.created_at)
        paid_moment = _aware(order.paid_at) if order.paid_at else created
        opportunity = opportunity_of(order)

        in_period = False
        if order.status == OrderStatus.PAID and paid_moment >= since:
            paid += amount
            in_period = True
        elif order.status == OrderStatus.PENDING and created >= since:
            pending += amount
            in_period = True
            if opportunity is not None and opportunity.outcome == OpportunityOutcome.PAID:
                pending_in_paid += amount

        if in_period and order.conversation_id is None and opportunity is None:
            outside_count += 1
            if order.status == OrderStatus.PAID:
                outside_paid += amount

    return {
        "revenue_paid": float(paid),
        "revenue_pending": float(pending),
        "revenue_pending_in_paid_opportunities": float(pending_in_paid),
        "outside_conversation_orders": outside_count,
        "outside_conversation_paid": float(outside_paid),
    }
