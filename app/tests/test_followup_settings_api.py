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
async def test_get_followup_settings_creates_defaults(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/tenants/me/followup-settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["first_followup_hours"] == 24
    assert body["second_followup_hours"] == 72


@pytest.mark.asyncio
async def test_update_followup_settings(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.put(
        "/api/v1/tenants/me/followup-settings",
        json={"enabled": True, "first_followup_hours": 12},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["first_followup_hours"] == 12


@pytest.mark.asyncio
async def test_update_followup_settings_requires_admin(client, db_session, unique_email):
    await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.put(
        "/api/v1/tenants/me/followup-settings", json={"enabled": True}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
