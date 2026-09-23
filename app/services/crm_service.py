"""
Fiche contact CRM (section CRM.9) — le statut commercial est TOUJOURS calculé depuis
les vraies données (commandes, conversations), jamais stocké séparément : évite une
double source de vérité qui pourrait se désynchroniser de la réalité.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.customer_product_view import CustomerProductView
from app.models.order import Order, OrderStatus

MAX_RECENT_ORDERS = 5


async def _commercial_status(db: AsyncSession, tenant_id, customer_id) -> str:
    has_order_stmt = select(Order.id).where(
        Order.tenant_id == tenant_id, Order.customer_id == customer_id, Order.status != OrderStatus.CANCELLED
    ).limit(1)
    if (await db.execute(has_order_stmt)).first() is not None:
        return "CLIENT"

    has_conversation_stmt = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.customer_id == customer_id
    ).limit(1)
    if (await db.execute(has_conversation_stmt)).first() is not None:
        return "PROSPECT"

    return "NOUVEAU"


async def _last_activity(db: AsyncSession, tenant_id, customer_id):
    stmt = select(func.max(Conversation.last_message_at)).where(
        Conversation.tenant_id == tenant_id, Conversation.customer_id == customer_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _order_stats(db: AsyncSession, tenant_id, customer_id) -> tuple[int, float]:
    stmt = select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0)).where(
        Order.tenant_id == tenant_id, Order.customer_id == customer_id, Order.status != OrderStatus.CANCELLED
    )
    row = (await db.execute(stmt)).one()
    return row[0], float(row[1])


async def list_customers_with_summary(db: AsyncSession, tenant_id) -> list[dict]:
    stmt = select(Customer).where(Customer.tenant_id == tenant_id).order_by(Customer.updated_at.desc())
    customers = (await db.execute(stmt)).scalars().all()

    results = []
    for customer in customers:
        status = await _commercial_status(db, tenant_id, customer.id)
        last_activity = await _last_activity(db, tenant_id, customer.id)
        order_count, total_spent = await _order_stats(db, tenant_id, customer.id)
        results.append(
            {
                "id": customer.id,
                "display_name": _display_name(customer),
                "whatsapp_number": customer.whatsapp_number,
                "acquisition_source": customer.acquisition_source,
                "commercial_status": status,
                "last_activity": last_activity,
                "order_count": order_count,
                "total_spent": total_spent,
                "tags": customer.tags or [],
                "marketing_consent": customer.marketing_consent,
            }
        )
    return results


async def get_customer_detail(db: AsyncSession, tenant_id, customer_id) -> dict | None:
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != tenant_id:
        return None

    status = await _commercial_status(db, tenant_id, customer.id)
    last_activity = await _last_activity(db, tenant_id, customer.id)
    order_count, total_spent = await _order_stats(db, tenant_id, customer.id)

    views_stmt = select(func.count(CustomerProductView.id)).where(
        CustomerProductView.tenant_id == tenant_id, CustomerProductView.customer_id == customer.id
    )
    viewed_count = (await db.execute(views_stmt)).scalar_one()

    active_conv_stmt = (
        select(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.customer_id == customer.id)
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    latest_conversation = (await db.execute(active_conv_stmt)).scalar_one_or_none()

    return {
        "id": customer.id,
        "display_name": _display_name(customer),
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "whatsapp_number": customer.whatsapp_number,
        "email": customer.email,
        "city": customer.city,
        "acquisition_source": customer.acquisition_source,
        "acquisition_detail": customer.acquisition_detail,
        "commercial_status": status,
        "last_activity": last_activity,
        "order_count": order_count,
        "total_spent": total_spent,
        "products_viewed_count": viewed_count,
        "detected_preferences": customer.detected_preferences or {},
        "tags": customer.tags or [],
        "notes": customer.notes,
        "marketing_consent": customer.marketing_consent,
        "marketing_consent_given_at": customer.marketing_consent_given_at,
        "marketing_consent_source": customer.marketing_consent_source,
        "marketing_consent_withdrawn_at": customer.marketing_consent_withdrawn_at,
        "latest_conversation_id": latest_conversation.id if latest_conversation else None,
    }


def _display_name(customer: Customer) -> str:
    if customer.first_name or customer.last_name:
        return f"{customer.first_name or ''} {customer.last_name or ''}".strip()
    return customer.whatsapp_number
