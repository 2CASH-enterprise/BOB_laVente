"""
Lot 27 — liens commerciaux (concession) : un lien appartient à un commercial, le client qui
arrive par ce lien lui est rattaché, et le commercial reçoit les alertes de SES clients.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.dependency import get_llm_client
from app.core.database import Base
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.business_type import CAR_DEALERSHIP
from app.services.handoff_service import alert_emails, commercial_for_customer
from app.services.plan_limits import max_contact_points
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

API = "/api/v1/contact-points"
SELLER = "moussa.vendeur@example.com"


async def _dealer(db_session, email, phone_number_id, plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=plan, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id,
                                   system_user_token="t", display_phone_number="+221 70 111 22 33"))
    await db_session.commit()
    return tenant


async def _auth(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _payload(phone_number_id, from_number, text):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
                      "text": {"body": text}, "timestamp": "1700000000"}],
    }}]}]}


async def _customer(db_session, number, tenant_id):
    return (await db_session.execute(
        select(Customer).where(Customer.whatsapp_number == number, Customer.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: sent.append(kw) or True)
    return sent


@pytest.fixture
def no_whatsapp_send(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

        async def send_image_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)


@pytest.fixture
def llm():
    def use(responses):
        fake = FakeLLMClient(responses)
        app.dependency_overrides[get_llm_client] = lambda: fake
        return fake

    yield use
    app.dependency_overrides.pop(get_llm_client, None)


# --- Gestion du lien ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_can_belong_to_a_commercial(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-1")
    headers = await _auth(client, unique_email)

    r = await client.post(API, json={"name": "Moussa", "owner_name": " Moussa Diop ", "owner_email": "Moussa.Vendeur@Example.com"},
                          headers=headers)

    assert r.status_code == 201
    assert r.json()["owner_name"] == "Moussa Diop"
    assert r.json()["owner_email"] == SELLER  # normalisé en minuscules


@pytest.mark.asyncio
async def test_commercial_name_without_email_is_not_kept(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-2")
    r = await client.post(API, json={"name": "Salon", "owner_name": "Moussa"}, headers=await _auth(client, unique_email))
    assert r.json()["owner_name"] is None and r.json()["owner_email"] is None


@pytest.mark.asyncio
async def test_invalid_commercial_email_rejected(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-3")
    r = await client.post(API, json={"name": "X", "owner_email": "pas-un-email"}, headers=await _auth(client, unique_email))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_commercial_can_be_changed_and_removed(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-4")
    headers = await _auth(client, unique_email)
    cp = (await client.post(API, json={"name": "Lien", "owner_name": "Moussa", "owner_email": SELLER}, headers=headers)).json()

    changed = (await client.patch(f"{API}/{cp['id']}", json={"owner_name": "Awa", "owner_email": "Awa@Example.com"}, headers=headers)).json()
    assert (changed["owner_name"], changed["owner_email"]) == ("Awa", "awa@example.com")

    # Changer seulement le message ne touche pas au commercial.
    kept = (await client.patch(f"{API}/{cp['id']}", json={"greeting": "Bonjour"}, headers=headers)).json()
    assert kept["owner_email"] == "awa@example.com"

    removed = (await client.patch(f"{API}/{cp['id']}", json={"owner_email": "", "owner_name": ""}, headers=headers)).json()
    assert removed["owner_name"] is None and removed["owner_email"] is None
    assert removed["name"] == "Lien"  # le nom du lien, lui, n'est jamais vidé


@pytest.mark.asyncio
async def test_removing_the_email_also_removes_the_name(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-5")
    headers = await _auth(client, unique_email)
    cp = (await client.post(API, json={"name": "Lien", "owner_name": "Moussa", "owner_email": SELLER}, headers=headers)).json()
    removed = (await client.patch(f"{API}/{cp['id']}", json={"owner_email": ""}, headers=headers)).json()
    assert removed["owner_name"] is None


@pytest.mark.asyncio
async def test_commercial_email_never_exposed_publicly(client, db_session, unique_email):
    await _dealer(db_session, unique_email, "pn-com-6")
    cp = (await client.post(API, json={"name": "Lien", "owner_name": "Moussa", "owner_email": SELLER},
                            headers=await _auth(client, unique_email))).json()
    config = (await client.get(f"{cp['short_path']}/config")).text
    redirect = await client.get(cp["short_path"])
    assert SELLER not in config and "Moussa" not in config
    assert SELLER not in redirect.headers.get("location", "") and SELLER not in redirect.text


def test_paid_dealership_gets_thirty_links():
    assert max_contact_points(Tenant(plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP, is_demo=False)) == 30
    assert max_contact_points(Tenant(plan=TenantPlan.PRO, business_type="ONLINE_STORE", is_demo=False)) == 10
    assert max_contact_points(Tenant(plan=TenantPlan.FREE, business_type=CAR_DEALERSHIP, is_demo=False)) == 1


# --- Rattachement du client ---------------------------------------------------------------

async def _link(db_session, tenant, code, owner_email=SELLER, owner_name="Moussa", **kw):
    cp = ContactPoint(tenant_id=tenant.id, code=code, name=f"Lien {code}", greeting="Bonjour",
                      owner_name=owner_name if owner_email else None, owner_email=owner_email, **kw)
    db_session.add(cp)
    await db_session.commit()
    return cp


@pytest.mark.asyncio
async def test_new_customer_is_referred_to_the_commercial(client, db_session, unique_email, outbox, no_whatsapp_send):
    tenant = await _dealer(db_session, unique_email, "pn-com-10")
    cp = await _link(db_session, tenant, "moussa10")

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-10", "221700001010", "Bonjour [W:moussa10]"))

    customer = await _customer(db_session, "221700001010", tenant.id)
    assert customer.referred_contact_point_id == cp.id
    assert customer.acquisition_contact_point_id == cp.id  # premier contact : aussi la source


@pytest.mark.asyncio
async def test_known_customer_is_referred_to_the_last_commercial(client, db_session, unique_email, outbox, no_whatsapp_send):
    """Le client est venu une première fois par la page Facebook, puis revient par le lien de Moussa."""
    tenant = await _dealer(db_session, unique_email, "pn-com-11")
    facebook = await _link(db_session, tenant, "fb11", owner_email=None)
    moussa = await _link(db_session, tenant, "moussa11")
    awa = await _link(db_session, tenant, "awa11", owner_email="awa@example.com", owner_name="Awa")

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-11", "221700001011", "Bonjour [W:fb11]"))
    customer = await _customer(db_session, "221700001011", tenant.id)
    assert customer.referred_contact_point_id is None  # la page Facebook n'est pas un commercial

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-11", "221700001011", "Re [W:moussa11]"))
    customer = await _customer(db_session, "221700001011", tenant.id)
    assert customer.referred_contact_point_id == moussa.id
    assert customer.acquisition_contact_point_id == facebook.id  # la source ne change jamais

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-11", "221700001011", "Re [W:awa11]"))
    assert (await _customer(db_session, "221700001011", tenant.id)).referred_contact_point_id == awa.id

    # Un lien sans commercial ne retire pas le commercial déjà rattaché.
    await client.post("/webhooks/whatsapp", json=_payload("pn-com-11", "221700001011", "Re [W:fb11]"))
    assert (await _customer(db_session, "221700001011", tenant.id)).referred_contact_point_id == awa.id


@pytest.mark.asyncio
async def test_link_of_another_shop_never_refers(client, db_session, unique_email, outbox, no_whatsapp_send):
    tenant = await _dealer(db_session, unique_email, "pn-com-12")
    other = await _dealer(db_session, f"autre-{unique_email}", "pn-com-12b")
    await _link(db_session, other, "other12")

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-12", "221700001012", "Bonjour [W:other12]"))

    assert (await _customer(db_session, "221700001012", tenant.id)).referred_contact_point_id is None


@pytest.mark.asyncio
async def test_archived_link_never_refers(client, db_session, unique_email, outbox, no_whatsapp_send):
    tenant = await _dealer(db_session, unique_email, "pn-com-13")
    await _link(db_session, tenant, "old13", archived_at=datetime.now(timezone.utc))

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-13", "221700001013", "Bonjour [W:old13]"))

    assert (await _customer(db_session, "221700001013", tenant.id)).referred_contact_point_id is None


# --- Alertes ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_appointment_alert_goes_to_shop_and_commercial(client, db_session, unique_email, outbox, no_whatsapp_send, llm):
    tenant = await _dealer(db_session, unique_email, "pn-com-20")
    await _link(db_session, tenant, "moussa20")
    llm([
        tool_use_response("request_appointment", {"kind": "ESSAI", "availability": "samedi 10 h", "vehicle": "Peugeot 3008"}),
        text_response("Votre demande d'essai est transmise, un conseiller vous confirme l'horaire."),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-20", "221700001020", "Je veux essayer la 3008 samedi [W:moussa20]"))

    assert sorted(m["to"] for m in outbox) == sorted([unique_email, SELLER])
    for mail in outbox:
        assert mail["subject"].startswith("Rendez-vous à confirmer")
        assert "Client amené par : Moussa" in mail["body"]


@pytest.mark.asyncio
async def test_no_commercial_means_exactly_one_alert(client, db_session, unique_email, outbox, no_whatsapp_send, llm):
    tenant = await _dealer(db_session, unique_email, "pn-com-21")
    await _link(db_session, tenant, "fb21", owner_email=None)
    llm([
        tool_use_response("handoff_to_human", {"reason": "Question sur la garantie"}),
        text_response("Je transmets à un conseiller."),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-21", "221700001021", "Et la garantie ? [W:fb21]"))

    assert [m["to"] for m in outbox] == [unique_email]
    assert "amené par" not in outbox[0]["body"]


@pytest.mark.asyncio
async def test_deactivated_link_stops_alerting_the_commercial(client, db_session, unique_email, outbox, no_whatsapp_send, llm):
    """Commercial parti : le lien est désactivé, il ne reçoit plus rien sur ses anciens clients."""
    tenant = await _dealer(db_session, unique_email, "pn-com-22")
    cp = await _link(db_session, tenant, "moussa22")
    llm([text_response("Bonjour !")])
    await client.post("/webhooks/whatsapp", json=_payload("pn-com-22", "221700001022", "Bonjour [W:moussa22]"))
    cp.active = False
    await db_session.commit()
    outbox.clear()
    llm([
        tool_use_response("handoff_to_human", {"reason": "Question sur la garantie"}),
        text_response("Je transmets à un conseiller."),
    ])

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-22", "221700001022", "Et la garantie ?"))

    assert [m["to"] for m in outbox] == [unique_email]


@pytest.mark.asyncio
async def test_follow_up_reminder_also_goes_to_the_commercial(client, db_session, unique_email, outbox, llm):
    tenant = await _dealer(db_session, unique_email, "pn-com-23")
    cp = await _link(db_session, tenant, "moussa23")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700001023", referred_contact_point_id=cp.id)
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN,
                                human_alert_sent_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await db_session.commit()
    llm([])

    await client.post("/webhooks/whatsapp", json=_payload("pn-com-23", "221700001023", "Toujours personne ?"))

    assert sorted(m["to"] for m in outbox) == sorted([unique_email, SELLER])
    assert all(m["subject"].startswith("Relance") for m in outbox)


def test_commercial_with_the_shop_address_gets_a_single_email():
    commercial = ContactPoint(owner_name="Patron", owner_email="shop@example.com")
    emails = alert_emails("Shop@Example.com", commercial, "S", "B")
    assert len(emails) == 1


def test_alert_body_names_the_commercial_or_his_email():
    named = alert_emails("shop@example.com", ContactPoint(owner_name="Moussa", owner_email=SELLER), "S", "B")
    unnamed = alert_emails("shop@example.com", ContactPoint(owner_name=None, owner_email=SELLER), "S", "B")
    assert named[1]["body"].endswith("Client amené par : Moussa")
    assert unnamed[1]["body"].endswith(f"Client amené par : {SELLER}")
    assert alert_emails("shop@example.com", None, "S", "B") == [{"to": "shop@example.com", "subject": "S", "body": "B"}]


@pytest.mark.asyncio
async def test_commercial_of_another_shop_is_never_used(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, "pn-com-30")
    other = await _dealer(db_session, f"autre-{unique_email}", "pn-com-30b")
    foreign = await _link(db_session, other, "foreign30")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700001030", referred_contact_point_id=foreign.id)
    db_session.add(customer)
    await db_session.commit()
    assert await commercial_for_customer(db_session, customer) is None


# --- Rendez-vous : page et rappel de la veille --------------------------------------------

@pytest.mark.asyncio
async def test_appointment_page_shows_who_brought_the_customer(client, db_session, unique_email):
    tenant = await _dealer(db_session, unique_email, "pn-com-40")
    cp = await _link(db_session, tenant, "moussa40")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700001040", referred_contact_point_id=cp.id)
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="samedi", status="REQUESTED"))
    await db_session.commit()

    items = (await client.get("/api/v1/appointments?view=pending", headers=await _auth(client, unique_email))).json()["items"]

    assert items[0]["referred_by"] == "Moussa"


@pytest.mark.asyncio
async def test_day_before_reminder_goes_to_the_commercial_too():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant = Tenant(name="Auto Plus", country="FR", currency="EUR", email="concession@example.com",
                        plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
        db.add(tenant)
        await db.flush()
        cp = ContactPoint(tenant_id=tenant.id, code="m1", name="Moussa", greeting="Bonjour", owner_name="Moussa", owner_email=SELLER)
        db.add(cp)
        await db.flush()
        cust = Customer(tenant_id=tenant.id, whatsapp_number="33600000009", referred_contact_point_id=cp.id)
        db.add(cust)
        await db.flush()
        conv = Conversation(tenant_id=tenant.id, customer_id=cust.id)
        db.add(conv)
        await db.flush()
        db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=cust.id, kind="ESSAI",
                                  availability="samedi", status="CONFIRMED",
                                  scheduled_at=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)))
        await db.commit()

    outbox = []

    def fake_send(to, subject, body, **kw):
        outbox.append({"to": to, "body": body})
        return True

    evening = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    first = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=fake_send)
    second = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=fake_send)

    assert first == {"staff": 1, "customer": 0, "failed": 0} and second["staff"] == 0
    assert sorted(m["to"] for m in outbox) == sorted(["concession@example.com", SELLER])
    await engine.dispose()


def test_dashboard_escapes_commercial_fields():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert "${esc(cp.owner_name || \"\")}" in html and "${esc(cp.owner_email)}" in html
    assert "${esc(a.referred_by)}" in html
    assert 'id="cp-owner-email"' in html and "editContactPointOwner" in html and "openLinkQr" in html
