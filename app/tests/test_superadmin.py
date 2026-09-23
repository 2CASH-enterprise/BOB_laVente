import pytest

from app.core.security import hash_password
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan


async def _bootstrap(client, email="admin@bob.internal", password="supersecret123"):
    response = await client.post(
        "/api/v1/superadmin/bootstrap", json={"email": email, "password": password, "full_name": "Admin"}
    )
    return response


@pytest.mark.asyncio
async def test_bootstrap_creates_first_superadmin(client, db_session):
    response = await _bootstrap(client)
    assert response.status_code == 200
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_bootstrap_fails_if_already_one_exists(client, db_session):
    await _bootstrap(client)
    second = await _bootstrap(client, email="autre@bob.internal")
    assert second.status_code == 403


@pytest.mark.asyncio
async def test_superadmin_login_works_after_bootstrap(client, db_session):
    await _bootstrap(client, email="admin2@bob.internal", password="supersecret123")
    response = await client.post(
        "/api/v1/superadmin/login", data={"username": "admin2@bob.internal", "password": "supersecret123"}
    )
    assert response.status_code == 200
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_superadmin_login_wrong_password_rejected(client, db_session):
    await _bootstrap(client, email="admin3@bob.internal", password="supersecret123")
    response = await client.post(
        "/api/v1/superadmin/login", data={"username": "admin3@bob.internal", "password": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_tenant_token_cannot_access_superadmin_routes(client, db_session, unique_email):
    """Vérification critique : un token tenant normal ne doit JAMAIS donner accès au Super Admin."""
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    from app.models.user import Role, User

    db_session.add(
        User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    tenant_token = login.json()["access_token"]

    response = await client.get("/api/v1/superadmin/tenants", headers={"Authorization": f"Bearer {tenant_token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_superadmin_token_cannot_access_tenant_routes(client, db_session):
    """Et inversement : un token Super Admin ne doit jamais donner accès aux routes tenant."""
    bootstrap = await _bootstrap(client, email="admin4@bob.internal")
    superadmin_token = bootstrap.json()["access_token"]

    response = await client.get("/api/v1/products", headers={"Authorization": f"Bearer {superadmin_token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_tenants_shows_all_tenants(client, db_session, unique_email):
    bootstrap = await _bootstrap(client, email="admin5@bob.internal")
    token = bootstrap.json()["access_token"]

    tenant_a = Tenant(name="Boutique A", country="SN", currency="XOF", email=f"a_{unique_email}")
    tenant_b = Tenant(name="Boutique B", country="SN", currency="XOF", email=f"b_{unique_email}", plan=TenantPlan.PRO)
    db_session.add_all([tenant_a, tenant_b])
    await db_session.commit()

    response = await client.get("/api/v1/superadmin/tenants", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    names = [t["name"] for t in response.json()]
    assert "Boutique A" in names
    assert "Boutique B" in names


@pytest.mark.asyncio
async def test_update_tenant_plan(client, db_session, unique_email):
    bootstrap = await _bootstrap(client, email="admin6@bob.internal")
    token = bootstrap.json()["access_token"]

    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    response = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/plan", json={"plan": "PRO"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["plan"] == "PRO"

    await db_session.refresh(tenant)
    assert tenant.plan == TenantPlan.PRO


@pytest.mark.asyncio
async def test_update_tenant_plan_invalid_value_rejected(client, db_session, unique_email):
    bootstrap = await _bootstrap(client, email="admin7@bob.internal")
    token = bootstrap.json()["access_token"]

    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    response = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/plan", json={"plan": "N_IMPORTE_QUOI"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_suspend_and_reactivate_tenant(client, db_session, unique_email):
    bootstrap = await _bootstrap(client, email="admin8@bob.internal")
    token = bootstrap.json()["access_token"]

    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    suspend = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/active", json={"active": False}, headers={"Authorization": f"Bearer {token}"}
    )
    assert suspend.json()["active"] is False

    reactivate = await client.put(
        f"/api/v1/superadmin/tenants/{tenant.id}/active", json={"active": True}, headers={"Authorization": f"Bearer {token}"}
    )
    assert reactivate.json()["active"] is True


@pytest.mark.asyncio
async def test_platform_stats_reflect_real_data(client, db_session, unique_email):
    bootstrap = await _bootstrap(client, email="admin9@bob.internal")
    token = bootstrap.json()["access_token"]

    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email, plan=TenantPlan.STARTER)
    db_session.add(tenant)
    await db_session.commit()

    response = await client.get("/api/v1/superadmin/stats", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["total_tenants"] >= 1
    assert response.json()["tenants_by_plan"].get("STARTER", 0) >= 1


@pytest.mark.asyncio
async def test_create_colleague_requires_existing_superadmin_auth(client, db_session):
    response = await client.post(
        "/api/v1/superadmin/create-colleague",
        json={"email": "colleague@bob.internal", "password": "supersecret123", "full_name": "Collègue"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_colleague_works_when_authenticated(client, db_session):
    bootstrap = await _bootstrap(client, email="admin10@bob.internal")
    token = bootstrap.json()["access_token"]

    response = await client.post(
        "/api/v1/superadmin/create-colleague",
        json={"email": "colleague2@bob.internal", "password": "supersecret123", "full_name": "Collègue"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_token_rejected(client, db_session):
    response = await client.get("/api/v1/superadmin/tenants")
    assert response.status_code == 401
