import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
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
async def test_get_negotiation_settings_creates_defaults(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/tenants/me/negotiation-settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["max_discount_pct"] == 10.0
    assert response.json()["max_rounds"] == 3


@pytest.mark.asyncio
async def test_update_negotiation_settings(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.put(
        "/api/v1/tenants/me/negotiation-settings", json={"max_discount_pct": 15, "max_rounds": 2}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["max_discount_pct"] == 15.0
    assert response.json()["max_rounds"] == 2


@pytest.mark.asyncio
async def test_update_negotiation_settings_requires_admin(client, db_session, unique_email):
    await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.put(
        "/api/v1/tenants/me/negotiation-settings", json={"max_discount_pct": 20}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
