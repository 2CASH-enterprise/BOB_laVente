import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.order_service import create_order


async def _setup(db_session, email: str, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
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
async def test_new_customer_has_status_prospect_with_conversation(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE))
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()[0]["commercial_status"] == "PROSPECT"


@pytest.mark.asyncio
async def test_customer_with_order_has_status_client(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=1000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    await db_session.refresh(product)
    await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token}"})
    assert response.json()[0]["commercial_status"] == "CLIENT"
    assert response.json()[0]["order_count"] == 1


@pytest.mark.asyncio
async def test_customer_detail_includes_latest_conversation(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conv = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(customer)
    token = await _login(client, unique_email)

    response = await client.get(f"/api/v1/customers/{customer.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["latest_conversation_id"] == str(conv.id)


@pytest.mark.asyncio
async def test_update_customer_notes_and_consent(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)
    token = await _login(client, unique_email)

    response = await client.put(
        f"/api/v1/customers/{customer.id}",
        json={"notes": "Client difficile mais fidèle", "marketing_consent": True, "tags": ["vip"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["notes"] == "Client difficile mais fidèle"
    assert response.json()["marketing_consent"] is True
    assert response.json()["tags"] == ["vip"]


@pytest.mark.asyncio
async def test_customers_isolated_by_tenant(client, db_session, unique_email):
    tenant_a = await _setup(db_session, unique_email)
    tenant_b = await _setup(db_session, f"b_{unique_email}")

    customer_a = Customer(tenant_id=tenant_a.id, whatsapp_number="221700000000")
    db_session.add(customer_a)
    await db_session.commit()

    token_b = await _login(client, f"b_{unique_email}")
    listing_b = await client.get("/api/v1/customers", headers={"Authorization": f"Bearer {token_b}"})
    assert listing_b.json() == []

    detail_b = await client.get(f"/api/v1/customers/{customer_a.id}", headers={"Authorization": f"Bearer {token_b}"})
    assert detail_b.status_code == 404


@pytest.mark.asyncio
async def test_customer_not_found_returns_404(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    import uuid

    response = await client.get(f"/api/v1/customers/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
