"""
Phase 1 (lot 12) : classification des messages clients et lecture des étiquettes.
Aucun effet sur les réponses de Bob dans ce lot : on observe, on mesure, on montre.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.classifier import MessageClassifier
from app.agents.taxonomy import TAXONOMY_VERSION
from app.core.config import get_settings
from app.models.conversation import Message, MessageSender
from app.models.message_signal import MessageSignal

logger = logging.getLogger(__name__)


async def classify_and_store(db: AsyncSession, classifier: MessageClassifier | None, message: Message) -> MessageSignal | None:
    """Classe un message client déjà enregistré. Ne lève jamais : en cas d'échec, rien n'est stocké."""
    if classifier is None or message.sender != MessageSender.CUSTOMER or message.message_type != "text":
        return None
    try:
        previous_stmt = (
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.sender.in_([MessageSender.AI, MessageSender.HUMAN]),
                Message.id != message.id,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        previous = (await db.execute(previous_stmt)).scalar_one_or_none()
        result = await classifier.classify(
            message.content, previous_shop_message=previous, timeout=get_settings().classifier_timeout_seconds
        )
        if result is None:
            return None
    except Exception:  # noqa: BLE001
        logger.exception("Échec de la classification du message %s", message.id)
        return None

    try:
        signal = MessageSignal(
            tenant_id=message.tenant_id,
            conversation_id=message.conversation_id,
            message_id=message.id,
            intents=result["intents"],
            objections=result["objections"],
            offered_amount=result["offered_amount"],
            model=classifier.model_name,
            taxonomy_version=TAXONOMY_VERSION,
            message_created_at=message.created_at or datetime.now(timezone.utc),
        )
        # Point de sauvegarde : en cas d'échec, SEULE l'écriture des étiquettes est annulée. Un
        # rollback complet invaliderait la conversation en mémoire et empêcherait Bob de répondre.
        async with db.begin_nested():
            db.add(signal)
        await db.commit()
        return signal
    except Exception:  # noqa: BLE001 — l'observation ne doit jamais perturber la conversation
        logger.exception("Échec de l'enregistrement de la classification du message %s", message.id)
        return None


# ---------------------------------------------------------------------------
# Lecture — chiffres CALCULÉS ici, jamais par un LLM.
# ---------------------------------------------------------------------------

def signal_to_dict(signal: MessageSignal) -> dict:
    from app.agents.taxonomy import intent_label, objection_label
    from app.services.handoff_rules import RULE_LABELS

    return {
        "intents": [{"code": c, "label": intent_label(c)} for c in signal.intents],
        "objections": [{"code": c, "label": objection_label(c)} for c in signal.objections],
        "offered_amount": float(signal.offered_amount) if signal.offered_amount is not None else None,
        "applied_rule": RULE_LABELS.get(signal.applied_rule) if signal.applied_rule else None,
        "handoff_blocked": bool(signal.handoff_blocked),
    }


async def signals_by_message(db: AsyncSession, tenant_id, message_ids: list) -> dict:
    if not message_ids:
        return {}
    rows = (await db.execute(
        select(MessageSignal).where(MessageSignal.tenant_id == tenant_id, MessageSignal.message_id.in_(message_ids))
    )).scalars().all()
    return {row.message_id: signal_to_dict(row) for row in rows}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def signals_summary(db: AsyncSession, tenant_id, days: int = 30, now: datetime | None = None) -> dict:
    """
    Intentions et objections de la période. Pour chaque objection : dans combien
    d'opportunités elle est apparue, et combien de ces opportunités ont été payées
    (rattachement message → opportunité : même conversation, dernier épisode commencé avant).
    """
    from datetime import timedelta

    from app.agents.taxonomy import INTENTS, OBJECTIONS, intent_label, objection_label
    from app.models.sales_opportunity import OpportunityOutcome, SalesOpportunity

    now = _aware(now or datetime.now(timezone.utc))
    since = now - timedelta(days=days)

    signals = (await db.execute(
        select(MessageSignal).where(MessageSignal.tenant_id == tenant_id, MessageSignal.message_created_at >= since)
    )).scalars().all()
    customer_text_messages = (await db.execute(
        select(Message.id).where(
            Message.tenant_id == tenant_id, Message.sender == MessageSender.CUSTOMER,
            Message.message_type == "text", Message.created_at >= since,
        )
    )).scalars().all()
    opportunities = (await db.execute(
        select(SalesOpportunity).where(SalesOpportunity.tenant_id == tenant_id)
    )).scalars().all()

    by_conversation: dict = {}
    for opp in opportunities:
        by_conversation.setdefault(opp.conversation_id, []).append(opp)
    for items in by_conversation.values():
        items.sort(key=lambda o: _aware(o.started_at))

    def opportunity_of(signal):
        candidates = by_conversation.get(signal.conversation_id, [])
        chosen = None
        for opp in candidates:
            if _aware(opp.started_at) <= _aware(signal.message_created_at):
                chosen = opp
        return chosen or (candidates[0] if candidates else None)

    intent_messages: dict = {}
    intent_opps: dict = {}
    objection_messages: dict = {}
    objection_opps: dict = {}
    for signal in signals:
        opp = opportunity_of(signal)
        for code in signal.intents:
            if code in INTENTS:
                intent_messages[code] = intent_messages.get(code, 0) + 1
                if opp is not None:
                    intent_opps.setdefault(code, {})[opp.id] = opp
        for code in signal.objections:
            if code in OBJECTIONS:
                objection_messages[code] = objection_messages.get(code, 0) + 1
                if opp is not None:
                    objection_opps.setdefault(code, {})[opp.id] = opp

    intents = sorted(
        (
            {"code": code, "label": intent_label(code), "messages": count, "opportunities": len(intent_opps.get(code, {}))}
            for code, count in intent_messages.items()
        ),
        key=lambda item: (-item["messages"], item["label"]),
    )

    objections = []
    for code, count in objection_messages.items():
        opps = list(objection_opps.get(code, {}).values())
        terminated = [o for o in opps if o.outcome in OpportunityOutcome.TERMINATED]
        paid = [o for o in opps if o.outcome == OpportunityOutcome.PAID]
        objections.append({
            "code": code,
            "label": objection_label(code),
            "messages": count,
            "opportunities": len(opps),
            "terminated": len(terminated),
            "paid": len(paid),
            "conversion_rate_pct": round(len(paid) / len(terminated) * 100, 1) if terminated else None,
        })
    objections.sort(key=lambda item: (-item["opportunities"], -item["messages"], item["label"]))

    return {
        "period_days": days,
        "classified_messages": len(signals),
        "customer_messages": len(customer_text_messages),
        "intents": intents,
        "objections": objections,
    }
