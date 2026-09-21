import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup_tenant(db_session, email: str, role: Role = Role.MANAGER) -> Tenant:
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("secret123456"),
        full_name="User",
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    return tenant


async def _login(client, email: str) -> str:
    response = await client.post("/api/v1/auth/login", data={"username": email, "password": "secret123456"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_create_and_get_product(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/products",
        json={"sku": "SAM-A56", "name": "Samsung A56", "price": "280000", "currency": "XOF", "stock_quantity": 12},
        headers=headers,
    )
    assert create.status_code == 201
    product_id = create.json()["id"]

    get_response = await client.get(f"/api/v1/products/{product_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Samsung A56"
    assert get_response.json()["stock_quantity"] == 12


@pytest.mark.asyncio
async def test_duplicate_sku_rejected(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    payload = {"sku": "SAM-A56", "name": "Samsung A56", "price": "280000", "currency": "XOF"}
    first = await client.post("/api/v1/products", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post("/api/v1/products", json=payload, headers=headers)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_search_by_name_and_price(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(
        "/api/v1/products",
        json={"sku": "SAM-A56", "name": "Samsung A56", "price": "280000", "currency": "XOF"},
        headers=headers,
    )
    await client.post(
        "/api/v1/products",
        json={"sku": "SAM-A36", "name": "Samsung A36", "price": "220000", "currency": "XOF"},
        headers=headers,
    )
    await client.post(
        "/api/v1/products",
        json={"sku": "IPH-15", "name": "iPhone 15", "price": "650000", "currency": "XOF"},
        headers=headers,
    )

    response = await client.get("/api/v1/products", params={"query": "Samsung", "max_price": 300000}, headers=headers)
    assert response.status_code == 200
    names = {p["name"] for p in response.json()}
    assert names == {"Samsung A56", "Samsung A36"}

    response_budget = await client.get(
        "/api/v1/products", params={"query": "Samsung", "max_price": 250000}, headers=headers
    )
    names_budget = {p["name"] for p in response_budget.json()}
    assert names_budget == {"Samsung A36"}


@pytest.mark.asyncio
async def test_product_search_isolated_by_tenant(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    await _setup_tenant(db_session, f"b_{unique_email}")

    token_a = await _login(client, unique_email)
    token_b = await _login(client, f"b_{unique_email}")

    await client.post(
        "/api/v1/products",
        json={"sku": "ONLY-A", "name": "Produit tenant A", "price": "1000", "currency": "XOF"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    response_b = await client.get("/api/v1/products", headers={"Authorization": f"Bearer {token_b}"})
    assert response_b.status_code == 200
    assert response_b.json() == []  # le tenant B ne voit RIEN du catalogue du tenant A


@pytest.mark.asyncio
async def test_viewer_cannot_create_product(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email, role=Role.VIEWER)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/products",
        json={"sku": "X", "name": "X", "price": "10", "currency": "XOF"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
