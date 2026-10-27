"""
Lot 90 — « mode simple » du tableau de bord (parcours « Bob — Parcours du commerçant », décisions du 26-27/10).

Quatre grands boutons pour un commerçant peu habitué au numérique : ajouter un produit, clients qui vous attendent,
ventes à confirmer (« Vendu » ou « Annuler » quand la preuve de vente est arrivée), mes ventes. Les données viennent de
ce qui existe déjà ; les actions passent par les routes existantes (création de produit, commande payée ou annulée,
conversation rendue à Bob). Le tableau de bord complet ne change pas : « Plus de réglages » y mène.

Mode par défaut : SIMPLE pour un commerce (boutique), FULL pour une agence, une concession ou un courtier ; les comptes
créés avant le lot 90 ont reçu FULL à la migration (ils voient un bandeau pour essayer).
"""
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

MODES = ("SIMPLE", "FULL")
SIMPLE_SECTORS = {"ONLINE_STORE"}
LIST_LIMIT = 50


def default_mode(tenant) -> str:
    return "SIMPLE" if (tenant.business_type or "ONLINE_STORE") in SIMPLE_SECTORS else "FULL"


def effective_mode(user, tenant) -> str:
    return user.ui_mode if user is not None and user.ui_mode in MODES else default_mode(tenant)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _day_start(tenant, now: datetime, days_back: int = 0) -> datetime:
    from app.services.local_time import tenant_zone

    zone = tenant_zone(tenant)
    local = now.astimezone(zone).date() - timedelta(days=days_back)
    return datetime.combine(local, time.min, zone).astimezone(timezone.utc)


def _amount(value) -> str:
    number = Decimal(str(value or 0)).quantize(Decimal("1"))
    return f"{int(number):,}".replace(",", " ")


def customer_label(customer) -> str:
    """Le prénom du client s'il est connu, sinon la fin de son numéro (le commerçant le reconnaît)."""
    if customer is None:
        return "Un client"
    name = " ".join(p for p in (customer.first_name, customer.last_name) if p).strip()
    if name:
        return name
    digits = re.sub(r"\D", "", customer.whatsapp_number or "")
    return f"Client •• {digits[-4:]}" if len(digits) >= 4 else "Un client"


async def _paid_between(db, tenant, since, until=None) -> tuple[int, Decimal]:
    from app.models.order import Order, OrderStatus

    when = func.coalesce(Order.paid_at, Order.created_at)
    filters = [Order.tenant_id == tenant.id, Order.status == OrderStatus.PAID, when >= since]
    if until is not None:
        filters.append(when < until)
    count, total = (await db.execute(select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0))
                                     .where(*filters))).one()
    return int(count or 0), Decimal(str(total or 0))


async def home(db, tenant, user, now: datetime | None = None) -> dict:
    from app.core.config import get_settings
    from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
    from app.models.order import Order, OrderStatus
    from app.models.product import Product
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services import bob_pause
    from app.services.business_type import is_insurance

    now = now or datetime.now(timezone.utc)
    today = _day_start(tenant, now)
    sales, revenue = await _paid_between(db, tenant, today)
    served = (await db.execute(select(func.count(func.distinct(Message.conversation_id))).where(
        Message.tenant_id == tenant.id, Message.sender == MessageSender.CUSTOMER, Message.created_at >= today))).scalar_one()
    waiting = (await db.execute(select(func.count(Conversation.id)).where(
        Conversation.tenant_id == tenant.id, Conversation.status == ConversationStatus.WAITING_HUMAN))).scalar_one()
    to_confirm = (await db.execute(select(func.count(Order.id)).where(
        Order.tenant_id == tenant.id, Order.status == OrderStatus.PENDING))).scalar_one()
    products = (await db.execute(select(func.count(Product.id)).where(
        Product.tenant_id == tenant.id, Product.active.is_(True)))).scalar_one()
    out_of_stock = [] if is_insurance(tenant) else (await db.execute(
        select(Product.name).where(Product.tenant_id == tenant.id, Product.active.is_(True), Product.stock_quantity <= 0)
        .order_by(Product.updated_at.desc()).limit(3))).scalars().all()
    whatsapp = (await db.execute(select(WhatsAppAccount.id).where(WhatsAppAccount.tenant_id == tenant.id).limit(1))).first()
    first = (user.full_name or "").split()[0] if user is not None and (user.full_name or "").strip() else None
    support = re.sub(r"\D", "", get_settings().naomy_whatsapp_number or "")  # numéro AgenC'AI (aide)
    return {"first_name": first, "shop": tenant.name, "currency": tenant.currency or "XOF",
            "bob": bob_pause.status(tenant, now).as_dict(),
            "today": {"sales": sales, "revenue": _amount(revenue), "customers": int(served or 0)},
            "waiting": int(waiting or 0), "to_confirm": int(to_confirm or 0), "out_of_stock": list(out_of_stock),
            "setup": {"whatsapp": whatsapp is not None, "products": int(products or 0),
                      "ready": whatsapp is not None and int(products or 0) > 0},
            "support_whatsapp": support or None}


async def waiting_list(db, tenant) -> list[dict]:
    from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
    from app.models.customer import Customer
    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalars().first()
    from_phone = bool(account is not None and account.coexistence_mode)
    rows = (await db.execute(select(Conversation, Customer).join(Customer, Customer.id == Conversation.customer_id).where(
        Conversation.tenant_id == tenant.id, Conversation.status == ConversationStatus.WAITING_HUMAN)
        .order_by(Conversation.last_message_at.desc().nulls_last()).limit(LIST_LIMIT))).all()
    out = []
    for conversation, customer in rows:
        last = (await db.execute(select(Message.content).where(
            Message.conversation_id == conversation.id, Message.sender == MessageSender.CUSTOMER)
            .order_by(Message.created_at.desc()).limit(1))).scalar_one_or_none()
        digits = re.sub(r"\D", "", customer.whatsapp_number or "")
        out.append({"conversation_id": str(conversation.id), "name": customer_label(customer),
                    "last_message": (last or "")[:200], "since": _iso(conversation.last_message_at),
                    # Coexistence (lot 52) : le commerçant répond depuis l'application WhatsApp de son téléphone.
                    "whatsapp_link": f"https://wa.me/{digits}" if from_phone and digits else None})
    return out


async def to_confirm_list(db, tenant) -> list[dict]:
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem, OrderStatus
    from app.models.product import Product

    rows = (await db.execute(select(Order, Customer).join(Customer, Customer.id == Order.customer_id).where(
        Order.tenant_id == tenant.id, Order.status == OrderStatus.PENDING)
        .order_by(Order.created_at.desc()).limit(LIST_LIMIT))).all()
    out = []
    for order, customer in rows:
        items = (await db.execute(select(OrderItem.quantity, Product.name).join(Product, Product.id == OrderItem.product_id)
                                  .where(OrderItem.order_id == order.id))).all()
        out.append({"order_id": str(order.id), "customer": customer_label(customer),
                    "items": " · ".join(f"{q} × {n}" for q, n in items), "total": _amount(order.total_amount),
                    "currency": order.currency, "created_at": _iso(order.created_at),
                    "conversation_id": str(order.conversation_id) if order.conversation_id else None,
                    "reminded": (order.reminders_sent or 0) > 0})
    return out


async def sales_summary(db, tenant, now: datetime | None = None) -> dict:
    from app.models.order import Order, OrderItem, OrderStatus
    from app.models.product import Product

    now = now or datetime.now(timezone.utc)
    periods = {}
    for key, days_back in (("today", 0), ("week", 6), ("month", 29)):
        count, total = await _paid_between(db, tenant, _day_start(tenant, now, days_back))
        periods[key] = {"sales": count, "revenue": _amount(total)}
    since = _day_start(tenant, now, 29)
    when = func.coalesce(Order.paid_at, Order.created_at)
    total = func.sum(OrderItem.subtotal)
    top = (await db.execute(select(Product.name, func.sum(OrderItem.quantity), total)
                            .join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
                            .where(Order.tenant_id == tenant.id, Order.status == OrderStatus.PAID, when >= since)
                            .group_by(Product.name).order_by(total.desc()).limit(5))).all()
    return {"currency": tenant.currency or "XOF", **periods,
            "top": [{"name": n, "quantity": int(q or 0), "revenue": _amount(s)} for n, q, s in top]}


def _iso(value):
    value = _aware(value)
    return value.isoformat() if value is not None else None
