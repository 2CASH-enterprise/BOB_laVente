import pytest

from app.core.security import hash_password
from app.integrations.ecommerce.dependency import get_meta_catalog_client_factory
from app.main import app
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.tests.fakes_meta_catalog import FakeMetaCatalogClient, make_meta_product


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
async def test_connect_meta_catalog_success(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    app.dependency_overrides[get_meta_catalog_client_factory] = lambda: (
        lambda catalog_id, access_token: FakeMetaCatalogClient(catalog_id, access_token)
    )
    try:
        response = await client.post(
            "/api/v1/integrations/meta-catalog/connect",
            json={"catalog_id": "catalog_123", "access_token": "tok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["shop_domain"] == "catalog_123"
    finally:
        del app.dependency_overrides[get_meta_catalog_client_factory]


@pytest.mark.asyncio
async def test_connect_meta_catalog_invalid_token_rejected(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    app.dependency_overrides[get_meta_catalog_client_factory] = lambda: (
        lambda catalog_id, access_token: FakeMetaCatalogClient(raise_on_connect=True)
    )
    try:
        response = await client.post(
            "/api/v1/integrations/meta-catalog/connect",
            json={"catalog_id": "catalog_123", "access_token": "bad"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
    finally:
        del app.dependency_overrides[get_meta_catalog_client_factory]


@pytest.mark.asyncio
async def test_sync_meta_catalog_creates_products_via_api(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    app.dependency_overrides[get_meta_catalog_client_factory] = lambda: (
        lambda catalog_id, access_token: FakeMetaCatalogClient(catalog_id, access_token)
    )
    try:
        await client.post(
            "/api/v1/integrations/meta-catalog/connect",
            json={"catalog_id": "catalog_123", "access_token": "tok"},
            headers=headers,
        )
        products = [make_meta_product("1", "Samsung A56", price="280000 XOF", retailer_id="SAM-A56")]
        app.dependency_overrides[get_meta_catalog_client_factory] = lambda: (
            lambda catalog_id, access_token: FakeMetaCatalogClient(catalog_id, access_token, products=products)
        )
        response = await client.post("/api/v1/integrations/meta-catalog/sync", headers=headers)
        assert response.status_code == 200
        assert response.json()["imported"] == 1

        products_response = await client.get("/api/v1/products", headers=headers)
        assert any(p["name"] == "Samsung A56" for p in products_response.json())
    finally:
        del app.dependency_overrides[get_meta_catalog_client_factory]


@pytest.mark.asyncio
async def test_shopify_and_meta_catalog_connections_are_independent(client, db_session, unique_email):
    """Un même tenant peut connecter Shopify ET Meta Catalog simultanément, sans collision."""
    from app.integrations.ecommerce.dependency import get_shopify_client_factory
    from app.tests.fakes_shopify import FakeShopifyClient

    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    app.dependency_overrides[get_shopify_client_factory] = lambda: (
        lambda shop_domain, access_token: FakeShopifyClient(shop_domain, access_token, currency="XOF")
    )
    app.dependency_overrides[get_meta_catalog_client_factory] = lambda: (
        lambda catalog_id, access_token: FakeMetaCatalogClient(catalog_id, access_token)
    )
    try:
        r1 = await client.post(
            "/api/v1/integrations/shopify/connect",
            json={"shop_domain": "test.myshopify.com", "access_token": "tok1"},
            headers=headers,
        )
        r2 = await client.post(
            "/api/v1/integrations/meta-catalog/connect",
            json={"catalog_id": "catalog_123", "access_token": "tok2"},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        status_shopify = await client.get("/api/v1/integrations/shopify/status", headers=headers)
        status_meta = await client.get("/api/v1/integrations/meta-catalog/status", headers=headers)
        assert status_shopify.json()["shop_domain"] == "test.myshopify.com"
        assert status_meta.json()["shop_domain"] == "catalog_123"
    finally:
        del app.dependency_overrides[get_shopify_client_factory]
        del app.dependency_overrides[get_meta_catalog_client_factory]
