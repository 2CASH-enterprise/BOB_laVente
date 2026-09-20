import pytest


@pytest.mark.asyncio
async def test_register_tenant_creates_owner(client, unique_email):
    payload = {
        "company_name": "Boutique Test",
        "country": "SN",
        "currency": "XOF",
        "phone": "+221700000000",
        "owner_email": unique_email,
        "owner_full_name": "Awa Diop",
        "owner_password": "supersecret123",
    }
    response = await client.post("/api/v1/auth/register-tenant", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert "tenant_id" in body
    assert "owner_user_id" in body


@pytest.mark.asyncio
async def test_register_duplicate_email_rejected(client, unique_email):
    payload = {
        "company_name": "Boutique Test",
        "country": "SN",
        "currency": "XOF",
        "owner_email": unique_email,
        "owner_full_name": "Awa Diop",
        "owner_password": "supersecret123",
    }
    first = await client.post("/api/v1/auth/register-tenant", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register-tenant", json=payload)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_login_success_and_wrong_password(client, unique_email):
    register_payload = {
        "company_name": "Boutique Test",
        "country": "SN",
        "currency": "XOF",
        "owner_email": unique_email,
        "owner_full_name": "Awa Diop",
        "owner_password": "supersecret123",
    }
    await client.post("/api/v1/auth/register-tenant", json=register_payload)

    ok = await client.post(
        "/api/v1/auth/login", data={"username": unique_email, "password": "supersecret123"}
    )
    assert ok.status_code == 200
    assert "access_token" in ok.json()

    wrong = await client.post(
        "/api/v1/auth/login", data={"username": unique_email, "password": "wrong-password"}
    )
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_authentication(client):
    response = await client.get("/api/v1/tenants/me")
    assert response.status_code == 401
