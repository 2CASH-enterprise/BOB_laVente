from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.integrations.whatsapp.client import WhatsAppClient
from app.models.conversation import Message, MessageSender
from app.models.delivery import Delivery
from app.models.order import OrderItem
from app.repositories.order_repository import OrderRepository
from app.schemas.delivery import DeliveryResponse, DeliveryUpdate
from app.schemas.order import OrderCancelRequest, OrderCancelResponse, OrderCreateRequest, OrderDetailResponse, OrderResponse
from app.services.audit import log_audit_event
from app.services.order_service import (
    OrderCreationError,
    build_cancellation_message,
    cancel_order,
    create_order,
    mark_order_as_paid,
)

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


async def _build_order_detail(db: AsyncSession, order) -> OrderDetailResponse:
    items_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
    items = (await db.execute(items_stmt)).scalars().all()

    delivery_stmt = select(Delivery).where(Delivery.order_id == order.id)
    delivery = (await db.execute(delivery_stmt)).scalar_one_or_none()

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
        delivery_status=delivery.status.value if delivery else None,
        tracking_number=delivery.tracking_number if delivery else None,
    )


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
    return await _build_order_detail(db, order)


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

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="ORDER_CREATED",
        tenant_id=current_user.tenant_id,
        details={"order_id": str(order.id), "total_amount": float(order.total_amount)},
    )
    await db.commit()
    await db.refresh(order)
    return await _build_order_detail(db, order)


@router.put(
    "/{order_id}/delivery", response_model=DeliveryResponse, dependencies=[Depends(require_role("AGENT"))]
)
async def update_delivery(
    order_id: UUID,
    payload: DeliveryUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Section 40 — mise à jour manuelle du suivi de livraison depuis le dashboard."""
    order_repo = OrderRepository(db)
    order = await order_repo.get(tenant_id=current_user.tenant_id, record_id=order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Commande introuvable")

    stmt = select(Delivery).where(Delivery.tenant_id == current_user.tenant_id, Delivery.order_id == order_id)
    delivery = (await db.execute(stmt)).scalar_one_or_none()
    if delivery is None:
        delivery = Delivery(tenant_id=current_user.tenant_id, order_id=order_id)
        db.add(delivery)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(delivery, field, value)

    await log_audit_event(
        db,
        actor=str(current_user.user_id),
        action="DELIVERY_UPDATED",
        tenant_id=current_user.tenant_id,
        details={"order_id": str(order_id), "status": payload.status},
    )
    await db.commit()
    await db.refresh(delivery)
    return delivery


@router.put("/{order_id}/mark-paid", response_model=OrderDetailResponse, dependencies=[Depends(require_role("AGENT"))])
async def mark_order_paid(
    order_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Section 13/39 — action humaine EXCLUSIVE : le commerçant confirme avoir vérifié une
    preuve de paiement (capture d'écran mobile money). C'est ce clic, et lui seul, qui
    déclenche le reçu client et la commission — jamais automatique, jamais depuis l'IA.
    """
    from app.models.customer import Customer
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.receipt_service import generate_receipt_text, get_order_item_lines

    try:
        order = await mark_order_as_paid(db, current_user.tenant_id, order_id)
    except OrderCreationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    # Reçu déterministe, envoyé tel quel — jamais reformulé par le LLM.
    item_lines = await get_order_item_lines(db, order.id, order.currency)
    tenant = await db.get(Tenant, current_user.tenant_id)
    receipt_text = generate_receipt_text(order, item_lines, tenant.name if tenant else "")

    # Le message n'est persisté dans l'historique QUE si la commande est liée à une
    # conversation (ex. commande créée par l'IA) — une commande créée depuis le dashboard
    # sans conversation associée n'a pas cette contrainte, mais l'envoi WhatsApp reste tenté.
    if order.conversation_id is not None:
        db.add(
            Message(
                tenant_id=current_user.tenant_id, conversation_id=order.conversation_id,
                sender=MessageSender.SYSTEM, message_type="receipt", content=receipt_text,
            )
        )

    account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
    account = (await db.execute(account_stmt)).scalar_one_or_none()
    customer = await db.get(Customer, order.customer_id)
    if account is not None and customer is not None:
        try:
            wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
            await wa_client.send_text_message(to=customer.whatsapp_number, body=receipt_text)
        except Exception:  # noqa: BLE001 — un échec d'envoi ne doit jamais bloquer la confirmation
            pass

    await log_audit_event(
        db, actor=str(current_user.user_id), action="ORDER_MARKED_PAID", tenant_id=current_user.tenant_id,
        details={"order_id": str(order_id)},
    )
    await db.commit()

    return await _build_order_detail(db, order)


@router.put("/{order_id}/cancel", response_model=OrderCancelResponse, dependencies=[Depends(require_role("AGENT"))])
async def cancel_order_route(
    order_id: UUID,
    payload: OrderCancelRequest | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Annulation par un humain uniquement (l'IA n'a aucun outil pour annuler). Remet les articles
    en stock, trace l'audit avec le motif, et prévient le client SEULEMENT si le commerçant le
    demande — par un message fixe, jamais rédigé par l'IA.
    """
    from app.models.customer import Customer
    from app.models.whatsapp_account import WhatsAppAccount

    payload = payload or OrderCancelRequest()
    try:
        order = await cancel_order(db, current_user.tenant_id, order_id)
    except OrderCreationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    reason = payload.reason.strip() if payload.reason and payload.reason.strip() else None
    customer_notified: bool | None = None
    if payload.notify_customer:
        text = build_cancellation_message(order)
        customer_notified = False
        account = (await db.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == current_user.tenant_id)
        )).scalar_one_or_none()
        customer = await db.get(Customer, order.customer_id)
        if account is not None and customer is not None:
            try:
                wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
                await wa_client.send_text_message(to=customer.whatsapp_number, body=text)
                customer_notified = True
            except Exception:  # noqa: BLE001 — l'annulation reste valable même si le message est refusé
                customer_notified = False
        # Trace dans l'historique uniquement si le message est réellement parti.
        if customer_notified and order.conversation_id is not None:
            db.add(Message(
                tenant_id=current_user.tenant_id, conversation_id=order.conversation_id,
                sender=MessageSender.SYSTEM, message_type="order_cancelled", content=text,
            ))

    await log_audit_event(
        db, actor=str(current_user.user_id), action="ORDER_CANCELLED", tenant_id=current_user.tenant_id,
        details={"order_id": str(order_id), "reason": reason, "customer_notified": customer_notified},
    )
    await db.commit()

    detail = await _build_order_detail(db, order)
    return OrderCancelResponse(**detail.model_dump(), customer_notified=customer_notified)
