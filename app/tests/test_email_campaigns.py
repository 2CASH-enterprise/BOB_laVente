import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.email_campaign import EmailCampaign
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.campaign_service import get_eligible_customers, send_campaign


async def _setup(db_session, email: str, plan=TenantPlan.INDEPENDANT, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=plan)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_eligible_excludes_customer_without_consent(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email="a@example.com", marketing_consent=False))
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000002", email="b@example.com", marketing_consent=True))
    await db_session.commit()

    eligible = await get_eligible_customers(db_session, tenant.id)
    assert len(eligible) == 1
    assert eligible[0].email == "b@example.com"


@pytest.mark.asyncio
async def test_eligible_excludes_customer_without_email(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email=None, marketing_consent=True))
    await db_session.commit()

    eligible = await get_eligible_customers(db_session, tenant.id)
    assert eligible == []


@pytest.mark.asyncio
async def test_send_campaign_only_reaches_consenting_customers(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email="a@example.com", marketing_consent=False))
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000002", email="b@example.com", marketing_consent=True))
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000003", email="c@example.com", marketing_consent=True))
    await db_session.commit()

    from sqlalchemy import select

    from app.models.user import User as UserModel

    user = (await db_session.execute(select(UserModel).where(UserModel.tenant_id == tenant.id))).scalar_one()

    result = await send_campaign(db_session, tenant.id, user.id, "Nouveaux arrivages", "Découvrez nos nouveautés", "Bonjour, je viens de votre email")
    await db_session.commit()

    assert result["eligible_count"] == 2


@pytest.mark.asyncio
async def test_send_campaign_creates_history_entry(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email="a@example.com", marketing_consent=True))
    await db_session.commit()

    from sqlalchemy import select

    from app.models.user import User as UserModel

    user = (await db_session.execute(select(UserModel).where(UserModel.tenant_id == tenant.id))).scalar_one()

    await send_campaign(db_session, tenant.id, user.id, "Promo", "Texte", "CTA")
    await db_session.commit()

    campaigns = (await db_session.execute(select(EmailCampaign).where(EmailCampaign.tenant_id == tenant.id))).scalars().all()
    assert len(campaigns) == 1
    assert campaigns[0].subject == "Promo"


@pytest.mark.asyncio
async def test_campaign_send_blocked_for_freemium(client, db_session, unique_email):
    await _setup(db_session, unique_email, plan=TenantPlan.FREE)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/campaigns/send",
        json={"subject": "S", "body_text": "B", "whatsapp_cta_message": "C"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_campaign_send_works_for_paid_tenant(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email, plan=TenantPlan.PRO)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email="a@example.com", marketing_consent=True))
    await db_session.commit()
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/campaigns/send",
        json={"subject": "S", "body_text": "B", "whatsapp_cta_message": "C"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["eligible_count"] == 1


@pytest.mark.asyncio
async def test_eligible_count_isolated_by_tenant(client, db_session, unique_email):
    tenant_a = await _setup(db_session, unique_email, plan=TenantPlan.PRO)
    tenant_b = await _setup(db_session, f"b_{unique_email}", plan=TenantPlan.PRO)
    db_session.add(Customer(tenant_id=tenant_a.id, whatsapp_number="221700000001", email="a@example.com", marketing_consent=True))
    db_session.add(Customer(tenant_id=tenant_b.id, whatsapp_number="221700000002", email="b@example.com", marketing_consent=True))
    await db_session.commit()

    token_a = await _login(client, unique_email)
    response = await client.get("/api/v1/campaigns/eligible-count", headers={"Authorization": f"Bearer {token_a}"})
    assert response.json()["eligible_count"] == 1  # jamais le client du tenant B
