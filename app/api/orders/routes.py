from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.order import OrderItem
from app.repositories.order_repository import OrderRepository
from app.schemas.order import OrderCreateRequest, OrderDetailResponse, OrderResponse
from app.services.order_service import OrderCreationError, create_order

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


@router.get("", response_model=list[OrderResponse])
async def list_orders(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrderRepository(db)
    return await repo.list(tenant_id=current_user.tenant_id, limit=100)


@router.get("/{order_id}", response_model=OrderDetailResponse)
async def get_order(
    order_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrderRepository(db)
    order = await repo.get(tenant_id=current_user.tenant_id, record_id=order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Commande introuvable")

    items_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
    items = (await db.execute(items_stmt)).scalars().all()

    return OrderDetailResponse(
        id=order.id,
        customer_id=order.customer_id,
        status=order.status.value,
        total_amount=order.total_amount,
        currency=order.currency,
        delivery_address=order.delivery_address,
        payment_method=order.payment_method,
        created_by=order.created_by,
        items=items,
    )


@router.post("", response_model=OrderDetailResponse, status_code=201, dependencies=[Depends(require_role("AGENT"))])
async def create_order_endpoint(
    payload: OrderCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Création manuelle d'une commande depuis le dashboard (ex. un vendeur humain qui finalise
    une vente négociée par téléphone). Réutilise exactement le même service que l'outil IA
    `create_order` (section 18) — mêmes vérifications de stock, jamais de raccourci.
    """
    try:
        order = await create_order(
            db=db,
            tenant_id=current_user.tenant_id,
            customer_id=payload.customer_id,
            items=[item.model_dump() for item in payload.items],
            delivery_address=payload.delivery_address,
            payment_method=payload.payment_method,
            created_by=str(current_user.user_id),
        )
    except OrderCreationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    await db.commit()
    await db.refresh(order)

    items_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
    items = (await db.execute(items_stmt)).scalars().all()

    return OrderDetailResponse(
        id=order.id,
        customer_id=order.customer_id,
        status=order.status.value,
        total_amount=order.total_amount,
        currency=order.currency,
        delivery_address=order.delivery_address,
        payment_method=order.payment_method,
        created_by=order.created_by,
        items=items,
    )
