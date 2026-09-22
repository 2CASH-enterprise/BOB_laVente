import io

import pytest

SAMPLE_CSV = b"""Nom,Prix,Description,Stock
Samsung A56,280000,256 Go,12
"""


async def _create_demo(client):
    files = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    response = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique Démo", "currency": "XOF"}, files=files
    )
    return response.json()


@pytest.mark.asyncio
async def test_promote_demo_keeps_catalog(client, db_session):
    demo = await _create_demo(client)
    demo_token = demo["demo_token"]

    response = await client.post(
        "/api/v1/demo/promote",
        json={"email": "nouveau@example.com", "password": "supersecret123"},
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert response.status_code == 200
    new_token = response.json()["access_token"]

    products = await client.get("/api/v1/products", headers={"Authorization": f"Bearer {new_token}"})
    assert len(products.json()) == 1
    assert products.json()[0]["name"] == "Samsung A56"


@pytest.mark.asyncio
async def test_promote_demo_flips_is_demo_flag(client, db_session):
    demo = await _create_demo(client)
    demo_token = demo["demo_token"]
    tenant_id = demo["tenant_id"]

    await client.post(
        "/api/v1/demo/promote",
        json={"email": "flag@example.com", "password": "supersecret123"},
        headers={"Authorization": f"Bearer {demo_token}"},
    )

    from sqlalchemy import select

    from app.models.tenant import Tenant

    import uuid as uuid_module

    tenant = (await db_session.execute(select(Tenant).where(Tenant.id == uuid_module.UUID(tenant_id)))).scalar_one()
    assert tenant.is_demo is False


@pytest.mark.asyncio
async def test_promote_demo_allows_login_with_new_credentials(client, db_session):
    demo = await _create_demo(client)
    demo_token = demo["demo_token"]

    await client.post(
        "/api/v1/demo/promote",
        json={"email": "login-test@example.com", "password": "supersecret123"},
        headers={"Authorization": f"Bearer {demo_token}"},
    )

    login = await client.post(
        "/api/v1/auth/login", data={"username": "login-test@example.com", "password": "supersecret123"}
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_promote_demo_rejects_duplicate_email(client, db_session, unique_email):
    from app.core.security import hash_password
    from app.models.tenant import Tenant
    from app.models.user import Role, User

    other_tenant = Tenant(name="Autre", country="SN", currency="XOF", email=unique_email)
    db_session.add(other_tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=other_tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    await db_session.commit()

    demo = await _create_demo(client)
    demo_token = demo["demo_token"]

    response = await client.post(
        "/api/v1/demo/promote",
        json={"email": unique_email, "password": "supersecret123"},
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_promote_already_real_tenant_rejected(client, db_session, unique_email):
    from app.core.security import hash_password
    from app.models.tenant import Tenant
    from app.models.user import Role, User
    from app.core.security import create_access_token

    tenant = Tenant(name="Réel", country="SN", currency="XOF", email=unique_email, is_demo=False)
    db_session.add(tenant)
    await db_session.flush()
    user = User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=tenant.id, role=user.role.value)
    response = await client.post(
        "/api/v1/demo/promote",
        json={"email": "x@example.com", "password": "supersecret123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_promote_rejects_short_password(client, db_session):
    demo = await _create_demo(client)
    demo_token = demo["demo_token"]

    response = await client.post(
        "/api/v1/demo/promote",
        json={"email": "short@example.com", "password": "123"},
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert response.status_code == 400
