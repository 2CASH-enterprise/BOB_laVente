"""Lot 41 — inscription guidée : email vérifié par code, pays/devise de la liste, activité, premiers pas."""
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email_verification import EmailVerification
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import email_verification
from app.services.countries import COUNTRIES, CURRENCIES
from app.services.local_time import COUNTRY_TIMEZONES

SEND = "/api/v1/auth/signup/send-code"
REGISTER = "/api/v1/auth/register-tenant"
HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
DEMO_HTML = (Path(__file__).resolve().parents[1] / "static" / "instant-demo" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.auth.routes.send_email", lambda **kw: sent.append(kw) or True)
    return sent


def _code_from(mail) -> str:
    return re.search(r"^(\d{6})$", mail["body"], re.M).group(1)


def _payload(email, code, **extra):
    return {"company_name": "Boutique Awa", "country": "SN", "currency": "XOF", "owner_email": email,
            "owner_full_name": "Awa Diop", "owner_password": "motdepasse41", "verification_code": code, **extra}


# --- Code par email ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_signup_with_code_and_activity(client, db_session, mails, unique_email):
    r = await client.post(SEND, json={"email": unique_email})
    assert r.status_code == 202
    [mail] = mails
    assert mail["to"] == unique_email and mail["subject"] == "Votre code pour créer votre compte Bob"
    code = _code_from(mail)
    stored = (await db_session.execute(select(EmailVerification))).scalar_one()
    assert stored.code_hash != code and code not in stored.code_hash  # jamais en clair

    r = await client.post(REGISTER, json=_payload(unique_email, code, business_type="CAR_DEALERSHIP"))
    assert r.status_code == 201
    tenant = await db_session.get(Tenant, uuid.UUID(r.json()["tenant_id"]))
    assert tenant.business_type == "CAR_DEALERSHIP" and tenant.business_type_chosen_at is not None
    assert (await db_session.execute(select(EmailVerification).execution_options(populate_existing=True))).scalars().all() == []
    log = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TENANT_REGISTERED"))).scalar_one()
    assert log.details["email_verified"] is True and log.details["business_type"] == "CAR_DEALERSHIP"
    # l'écran « Quel est votre secteur ? » ne s'affiche plus
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "motdepasse41"})).json()["access_token"]
    bt = (await client.get("/api/v1/tenants/me/business-type", headers={"Authorization": f"Bearer {token}"})).json()
    assert bt["chosen"] is True and bt["business_type"] == "CAR_DEALERSHIP"


@pytest.mark.asyncio
async def test_no_account_without_the_right_code(client, db_session, email_code, unique_email):
    code = await email_code(unique_email)
    wrong = "000000" if code != "000000" else "111111"
    r = await client.post(REGISTER, json=_payload(unique_email, wrong))
    assert r.status_code == 400 and "Code incorrect" in r.json()["detail"]
    assert (await db_session.execute(select(Tenant))).scalars().all() == []
    r = await client.post(REGISTER, json={k: v for k, v in _payload(unique_email, code).items() if k != "verification_code"})
    assert r.status_code == 422  # le code est obligatoire
    assert (await client.post(REGISTER, json=_payload(unique_email, code))).status_code == 201


@pytest.mark.asyncio
async def test_code_for_another_email_is_refused(client, email_code, unique_email):
    code = await email_code("autre@example.com")
    assert (await client.post(REGISTER, json=_payload(unique_email, code))).status_code == 400


@pytest.mark.asyncio
async def test_five_wrong_tries_kill_the_code(client, email_code, unique_email):
    code = await email_code(unique_email)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(email_verification.MAX_ATTEMPTS):
        assert (await client.post(REGISTER, json=_payload(unique_email, wrong))).status_code == 400
    assert (await client.post(REGISTER, json=_payload(unique_email, code))).status_code == 400  # même le bon : trop tard


@pytest.mark.asyncio
async def test_code_expires_after_10_minutes(db_session, unique_email):
    now = datetime.now(timezone.utc)
    code = await email_verification.new_code(db_session, unique_email, now)
    assert await email_verification.check_code(db_session, unique_email, code, now + timedelta(minutes=11)) is False
    assert await email_verification.check_code(db_session, unique_email, code, now + timedelta(minutes=9)) is True
    assert await email_verification.check_code(db_session, unique_email, code, now + timedelta(minutes=9)) is False  # une seule fois


@pytest.mark.asyncio
async def test_new_code_replaces_the_old_one_and_email_case_is_ignored(db_session, unique_email):
    old = await email_verification.new_code(db_session, unique_email)
    new = await email_verification.new_code(db_session, unique_email.upper())
    if old != new:
        assert await email_verification.check_code(db_session, unique_email, old) is False
    assert await email_verification.check_code(db_session, unique_email.upper(), new) is True


@pytest.mark.asyncio
async def test_no_code_for_an_existing_account(client, db_session, mails, unique_email):
    tenant = Tenant(name="B", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    assert (await client.post(SEND, json={"email": unique_email})).status_code == 409
    assert mails == []


@pytest.mark.asyncio
async def test_codes_are_rate_limited(client, mails, unique_email):
    codes = [(await client.post(SEND, json={"email": unique_email})).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]  # 3 par adresse et par 10 minutes
    ip = [(await client.post(SEND, json={"email": f"x{i}@example.com"})).status_code for i in range(8)]
    assert ip.count(429) >= 1  # 10 par connexion et par 10 minutes


@pytest.mark.asyncio
async def test_register_is_rate_limited(client):
    codes = [(await client.post(REGISTER, json=_payload(f"r{i}@example.com", "123456"))).status_code for i in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429


def test_code_email_shows_the_code_alone():
    subject, body = email_verification.code_email("482913")
    assert "\n\n482913\n\n" in body  # seul sur sa ligne : affiché en grand (lot 40)


# --- Pays, devise, activité ----------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [("country", "ZZ"), ("country", "US"), ("currency", "BTC"), ("business_type", "GARAGE")])
async def test_unknown_country_currency_or_activity_is_refused(client, email_code, unique_email, field, value):
    r = await client.post(REGISTER, json=_payload(unique_email, await email_code(unique_email), **{field: value}))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_signup_options(client):
    o = (await client.get("/api/v1/auth/signup-options")).json()
    assert {c["code"] for c in o["countries"]} == {c for c, _, _ in COUNTRIES}
    assert {"ONLINE_STORE", "CAR_DEALERSHIP", "INSURANCE_BROKER"} == {b["code"] for b in o["business_types"]}  # lot 53
    assert next(c for c in o["countries"] if c["code"] == "CM")["currency"] == "XAF"


def test_every_country_has_its_local_time_and_currency():
    for code, _, currency in COUNTRIES:
        assert code in COUNTRY_TIMEZONES and currency in CURRENCIES


# --- Démo → vrai compte : même vérification -------------------------------------------------------------

@pytest.mark.asyncio
async def test_demo_promotion_needs_the_code(client, db_session, email_code):
    from app.tests.test_demo_promote import _create_demo

    demo = await _create_demo(client)
    headers = {"Authorization": f"Bearer {demo['demo_token']}"}
    r = await client.post("/api/v1/demo/promote", headers=headers,
                          json={"full_name": "Awa", "email": "demo41@example.com", "password": "motdepasse41", "verification_code": "123456"})
    assert r.status_code == 400
    code = await email_code("demo41@example.com")
    r = await client.post("/api/v1/demo/promote", headers=headers,
                          json={"full_name": "Awa", "email": "demo41@example.com", "password": "motdepasse41", "verification_code": code})
    assert r.status_code == 200


# --- Premiers pas ----------------------------------------------------------------------------------------

async def _owner(client, db_session, business_type="ONLINE_STORE"):
    email = f"o{uuid.uuid4().hex[:6]}@example.com"
    tenant = Tenant(name="B", country="SN", currency="XOF", email=email, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return tenant, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_first_steps_follow_the_real_data(client, db_session):
    tenant, h = await _owner(client, db_session)
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert [s["key"] for s in o["steps"]] == ["whatsapp", "products", "profile", "payment", "test"]
    assert o["done"] == 0 and o["show"] is True

    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn{uuid.uuid4().hex[:6]}", system_user_token="t"))
    db_session.add(Product(tenant_id=tenant.id, sku="A", name="Robe", price=1, currency="XOF", stock_quantity=1))
    tenant.company_profile = "Boutique de pagnes"
    tenant.payment_link = "https://pay.example/x"
    demo = Customer(tenant_id=tenant.id, whatsapp_number="demo-web-session")
    db_session.add(demo)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=demo.id))  # la conversation de démo ne compte pas
    await db_session.commit()
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert o["done"] == 4 and [s["key"] for s in o["steps"] if not s["done"]] == ["test"]

    real = Customer(tenant_id=tenant.id, whatsapp_number="221700000099")
    db_session.add(real)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=real.id))
    await db_session.commit()
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert o["done"] == 5 and o["show"] is False  # tout est fait : la carte disparaît


@pytest.mark.asyncio
async def test_inactive_products_do_not_count(client, db_session):
    tenant, h = await _owner(client, db_session)
    db_session.add(Product(tenant_id=tenant.id, sku="A", name="Robe", price=1, currency="XOF", stock_quantity=1, active=False))
    await db_session.commit()
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert next(s for s in o["steps"] if s["key"] == "products")["done"] is False


@pytest.mark.asyncio
async def test_dealership_steps(client, db_session):
    tenant, h = await _owner(client, db_session, "CAR_DEALERSHIP")
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert [s["key"] for s in o["steps"]] == ["whatsapp", "products", "profile", "booking", "test"]
    assert o["steps"][1]["label"] == "Ajouter vos véhicules"
    db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True,
                                             opening_hours={"0": [["09:00", "12:00"]]}, slot_minutes=60, capacity=1))
    await db_session.commit()
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert next(s for s in o["steps"] if s["key"] == "booking")["done"] is True


@pytest.mark.asyncio
async def test_first_steps_can_be_hidden_and_belong_to_one_shop(client, db_session):
    mine, h = await _owner(client, db_session)
    other, h2 = await _owner(client, db_session)
    db_session.add(Product(tenant_id=other.id, sku="B", name="X", price=1, currency="XOF", stock_quantity=1))
    db_session.add(WhatsAppAccount(tenant_id=other.id, waba_id="w", phone_number_id=f"pn{uuid.uuid4().hex[:6]}", system_user_token="t"))
    await db_session.commit()
    o = (await client.get("/api/v1/tenants/me/onboarding", headers=h)).json()
    assert next(s for s in o["steps"] if s["key"] == "products")["done"] is False  # le produit d'une autre boutique
    assert next(s for s in o["steps"] if s["key"] == "whatsapp")["done"] is False  # son WhatsApp non plus
    o = (await client.put("/api/v1/tenants/me/onboarding", headers=h, json={"hidden": True})).json()
    assert o["hidden"] is True and o["show"] is False
    assert (await client.get("/api/v1/tenants/me/onboarding", headers=h2)).json()["show"] is True
    assert (await client.get("/api/v1/tenants/me/onboarding")).status_code == 401


# --- Pages -----------------------------------------------------------------------------------------------------

def test_signup_screen_has_four_steps_and_no_typed_country():
    screen = HTML[HTML.index('<div id="signup-screen"'):HTML.index('<div id="mfa-screen"')]
    assert len(re.findall(r'class="signup-step(?: hidden)?" data-step="[1-4]"', screen)) == 4
    assert '<select id="signup-country"' in screen and '<select id="signup-currency"' in screen
    assert 'placeholder="Pays (ex. SN)"' not in HTML
    assert 'autocomplete="one-time-code"' in screen and 'autocomplete="new-password"' in screen
    assert "/api/v1/auth/signup/send-code" in HTML and "verification_code: code" in HTML
    assert "business_type: signupState.businessType" in HTML


def test_home_shows_first_steps():
    assert '<div id="onboarding-card"></div>' in HTML and "/api/v1/tenants/me/onboarding" in HTML
    assert "esc(st.label)" in HTML and "esc(st.hint)" in HTML


def test_demo_page_asks_the_code_before_creating_the_account():
    assert "/api/v1/auth/signup/send-code" in DEMO_HTML and "verification_code: code" in DEMO_HTML
