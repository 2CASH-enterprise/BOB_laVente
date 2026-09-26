"""
Phase 2 (lot 15) : choix et mesure des stratégies de réponse aux objections.
"""
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.strategies import (
    MIN_TERMINATED_FOR_RATE,
    NO_KNOWLEDGE_INSTRUCTION,
    is_available,
    STRATEGIES,
    STRATEGIES_BY_CODE,
    select_strategy,
    strategies_for,
    strategy_instruction,
)
from app.models.strategy_settings import TenantStrategySettings


async def disabled_strategies(db: AsyncSession, tenant_id) -> set[str]:
    row = (await db.execute(
        select(TenantStrategySettings).where(TenantStrategySettings.tenant_id == tenant_id)
    )).scalar_one_or_none()
    return set(row.disabled_strategies or []) if row else set()


async def knowledge_categories(db: AsyncSession, tenant_id) -> set[str]:
    """Catégories renseignées (entrées actives) dans la base de connaissances du commerce."""
    from app.models.knowledge_entry import KnowledgeEntry

    rows = (await db.execute(
        select(KnowledgeEntry.category).where(KnowledgeEntry.tenant_id == tenant_id, KnowledgeEntry.active.is_(True))
    )).scalars().all()
    return {getattr(c, "value", c) for c in rows}


async def apply_strategy(db: AsyncSession, tenant_id, turn, signal: dict | None, rng: random.Random | None = None):
    """
    Ajoute à la consigne du message la stratégie tirée pour l'objection prioritaire.
    Les règles du lot 13 passent TOUJOURS en premier : dès qu'une règle s'applique (transfert,
    remise, réclamation, politesse), aucune stratégie ne s'y ajoute — jamais deux consignes
    contradictoires. Renvoie la stratégie appliquée, ou None.
    """
    from app.services.handoff_rules import TRANSFER_NOW

    if not signal or not signal.get("objections") or turn.mode == TRANSFER_NOW or turn.rule is not None:
        return None
    strategy, blocked_objection = select_strategy(
        signal["objections"], await disabled_strategies(db, tenant_id), rng,
        knowledge_categories=await knowledge_categories(db, tenant_id),
    )
    if strategy is not None:
        text = strategy_instruction(strategy)
    elif blocked_objection is not None:
        text = NO_KNOWLEDGE_INSTRUCTION
    else:
        return None
    turn.instruction = f"{turn.instruction}\n{text}" if turn.instruction else text
    return strategy


def library_with_state(disabled: set[str], known: set[str] | None = None) -> list[dict]:
    """Bibliothèque groupée par objection, pour l'écran de réglages."""
    from app.agents.strategies import OBJECTION_PRIORITY
    from app.agents.taxonomy import objection_label

    groups = []
    for objection in OBJECTION_PRIORITY:
        items = strategies_for(objection)
        if items:
            groups.append({
                "objection": objection,
                "label": objection_label(objection),
                "strategies": [
                    {
                        "code": s.code, "label": s.label, "description": s.description,
                        "enabled": s.code not in disabled,
                        # Indisponible tant que la base de connaissances ne contient pas l'information requise.
                        "available": known is None or is_available(s, known),
                        "requires": sorted(s.requires),
                    }
                    for s in items
                ],
            })
    return groups


def validate_disabled(disabled: list[str]) -> str | None:
    """Message d'erreur, ou None si la liste est acceptable."""
    unknown = [code for code in disabled if code not in STRATEGIES_BY_CODE]
    if unknown:
        return f"Stratégies inconnues : {', '.join(sorted(unknown))}"
    for objection in {s.objection for s in STRATEGIES}:
        if all(s.code in disabled for s in strategies_for(objection)):
            from app.agents.taxonomy import objection_label

            return f"Gardez au moins une stratégie active pour l'objection « {objection_label(objection)} »."
    return None


async def strategies_summary(db: AsyncSession, tenant_id, days: int = 30, now=None) -> dict:
    """
    Pour chaque stratégie utilisée : opportunités concernées, terminées, payées. Le taux n'est
    affiché qu'à partir de MIN_TERMINATED_FOR_RATE opportunités terminées ; en dessous, un
    pourcentage serait trompeur (3 ventes sur 5 ne font pas « 60 % »).
    """
    from datetime import datetime, timedelta, timezone

    from app.agents.taxonomy import objection_label
    from app.models.message_signal import MessageSignal
    from app.models.sales_opportunity import OpportunityOutcome, SalesOpportunity

    def aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    now = aware(now or datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    signals = (await db.execute(
        select(MessageSignal).where(
            MessageSignal.tenant_id == tenant_id, MessageSignal.strategy.is_not(None),
            MessageSignal.message_created_at >= since,
        )
    )).scalars().all()
    opportunities = (await db.execute(
        select(SalesOpportunity).where(SalesOpportunity.tenant_id == tenant_id)
    )).scalars().all()
    by_conversation: dict = {}
    for opp in opportunities:
        by_conversation.setdefault(opp.conversation_id, []).append(opp)
    for items in by_conversation.values():
        items.sort(key=lambda o: aware(o.started_at))

    def opportunity_of(signal):
        chosen = None
        for opp in by_conversation.get(signal.conversation_id, []):
            if aware(opp.started_at) <= aware(signal.message_created_at):
                chosen = opp
        return chosen

    stats: dict = {}
    for signal in signals:
        strategy = STRATEGIES_BY_CODE.get(signal.strategy)
        if strategy is None:
            continue
        entry = stats.setdefault(strategy.code, {"uses": 0, "opps": {}})
        entry["uses"] += 1
        opp = opportunity_of(signal)
        if opp is not None:
            entry["opps"][opp.id] = opp

    rows = []
    for code, entry in stats.items():
        strategy = STRATEGIES_BY_CODE[code]
        opps = list(entry["opps"].values())
        terminated = [o for o in opps if o.outcome in OpportunityOutcome.TERMINATED]
        paid = [o for o in opps if o.outcome == OpportunityOutcome.PAID]
        enough = len(terminated) >= MIN_TERMINATED_FOR_RATE
        rows.append({
            "code": code,
            "label": strategy.label,
            "objection": strategy.objection,
            "objection_label": objection_label(strategy.objection),
            "uses": entry["uses"],
            "opportunities": len(opps),
            "terminated": len(terminated),
            "paid": len(paid),
            "enough_data": enough,
            "conversion_rate_pct": round(len(paid) / len(terminated) * 100, 1) if enough else None,
        })
    rows.sort(key=lambda r: (r["objection_label"], -r["uses"], r["label"]))
    return {"period_days": days, "min_terminated_for_rate": MIN_TERMINATED_FOR_RATE, "strategies": rows}
