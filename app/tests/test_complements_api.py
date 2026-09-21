import pytest

from app.core.security import hash_password
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, role: Role = Role.MANAGER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    main = Product(tenant_id=tenant.id, sku="MAIN", name="Produit principal", price=100000, currency="XOF", stock_quantity=5)
    comp = Product(tenant_id=tenant.id, sku="COMP", name="Accessoire", price=5000, currency="XOF", stock_quantity=10)
    db_session.add_all([main, comp])
    await db_session.commit()
    await db_session.refresh(main)
    await db_session.refresh(comp)
    return tenant, main, comp


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_add_and_list_complement(client, db_session, unique_email):
    tenant, main, comp = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        f"/api/v1/products/{main.id}/complements",
        json={"product_id": str(main.id), "complement_product_id": str(comp.id)},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["complement_name"] == "Accessoire"

    listing = await client.get(f"/api/v1/products/{main.id}/complements", headers=headers)
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_cannot_link_product_to_itself(client, db_session, unique_email):
    tenant, main, comp = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.post(
        f"/api/v1/products/{main.id}/complements",
        json={"product_id": str(main.id), "complement_product_id": str(main.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_link_rejected(client, db_session, unique_email):
    tenant, main, comp = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"product_id": str(main.id), "complement_product_id": str(comp.id)}

    first = await client.post(f"/api/v1/products/{main.id}/complements", json=payload, headers=headers)
    assert first.status_code == 201
    second = await client.post(f"/api/v1/products/{main.id}/complements", json=payload, headers=headers)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_delete_complement(client, db_session, unique_email):
    tenant, main, comp = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        f"/api/v1/products/{main.id}/complements",
        json={"product_id": str(main.id), "complement_product_id": str(comp.id)},
        headers=headers,
    )
    link_id = created.json()["id"]

    response = await client.delete(f"/api/v1/products/complements/{link_id}", headers=headers)
    assert response.status_code == 204

    listing = await client.get(f"/api/v1/products/{main.id}/complements", headers=headers)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_complements_require_manager_role(client, db_session, unique_email):
    tenant, main, comp = await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.post(
        f"/api/v1/products/{main.id}/complements",
        json={"product_id": str(main.id), "complement_product_id": str(comp.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
