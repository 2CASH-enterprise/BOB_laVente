import pytest

from app.agents.prompts import build_system_prompt
from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User


def test_prompt_without_profile_has_no_empty_section():
    tenant = Tenant(name="Boutique", country="SN", currency="XOF")
    prompt = build_system_prompt(tenant)
    assert "Présentation" not in prompt
    assert "Site web" not in prompt


def test_prompt_includes_company_profile():
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", company_profile="Boutique de mode africaine fondée en 2020")
    prompt = build_system_prompt(tenant)
    assert "Boutique de mode africaine fondée en 2020" in prompt


def test_prompt_includes_website_url():
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", website_url="https://ma-boutique.com")
    prompt = build_system_prompt(tenant)
    assert "https://ma-boutique.com" in prompt


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_update_company_profile_via_api(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.put(
        "/api/v1/tenants/me/profile",
        json={"company_profile": "Vente de tissus wax et accessoires"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["company_profile"] == "Vente de tissus wax et accessoires"


@pytest.mark.asyncio
async def test_company_profile_none_by_default(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    response = await client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["company_profile"] is None
