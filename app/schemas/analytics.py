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
