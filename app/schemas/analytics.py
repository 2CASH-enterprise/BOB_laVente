from datetime import datetime

from pydantic import BaseModel


class AnalyticsResponse(BaseModel):
    """Section 26 — chiffres du dashboard entreprise."""

    total_conversations: int
    qualified_prospects: int
    total_orders: int
    revenue: float
    currency: str | None
    conversion_rate_pct: float
    human_handoffs: int


class SourceBreakdown(BaseModel):
    label: str
    opportunities: int
    terminated: int
    paid: int
    conversion_rate_pct: float | None


class SalesSummaryResponse(BaseModel):
    """Phase 0 — issues des opportunités, calculées à partir de faits (jamais par un LLM)."""

    period_days: int
    computed_at: datetime | None  # None = jamais calculé pour ce compte
    currency: str | None
    total: int
    outcomes: dict[str, int]
    with_order: int
    paid: int
    terminated: int
    conversion_rate_pct: float | None  # payées ÷ terminées ; None si aucune opportunité terminée
    revenue_paid: float
    revenue_pending: float
    by_source: list[SourceBreakdown]
