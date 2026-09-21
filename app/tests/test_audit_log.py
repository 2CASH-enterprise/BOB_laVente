import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog


@pytest.mark.asyncio
async def test_register_tenant_creates_audit_log(client, db_session, unique_email):
    payload = {
        "company_name": "Boutique Test",
        "country": "SN",
        "currency": "XOF",
        "owner_email": unique_email,
        "owner_full_name": "Test",
        "owner_password": "supersecret123",
    }
    await client.post("/api/v1/auth/register-tenant", json=payload)

    result = await db_session.execute(select(AuditLog).where(AuditLog.action == "TENANT_REGISTERED"))
    logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].details["owner_email"] == unique_email


@pytest.mark.asyncio
async def test_login_success_and_failure_create_audit_logs(client, db_session, unique_email):
    payload = {
        "company_name": "Boutique Test",
        "country": "SN",
        "currency": "XOF",
        "owner_email": unique_email,
        "owner_full_name": "Test",
        "owner_password": "supersecret123",
    }
    await client.post("/api/v1/auth/register-tenant", json=payload)

    await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "wrong"})
    await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "supersecret123"})

    failed = (await db_session.execute(select(AuditLog).where(AuditLog.action == "LOGIN_FAILED"))).scalars().all()
    success = (await db_session.execute(select(AuditLog).where(AuditLog.action == "LOGIN_SUCCESS"))).scalars().all()
    assert len(failed) == 1
    assert len(success) == 1


@pytest.mark.asyncio
async def test_takeover_and_release_create_audit_logs(client, db_session, unique_email):
    from app.core.security import hash_password
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.customer import Customer
    from app.models.tenant import Tenant
    from app.models.user import Role, User

    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.AGENT)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(f"/api/v1/conversations/{conversation.id}/takeover", headers=headers)
    await client.post(f"/api/v1/conversations/{conversation.id}/release", headers=headers)

    takeover_logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "CONVERSATION_TAKEOVER"))).scalars().all()
    release_logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "CONVERSATION_RELEASE"))).scalars().all()
    assert len(takeover_logs) == 1
    assert len(release_logs) == 1
    assert takeover_logs[0].tenant_id == tenant.id


@pytest.mark.asyncio
async def test_order_creation_creates_audit_log(client, db_session, unique_email):
    from app.core.security import hash_password
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.tenant import Tenant
    from app.models.user import Role, User

    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.AGENT)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=1000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(customer)
    await db_session.refresh(product)

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    token = login.json()["access_token"]

    await client.post(
        "/api/v1/orders",
        json={"customer_id": str(customer.id), "items": [{"product_id": str(product.id), "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "ORDER_CREATED"))).scalars().all()
    assert len(logs) == 1
