from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.models.conversation import Conversation, Message
from app.models.order import Order, OrderStatus
from app.schemas.analytics import AnalyticsResponse, SalesSummaryResponse, SignalsSummaryResponse
from app.services.opportunity_service import recompute_tenant_opportunities, sales_summary

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("", response_model=AnalyticsResponse)
async def get_analytics(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    tenant_id = current_user.tenant_id

    total_conversations = (
        await db.execute(select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant_id))
    ).scalar_one()

    qualified_prospects = (
        await db.execute(
            select(func.count(func.distinct(Conversation.customer_id))).where(Conversation.tenant_id == tenant_id)
        )
    ).scalar_one()

    orders_stmt = select(Order).where(Order.tenant_id == tenant_id, Order.status != OrderStatus.CANCELLED)
    orders = (await db.execute(orders_stmt)).scalars().all()
    total_orders = len(orders)
    revenue = sum(float(o.total_amount) for o in orders)
    currency = orders[0].currency if orders else None

    conversion_rate = (total_orders / total_conversations * 100) if total_conversations else 0.0

    human_handoffs = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.tenant_id == tenant_id, Message.message_type == "handoff")
        )
    ).scalar_one()

    return AnalyticsResponse(
        total_conversations=total_conversations,
        qualified_prospects=qualified_prospects,
        total_orders=total_orders,
        revenue=revenue,
        currency=currency,
        conversion_rate_pct=round(conversion_rate, 1),
        human_handoffs=human_handoffs,
    )


@router.get("/sales", response_model=SalesSummaryResponse)
async def get_sales_summary(
    days: int = Query(default=30, ge=1, le=365),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SalesSummaryResponse:
    """Issues des opportunités de la période (table recalculée chaque nuit et à la demande)."""
    return SalesSummaryResponse(**await sales_summary(db, current_user.tenant_id, days=days))


@router.post("/sales/refresh", response_model=SalesSummaryResponse)
async def refresh_sales_summary(
    days: int = Query(default=30, ge=1, le=365),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SalesSummaryResponse:
    """Recalcul immédiat pour CE compte uniquement (lecture de ses propres données)."""
    await recompute_tenant_opportunities(db, current_user.tenant_id)
    return SalesSummaryResponse(**await sales_summary(db, current_user.tenant_id, days=days))


@router.get("/signals", response_model=SignalsSummaryResponse)
async def get_signals_summary(
    days: int = Query(default=30, ge=1, le=365),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalsSummaryResponse:
    """Ce que demandent les clients, et les objections avec la conversion des opportunités concernées."""
    from app.services.signal_service import signals_summary

    return SignalsSummaryResponse(**await signals_summary(db, current_user.tenant_id, days=days))


@router.get("/strategies")
async def get_strategies_summary(
    days: int = Query(default=30, ge=1, le=365),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stratégies utilisées et issue des opportunités concernées (taux affiché seulement au-delà du seuil)."""
    from app.services.strategy_service import strategies_summary

    return await strategies_summary(db, current_user.tenant_id, days=days)
