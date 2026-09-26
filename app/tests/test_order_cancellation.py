import uuid

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryStatus
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.order_service import build_cancellation_message, create_order


async def _setup(db_session, email: str, role=Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn-{uuid.uuid4().hex[:8]}", system_user_token="t"))
    phone = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=10)
    case = Product(tenant_id=tenant.id, sku="COQUE", name="Coque", price=5000, currency="XOF", stock_quantity=50)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000001")
    db_session.add_all([phone, case, customer])
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    return tenant, phone, case, customer, conversation


async def _order(db_session, tenant, customer, conversation, items):
    order = await create_order(
        db_session, tenant_id=tenant.id, customer_id=customer.id, items=items,
        delivery_address="Dakar", payment_method="Wave", created_by="IA", conversation_id=conversation.id,
    )
    await db_session.commit()
    return order


async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _reload(db_session, model, record_id):
    return (await db_session.execute(
        select(model).where(model.id == record_id).execution_options(populate_existing=True)
    )).scalar_one()


async def _delivery_of(db_session, order) -> Delivery:
    return (await db_session.execute(select(Delivery).where(Delivery.order_id == order.id))).scalar_one()


@pytest.mark.asyncio
async def test_order_creation_creates_its_delivery_which_cancellation_cancels(client, db_session, unique_email):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    delivery = await _delivery_of(db_session, order)
    assert delivery.status == DeliveryStatus.PENDING

    await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert (await _reload(db_session, Delivery, delivery.id)).status == DeliveryStatus.CANCELLED


@pytest.fixture
def whatsapp(monkeypatch):
    sent: list[dict] = []
    state = {"fail": False}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            if state["fail"]:
                raise RuntimeError("131047 : hors de la fenêtre de 24 h")
            sent.append({"to": to, "body": body})
            return {}

    monkeypatch.setattr("app.api.orders.routes.WhatsAppClient", _Client)
    return {"sent": sent, "state": state}


# --- Le cœur : statut et remise en stock ---------------------------------------------

@pytest.mark.asyncio
async def test_cancel_restores_stock_of_every_item(client, db_session, unique_email, whatsapp):
    tenant, phone, case, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv,
                         [{"product_id": str(phone.id), "quantity": 2}, {"product_id": str(case.id), "quantity": 3}])
    assert (await _reload(db_session, Product, phone.id)).stock_quantity == 8  # la création a bien réservé

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    assert r.json()["customer_notified"] is None  # non demandé
    assert (await _reload(db_session, Product, phone.id)).stock_quantity == 10
    assert (await _reload(db_session, Product, case.id)).stock_quantity == 50
    assert whatsapp["sent"] == []  # désactivé par défaut


@pytest.mark.asyncio
async def test_restock_even_if_product_was_deactivated(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    product = await _reload(db_session, Product, phone.id)
    product.active = False
    await db_session.commit()

    await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert (await _reload(db_session, Product, phone.id)).stock_quantity == 10


# --- Refus ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paid_order_cannot_be_cancelled(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    headers = await _headers(client, unique_email)
    await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=headers)

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=headers)

    assert r.status_code == 400
    assert "payée" in r.json()["detail"]
    assert (await _reload(db_session, Product, phone.id)).stock_quantity == 9  # stock inchangé


@pytest.mark.asyncio
async def test_double_cancellation_is_refused_and_does_not_restock_twice(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 4}])
    headers = await _headers(client, unique_email)

    assert (await client.put(f"/api/v1/orders/{order.id}/cancel", headers=headers)).status_code == 200
    second = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=headers)

    assert second.status_code == 400
    assert (await _reload(db_session, Product, phone.id)).stock_quantity == 10  # pas 14


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.DELIVERED])
async def test_cannot_cancel_when_delivery_has_left(client, db_session, unique_email, whatsapp, status):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    delivery = await _delivery_of(db_session, order)  # créée automatiquement avec la commande
    delivery.status = status
    await db_session.commit()

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert r.status_code == 400
    assert "livraison" in r.json()["detail"]
    assert (await _reload(db_session, Order, order.id)).status == OrderStatus.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DeliveryStatus.PENDING, DeliveryStatus.CONFIRMED])
async def test_delivery_not_yet_left_is_cancelled_too(client, db_session, unique_email, whatsapp, status):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    delivery = await _delivery_of(db_session, order)
    delivery.status = status
    await db_session.commit()

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert r.status_code == 200
    assert (await _reload(db_session, Delivery, delivery.id)).status == DeliveryStatus.CANCELLED


@pytest.mark.asyncio
async def test_other_merchant_cannot_cancel(client, db_session, whatsapp):
    tenant_a, phone_a, _, customer_a, conv_a = await _setup(db_session, "a@cancel.sn")
    await _setup(db_session, "b@cancel.sn")
    order = await _order(db_session, tenant_a, customer_a, conv_a, [{"product_id": str(phone_a.id), "quantity": 1}])

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, "b@cancel.sn"))

    assert r.status_code == 400
    assert (await _reload(db_session, Order, order.id)).status == OrderStatus.PENDING
    assert (await _reload(db_session, Product, phone_a.id)).stock_quantity == 9


@pytest.mark.asyncio
async def test_viewer_role_cannot_cancel(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email, role=Role.VIEWER)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", headers=await _headers(client, unique_email))

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unknown_order(client, db_session, unique_email, whatsapp):
    await _setup(db_session, unique_email)
    r = await client.put(f"/api/v1/orders/{uuid.uuid4()}/cancel", headers=await _headers(client, unique_email))
    assert r.status_code == 400


# --- Audit et message au client ------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_is_audited_with_reason(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])

    await client.put(f"/api/v1/orders/{order.id}/cancel", json={"reason": "  Commande de test  "},
                     headers=await _headers(client, unique_email))

    entry = (await db_session.execute(select(AuditLog).where(AuditLog.action == "ORDER_CANCELLED"))).scalar_one()
    assert entry.tenant_id == tenant.id
    assert entry.details["reason"] == "Commande de test"
    assert entry.details["order_id"] == str(order.id)


@pytest.mark.asyncio
async def test_reason_too_long_is_refused(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"reason": "x" * 301},
                         headers=await _headers(client, unique_email))
    assert r.status_code == 422
    assert (await _reload(db_session, Order, order.id)).status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_customer_notified_only_on_request_with_fixed_message(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 2}])

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers=await _headers(client, unique_email))

    assert r.json()["customer_notified"] is True
    assert whatsapp["sent"] == [{"to": "221700000001", "body": build_cancellation_message(order)}]
    assert f"n° {str(order.id)[:8].upper()}" in whatsapp["sent"][0]["body"]
    assert "560,000 XOF" in whatsapp["sent"][0]["body"]
    stored = (await db_session.execute(select(Message).where(Message.message_type == "order_cancelled"))).scalars().all()
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_refused_message_is_reported_but_cancellation_stands(client, db_session, unique_email, whatsapp):
    """Fenêtre de 24 h dépassée : on le dit honnêtement, sans prétendre que le message est parti."""
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])
    whatsapp["state"]["fail"] = True

    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers=await _headers(client, unique_email))

    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    assert r.json()["customer_notified"] is False
    assert (await db_session.execute(select(Message).where(Message.message_type == "order_cancelled"))).first() is None
    entry = (await db_session.execute(select(AuditLog).where(AuditLog.action == "ORDER_CANCELLED"))).scalar_one()
    assert entry.details["customer_notified"] is False


# --- Effet sur la mesure des ventes et garde-fou IA ------------------------------------

@pytest.mark.asyncio
async def test_sales_measurement_reflects_cancellation(client, db_session, unique_email, whatsapp):
    tenant, phone, _, customer, conv = await _setup(db_session, unique_email)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv.id, sender="CUSTOMER", message_type="text", content="Je prends"))
    await db_session.commit()
    order = await _order(db_session, tenant, customer, conv, [{"product_id": str(phone.id), "quantity": 1}])

    headers = await _headers(client, unique_email)
    await client.put(f"/api/v1/orders/{order.id}/cancel", headers=headers)
    # Comme le commerçant : bouton « Recalculer » (session neuve, comme la tâche de nuit).
    summary = (await client.post("/api/v1/analytics/sales/refresh", headers=headers)).json()

    assert summary["outcomes"]["CANCELLED"] == 1
    assert summary["revenue_pending"] == 0


def test_ai_has_no_tool_to_cancel_an_order():
    """Principe : aucune action commerciale irréversible par l'IA."""
    from app.agents.tool_definitions import TOOL_DEFINITIONS

    names = {tool["name"] for tool in TOOL_DEFINITIONS}
    assert not any("cancel" in name or "annul" in name for name in names)
