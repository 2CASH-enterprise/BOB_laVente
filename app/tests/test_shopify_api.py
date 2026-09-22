import pytest

from app.core.security import hash_password
from app.integrations.ecommerce.dependency import get_shopify_client_factory
from app.main import app
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.tests.fakes_shopify import FakeShopifyClient, make_shopify_product


async def _setup(db_session, email: str, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    await db_session.commit()
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_connect_shopify_success(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    app.dependency_overrides[get_shopify_client_factory] = lambda: (
        lambda shop_domain, access_token: FakeShopifyClient(shop_domain, access_token, currency="EUR")
    )
    try:
        response = await client.post(
            "/api/v1/integrations/shopify/connect",
            json={"shop_domain": "test.myshopify.com", "access_token": "tok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["currency"] == "EUR"
    finally:
        del app.dependency_overrides[get_shopify_client_factory]


@pytest.mark.asyncio
async def test_connect_shopify_invalid_credentials_rejected(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    app.dependency_overrides[get_shopify_client_factory] = lambda: (
        lambda shop_domain, access_token: FakeShopifyClient(raise_on_connect=True)
    )
    try:
        response = await client.post(
            "/api/v1/integrations/shopify/connect",
            json={"shop_domain": "test.myshopify.com", "access_token": "bad"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
    finally:
        del app.dependency_overrides[get_shopify_client_factory]


@pytest.mark.asyncio
async def test_connect_shopify_requires_admin_role(client, db_session, unique_email):
    await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/integrations/shopify/connect",
        json={"shop_domain": "test.myshopify.com", "access_token": "tok"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_sync_shopify_creates_products_via_api(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    products = [make_shopify_product(1, "Samsung A56", "280000", sku="SAM-A56", inventory_quantity=5)]
    app.dependency_overrides[get_shopify_client_factory] = lambda: (
        lambda shop_domain, access_token: FakeShopifyClient(shop_domain, access_token, currency="XOF")
    )
    try:
        await client.post(
            "/api/v1/integrations/shopify/connect",
            json={"shop_domain": "test.myshopify.com", "access_token": "tok"},
            headers=headers,
        )
        # On surcharge à nouveau avec les produits pour l'étape de synchronisation
        app.dependency_overrides[get_shopify_client_factory] = lambda: (
            lambda shop_domain, access_token: FakeShopifyClient(shop_domain, access_token, products=products, currency="XOF")
        )
        response = await client.post("/api/v1/integrations/shopify/sync", headers=headers)
        assert response.status_code == 200
        assert response.json()["imported"] == 1

        products_response = await client.get("/api/v1/products", headers=headers)
        assert any(p["name"] == "Samsung A56" for p in products_response.json())
    finally:
        del app.dependency_overrides[get_shopify_client_factory]


@pytest.mark.asyncio
async def test_sync_without_connection_returns_404(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.post("/api/v1/integrations/shopify/sync", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_shopify_status_isolated_by_tenant(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    await _setup(db_session, f"b_{unique_email}")

    token_b = await _login(client, f"b_{unique_email}")
    response = await client.get(
        "/api/v1/integrations/shopify/status", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response.status_code == 404  # le tenant B n'a rien connecté, même si A l'a fait ailleurs
