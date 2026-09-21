import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.MANAGER)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(customer)
    await db_session.refresh(product)
    return tenant, customer, product


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_analytics_empty_tenant_returns_zeros(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/analytics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_conversations"] == 0
    assert body["total_orders"] == 0
    assert body["revenue"] == 0
    assert body["conversion_rate_pct"] == 0
    assert body["human_handoffs"] == 0


@pytest.mark.asyncio
async def test_analytics_counts_orders_and_revenue(client, db_session, unique_email):
    tenant, customer, product = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/api/v1/orders",
        json={"customer_id": str(customer.id), "items": [{"product_id": str(product.id), "quantity": 2}]},
        headers=headers,
    )

    response = await client.get("/api/v1/analytics", headers=headers)
    body = response.json()
    assert body["total_orders"] == 1
    assert body["revenue"] == 560000.0
    assert body["currency"] == "XOF"


@pytest.mark.asyncio
async def test_analytics_counts_human_handoffs_from_ai_tool(client, db_session, unique_email):
    from app.agents.tools import ToolExecutor
    from app.core.security import create_access_token
    from app.models.conversation import Conversation, ConversationStatus
    from sqlalchemy import select
    from app.models.user import User

    tenant, customer, product = await _setup(db_session, unique_email)
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)

    executor = ToolExecutor(db_session, tenant.id, conversation)
    await executor.execute("handoff_to_human", {"reason": "Client mécontent"})
    await db_session.commit()

    user = (await db_session.execute(select(User).where(User.tenant_id == tenant.id))).scalars().first()
    token = create_access_token(user_id=user.id, tenant_id=tenant.id, role=user.role.value)

    response = await client.get("/api/v1/analytics", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["human_handoffs"] == 1


@pytest.mark.asyncio
async def test_analytics_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, customer_a, product_a = await _setup(db_session, unique_email)
    tenant_b, customer_b, product_b = await _setup(db_session, f"b_{unique_email}")

    token_a = await _login(client, unique_email)
    await client.post(
        "/api/v1/orders",
        json={"customer_id": str(customer_a.id), "items": [{"product_id": str(product_a.id), "quantity": 1}]},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    token_b = await _login(client, f"b_{unique_email}")
    response_b = await client.get("/api/v1/analytics", headers={"Authorization": f"Bearer {token_b}"})
    assert response_b.json()["total_orders"] == 0  # ne voit rien du tenant A
