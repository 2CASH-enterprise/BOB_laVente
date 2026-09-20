import uuid

import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository


async def _create_tenant_with_owner(db_session, email: str) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    owner = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("secret123456"),
        full_name="Owner",
        role=Role.OWNER,
    )
    db_session.add(owner)
    await db_session.commit()
    return tenant, owner


@pytest.mark.asyncio
async def test_get_my_tenant_only_returns_own_tenant(client, db_session, unique_email):
    email_a = unique_email
    email_b = f"other_{unique_email}"

    tenant_a, _ = await _create_tenant_with_owner(db_session, email_a)
    tenant_b, _ = await _create_tenant_with_owner(db_session, email_b)

    login_a = await client.post("/api/v1/auth/login", data={"username": email_a, "password": "secret123456"})
    token_a = login_a.json()["access_token"]

    response = await client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token_a}"})
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == str(tenant_a.id)
    assert body["id"] != str(tenant_b.id)


@pytest.mark.asyncio
async def test_repository_never_returns_another_tenants_record(db_session, unique_email):
    """
    Vérifie au niveau repository (pas seulement HTTP) qu'un tenant A ne peut jamais
    récupérer un enregistrement appartenant au tenant B, même en connaissant son id.
    """
    tenant_a, user_a = await _create_tenant_with_owner(db_session, unique_email)
    tenant_b, user_b = await _create_tenant_with_owner(db_session, f"b_{unique_email}")

    repo = UserRepository(db_session)

    # Le tenant A ne doit jamais pouvoir lire user_b via son propre tenant_id.
    result = await repo.get(tenant_id=tenant_a.id, record_id=user_b.id)
    assert result is None

    # Mais peut bien lire ses propres données.
    own = await repo.get(tenant_id=tenant_a.id, record_id=user_a.id)
    assert own is not None
    assert own.id == user_a.id


@pytest.mark.asyncio
async def test_random_tenant_id_never_matches(db_session, unique_email):
    tenant_a, user_a = await _create_tenant_with_owner(db_session, unique_email)
    repo = UserRepository(db_session)

    result = await repo.get(tenant_id=uuid.uuid4(), record_id=user_a.id)
    assert result is None
