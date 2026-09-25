import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.order_service import create_order


async def _setup(db_session, email: str, role: Role = Role.OWNER, commission_rate=None):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, commission_rate=commission_rate)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=100000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(tenant)
    await db_session.refresh(product)
    await db_session.refresh(customer)
    return tenant, product, customer


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_mark_order_paid_via_api(client, db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=5.0)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["status"] == "PAID"


@pytest.mark.asyncio
async def test_mark_order_paid_generates_receipt_message(client, db_session, unique_email):
    from app.models.conversation import Conversation, ConversationStatus

    tenant, product, customer = await _setup(db_session, unique_email)
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None,
        created_by="IA", conversation_id=conversation.id,
    )
    await db_session.commit()
    token = await _login(client, unique_email)

    await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers={"Authorization": f"Bearer {token}"})

    from sqlalchemy import select

    from app.models.conversation import Message

    receipt = (await db_session.execute(select(Message).where(Message.message_type == "receipt"))).scalar_one_or_none()
    assert receipt is not None
    assert "Reçu établi par Bob AI" in receipt.content


@pytest.mark.asyncio
async def test_mark_order_paid_works_for_lowest_role_agent(client, db_session, unique_email):
    """AGENT est le rôle minimum requis — vérifie qu'il suffit (pas besoin de MANAGER+)."""
    tenant, product, customer = await _setup(db_session, unique_email, role=Role.AGENT)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_mark_order_paid_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, product_a, customer_a = await _setup(db_session, unique_email)
    tenant_b, product_b, customer_b = await _setup(db_session, f"b_{unique_email}")

    order_a = await create_order(
        db=db_session, tenant_id=tenant_a.id, customer_id=customer_a.id,
        items=[{"product_id": str(product_a.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    token_b = await _login(client, f"b_{unique_email}")
    response = await client.put(f"/api/v1/orders/{order_a.id}/mark-paid", headers={"Authorization": f"Bearer {token_b}"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_mark_order_paid_twice_rejected(client, db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()
    token = await _login(client, unique_email)

    first = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200

    second = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers={"Authorization": f"Bearer {token}"})
    assert second.status_code == 400
