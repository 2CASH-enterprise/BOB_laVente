import pytest

from app.core.security import hash_password
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.plan_limits import FREEMIUM_MAX_PRODUCTS


async def _setup(db_session, email: str, is_paid: bool = False, role: Role = Role.OWNER):
    tenant = Tenant(
        name="Boutique", country="SN", currency="XOF", email=email,
        plan=TenantPlan.INDEPENDANT if is_paid else TenantPlan.FREE,
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_product_creation_blocked_at_freemium_limit(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    for i in range(FREEMIUM_MAX_PRODUCTS):
        db_session.add(Product(tenant_id=tenant.id, sku=f"P{i}", name=f"P{i}", price=1000, currency="XOF", stock_quantity=1))
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/products",
        json={"sku": "OVERFLOW", "name": "Trop", "price": "1000", "currency": "XOF", "stock_quantity": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_product_creation_unlimited_for_paid(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=True)
    for i in range(FREEMIUM_MAX_PRODUCTS):
        db_session.add(Product(tenant_id=tenant.id, sku=f"P{i}", name=f"P{i}", price=1000, currency="XOF", stock_quantity=1))
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/products",
        json={"sku": "MORE", "name": "Encore un", "price": "1000", "currency": "XOF", "stock_quantity": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_negotiation_blocked_for_freemium_even_if_enabled(db_session, unique_email):
    from app.agents.tools import ToolExecutor
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.customer import Customer
    from app.models.negotiation_settings import TenantNegotiationSettings

    tenant = await _setup(db_session, unique_email, is_paid=False)
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=100000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    db_session.add(TenantNegotiationSettings(tenant_id=tenant.id, enabled=True))
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(product)

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("negotiate_price", {"product_id": str(product.id), "customer_offer": 50000})
    assert "error" in result
    assert "payant" in result["error"].lower()


@pytest.mark.asyncio
async def test_shopify_connect_blocked_for_freemium(client, db_session, unique_email):
    await _setup(db_session, unique_email, is_paid=False)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/integrations/shopify/connect",
        json={"shop_domain": "x.myshopify.com", "access_token": "tok"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_followups_never_sent_for_freemium(db_session, unique_email):
    from datetime import datetime, timedelta, timezone

    from app.models.conversation import Conversation, ConversationStatus
    from app.models.customer import Customer
    from app.models.followup_settings import TenantFollowupSettings
    from app.services.followup_service import run_followups_for_tenant

    tenant = await _setup(db_session, unique_email, is_paid=False)
    db_session.add(TenantFollowupSettings(tenant_id=tenant.id, enabled=True, first_followup_hours=1))
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(
        Conversation(
            tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
            last_message_at=datetime.now(timezone.utc) - timedelta(hours=100),
        )
    )
    await db_session.commit()

    sent = await run_followups_for_tenant(db_session, tenant.id)
    assert sent == 0
