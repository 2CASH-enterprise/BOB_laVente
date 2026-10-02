"""
Lot 50 — lecture de la consommation de l'IA : par boutique et par mois, coût estimé, taux de cache.
Chiffres CALCULÉS à partir du journal llm_usage (jamais estimés par un modèle).
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.llm_usage import LlmUsage
from app.models.tenant import Tenant
from app.services.llm_costs import cost_usd


def month_bounds(month: str | None, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """« 2026-10 » → (1er octobre, 1er novembre, « 2026-10 ») en UTC ; mois en cours par défaut."""
    now = now or datetime.now(timezone.utc)
    if month:
        year, mon = (int(x) for x in month.split("-"))
    else:
        year, mon = now.year, now.month
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end, f"{year:04d}-{mon:02d}"


async def costs_by_tenant(db: AsyncSession, start: datetime, end: datetime, tenant_id=None) -> list[dict]:
    stmt = select(
        LlmUsage.tenant_id, LlmUsage.model, func.count(LlmUsage.id),
        func.sum(LlmUsage.prompt_tokens), func.sum(LlmUsage.cached_tokens), func.sum(LlmUsage.completion_tokens),
    ).where(LlmUsage.created_at >= start, LlmUsage.created_at < end).group_by(LlmUsage.tenant_id, LlmUsage.model)
    if tenant_id is not None:
        stmt = stmt.where(LlmUsage.tenant_id == tenant_id)
    per_tenant: dict = {}
    for tid, model, calls, prompt, cached, completion in (await db.execute(stmt)).all():
        row = per_tenant.setdefault(tid, {"calls": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
                                          "cost_usd": 0.0, "unpriced_models": []})
        row["calls"] += calls
        row["prompt_tokens"] += prompt or 0
        row["cached_tokens"] += cached or 0
        row["completion_tokens"] += completion or 0
        cost = cost_usd(model, prompt or 0, cached or 0, completion or 0)
        if cost is None:
            row["unpriced_models"].append(model)
        else:
            row["cost_usd"] += cost
    if not per_tenant:
        return []
    conversations = dict((await db.execute(
        select(LlmUsage.tenant_id, func.count(func.distinct(LlmUsage.conversation_id)))
        .where(LlmUsage.created_at >= start, LlmUsage.created_at < end, LlmUsage.conversation_id.is_not(None))
        .group_by(LlmUsage.tenant_id)
    )).all())
    names = dict((await db.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(list(per_tenant))))).all())
    rows = []
    for tid, row in per_tenant.items():
        convs = conversations.get(tid, 0)
        rows.append({
            "tenant_id": str(tid), "tenant": names.get(tid, "?"), "conversations": convs, **row,
            "cost_usd": round(row["cost_usd"], 4),
            "cost_per_conversation_usd": round(row["cost_usd"] / convs, 4) if convs else None,
            "cache_rate_pct": round(row["cached_tokens"] / row["prompt_tokens"] * 100, 1) if row["prompt_tokens"] else None,
        })
    rows.sort(key=lambda r: -r["cost_usd"])
    return rows
