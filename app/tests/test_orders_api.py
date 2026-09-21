import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, role: Role = Role.AGENT):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)
    return tenant, customer


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_create_order_via_dashboard(client, db_session, unique_email):
    from app.models.product import Product

    tenant, customer = await _setup(db_session, unique_email)
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/orders",
        json={"customer_id": str(customer.id), "items": [{"product_id": str(product.id), "quantity": 3}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    body = response.json()
    assert float(body["total_amount"]) == 840000.0
    assert len(body["items"]) == 1
    assert body["created_by"] != "IA"  # créée par un humain identifié


@pytest.mark.asyncio
async def test_order_insufficient_stock_returns_400(client, db_session, unique_email):
    from app.models.product import Product

    tenant, customer = await _setup(db_session, unique_email)
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=1000, currency="XOF", stock_quantity=1)
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    token = await _login(client, unique_email)
    response = await client.post(
        "/api/v1/orders",
        json={"customer_id": str(customer.id), "items": [{"product_id": str(product.id), "quantity": 5}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_orders_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, customer_a = await _setup(db_session, unique_email)
    tenant_b, customer_b = await _setup(db_session, f"b_{unique_email}")

    token_b = await _login(client, f"b_{unique_email}")
    response = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token_b}"})
    assert response.status_code == 200
    assert response.json() == []
