import pytest

from app.core.security import hash_password
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount


async def _setup(db_session, email: str, role: Role = Role.OWNER, with_phone=True):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="Samsung A56", price=280000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    if with_phone:
        db_session.add(
            WhatsAppAccount(
                tenant_id=tenant.id, waba_id=f"w-{email}", phone_number_id=f"p-{email}",
                system_user_token="t", display_phone_number="221771234567",
            )
        )
    await db_session.commit()
    await db_session.refresh(tenant)
    await db_session.refresh(product)
    return tenant, product


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_create_and_list_qr_code(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
    assert created.status_code == 201
    assert created.json()["product_name"] == "Samsung A56"

    listing = await client.get("/api/v1/qr-codes", headers=headers)
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_qr_code_limit_enforced(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(5):
        r = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
        assert r.status_code == 201

    sixth = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
    assert sixth.status_code == 403


@pytest.mark.asyncio
async def test_qr_redirect_increments_scan_count_and_redirects_to_whatsapp(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
    code = created.json()["code"]

    response = await client.get(f"/qr/{code}", follow_redirects=False)
    assert response.status_code == 302
    assert "wa.me/221771234567" in response.headers["location"]
    assert "Samsung" in response.headers["location"]

    listing = await client.get("/api/v1/qr-codes", headers=headers)
    assert listing.json()[0]["scan_count"] == 1


@pytest.mark.asyncio
async def test_qr_redirect_unknown_code_404(client, db_session):
    response = await client.get("/qr/doesnotexist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_qr_redirect_fails_gracefully_without_display_number(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email, with_phone=False)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
    code = created.json()["code"]

    response = await client.get(f"/qr/{code}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_qr_code(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/api/v1/qr-codes", json={"product_id": str(product.id)}, headers=headers)
    qr_id = created.json()["id"]

    response = await client.delete(f"/api/v1/qr-codes/{qr_id}", headers=headers)
    assert response.status_code == 204

    listing = await client.get("/api/v1/qr-codes", headers=headers)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_qr_codes_require_manager_role(client, db_session, unique_email):
    tenant, product = await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/qr-codes", json={"product_id": str(product.id)}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_qr_codes_isolated_by_tenant(client, db_session, unique_email):
    tenant_a, product_a = await _setup(db_session, unique_email)
    tenant_b, product_b = await _setup(db_session, f"b_{unique_email}")

    token_a = await _login(client, unique_email)
    await client.post("/api/v1/qr-codes", json={"product_id": str(product_a.id)}, headers={"Authorization": f"Bearer {token_a}"})

    token_b = await _login(client, f"b_{unique_email}")
    listing_b = await client.get("/api/v1/qr-codes", headers={"Authorization": f"Bearer {token_b}"})
    assert listing_b.json() == []
