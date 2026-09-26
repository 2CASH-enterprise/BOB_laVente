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
    revenue_paid: float  # toutes les commandes payées de la période (y compris hors conversation)
    revenue_pending: float  # toutes les commandes non payées créées pendant la période
    revenue_pending_in_paid_opportunities: float = 0.0  # part du « en attente » dans des opportunités déjà payées
    outside_conversation_orders: int = 0  # commandes saisies à la main qu'aucune opportunité ne porte
    outside_conversation_paid: float = 0.0
    by_source: list[SourceBreakdown]


class IntentStat(BaseModel):
    code: str
    label: str
    messages: int
    opportunities: int


class ObjectionStat(BaseModel):
    code: str
    label: str
    messages: int
    opportunities: int
    terminated: int
    paid: int
    conversion_rate_pct: float | None


class SignalsSummaryResponse(BaseModel):
    """Phase 1 — étiquettes DÉDUITES par un classificateur : des indications, pas des faits."""

    period_days: int
    classified_messages: int
    customer_messages: int
    intents: list[IntentStat]
    objections: list[ObjectionStat]
