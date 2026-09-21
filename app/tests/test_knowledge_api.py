import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, role: Role = Role.MANAGER):
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
async def test_create_and_list_knowledge_entry(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/knowledge",
        json={"category": "OBJECTION", "title": "Trop cher", "content": "Proposer le modèle économique."},
        headers=headers,
    )
    assert response.status_code == 201

    listing = await client.get("/api/v1/knowledge", headers=headers)
    assert len(listing.json()) == 1
    assert listing.json()[0]["category"] == "OBJECTION"


@pytest.mark.asyncio
async def test_update_knowledge_entry(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/api/v1/knowledge", json={"title": "Ancien titre", "content": "Ancien contenu"}, headers=headers
    )
    entry_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/knowledge/{entry_id}", json={"title": "Nouveau titre"}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Nouveau titre"
    assert updated.json()["content"] == "Ancien contenu"  # inchangé


@pytest.mark.asyncio
async def test_delete_knowledge_entry(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post("/api/v1/knowledge", json={"title": "X", "content": "Y"}, headers=headers)
    entry_id = created.json()["id"]

    response = await client.delete(f"/api/v1/knowledge/{entry_id}", headers=headers)
    assert response.status_code == 204

    listing = await client.get("/api/v1/knowledge", headers=headers)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_knowledge_requires_manager_role_to_write(client, db_session, unique_email):
    await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/knowledge", json={"title": "X", "content": "Y"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_knowledge_isolated_by_tenant(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    await _setup(db_session, f"b_{unique_email}")

    token_a = await _login(client, unique_email)
    await client.post(
        "/api/v1/knowledge", json={"title": "Secret A", "content": "Y"}, headers={"Authorization": f"Bearer {token_a}"}
    )

    token_b = await _login(client, f"b_{unique_email}")
    listing_b = await client.get("/api/v1/knowledge", headers={"Authorization": f"Bearer {token_b}"})
    assert listing_b.json() == []
