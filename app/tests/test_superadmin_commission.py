import pytest

from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.services.order_service import create_order


async def _bootstrap(client, email="admin@bob.internal"):
    response = await client.post(
        "/api/v1/superadmin/bootstrap", json={"email": email, "password": "supersecret123", "full_name": "Admin"}
    )
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_update_commission_rate(client, db_session, unique_email):
    token = await _bootstrap(client, "admin1@bob.internal")
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    response = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/commission-rate",
        json={"commission_rate": 7.5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["commission_rate"] == 7.5

    await db_session.refresh(tenant)
    assert float(tenant.commission_rate) == 7.5


@pytest.mark.asyncio
async def test_commission_rate_rejects_out_of_range(client, db_session, unique_email):
    token = await _bootstrap(client, "admin2@bob.internal")
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    response = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/commission-rate",
        json={"commission_rate": 150},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_tenant_detail_shows_total_commission_due(client, db_session, unique_email):
    token = await _bootstrap(client, "admin3@bob.internal")
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email, commission_rate=10.0)
    db_session.add(tenant)
    await db_session.flush()
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=100000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(tenant)
    await db_session.refresh(product)
    await db_session.refresh(customer)

    from app.services.order_service import mark_order_as_paid

    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()
    await mark_order_as_paid(db_session, tenant.id, order.id)
    await db_session.commit()

    response = await client.get(f"/api/v1/superadmin/tenants/{tenant.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["total_commission_due"] == 10000.0  # 10% de 100000
