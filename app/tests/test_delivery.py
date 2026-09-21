import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.order_service import create_order


async def _setup(db_session, email: str, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="Samsung A56", price=280000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(product)
    await db_session.refresh(customer)
    await db_session.refresh(conversation)
    return tenant, product, customer, conversation


@pytest.mark.asyncio
async def test_order_creation_auto_creates_delivery(db_session, unique_email):
    tenant, product, customer, conversation = await _setup(db_session, unique_email)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address="Dakar", payment_method=None, created_by="IA",
    )
    await db_session.commit()

    from sqlalchemy import select

    delivery = (await db_session.execute(select(Delivery).where(Delivery.order_id == order.id))).scalar_one()
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.address == "Dakar"


@pytest.mark.asyncio
async def test_check_order_status_tool_returns_real_data(db_session, unique_email):
    tenant, product, customer, conversation = await _setup(db_session, unique_email)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 2}], delivery_address="Dakar", payment_method=None, created_by="IA",
    )
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("check_order_status", {})
    assert result["order_id"] == str(order.id)
    assert result["delivery_status"] == "PENDING"
    assert result["items"][0]["name"] == "Samsung A56"
    assert result["items"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_check_order_status_no_order_returns_error(db_session, unique_email):
    tenant, product, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("check_order_status", {})
    assert "error" in result


@pytest.mark.asyncio
async def test_check_order_status_isolated_by_customer(db_session, unique_email):
    """Un client ne doit jamais voir la commande d'un autre client, même dans le même tenant."""
    tenant, product, customer_a, conversation_a = await _setup(db_session, unique_email)
    customer_b = Customer(tenant_id=tenant.id, whatsapp_number="221700000099")
    db_session.add(customer_b)
    await db_session.flush()
    conversation_b = Conversation(tenant_id=tenant.id, customer_id=customer_b.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation_b)
    await db_session.commit()

    await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer_a.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    executor_b = ToolExecutor(db_session, tenant.id, conversation_b)
    result = await executor_b.execute("check_order_status", {})
    assert "error" in result  # le client B n'a pas de commande, même si le client A en a une


@pytest.mark.asyncio
async def test_update_delivery_status_via_api(client, db_session, unique_email):
    tenant, product, customer, conversation = await _setup(db_session, unique_email)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address="Dakar", payment_method=None, created_by="IA",
    )
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    token = login.json()["access_token"]

    response = await client.put(
        f"/api/v1/orders/{order.id}/delivery",
        json={"status": "IN_TRANSIT", "tracking_number": "TRACK123", "provider": "DHL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "IN_TRANSIT"
    assert response.json()["tracking_number"] == "TRACK123"


@pytest.mark.asyncio
async def test_delivery_update_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, product_a, customer_a, conversation_a = await _setup(db_session, unique_email)
    tenant_b, product_b, customer_b, conversation_b = await _setup(db_session, f"b_{unique_email}")

    order_a = await create_order(
        db=db_session, tenant_id=tenant_a.id, customer_id=customer_a.id,
        items=[{"product_id": str(product_a.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    login_b = await client.post("/api/v1/auth/login", data={"username": f"b_{unique_email}", "password": "x"})
    token_b = login_b.json()["access_token"]

    response = await client.put(
        f"/api/v1/orders/{order_a.id}/delivery",
        json={"status": "DELIVERED"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 404  # le tenant B ne peut pas toucher à la commande du tenant A
