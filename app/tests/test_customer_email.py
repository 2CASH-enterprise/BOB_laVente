"""Lot 34 — email et accord pour les offres du client final : bon moment, une seule fois, jamais deviné."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import contact_capture
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.customer_memory_service import build_customer_memory
from app.tests.fakes import FakeLLMClient, text_response


# --- Règles pures ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("mon mail : Awa.Diop@Example.com merci", "awa.diop@example.com"),
    ("c'est awa@example.sn.", "awa@example.sn"),
    ("awa@example.com ou bien awa@example.com", "awa@example.com"),
    ("awa@example.com ou moussa@example.com", None),  # deux adresses : pas de devinette
    ("écrivez-moi à awa arobase example point com", None),
    ("awa@exemple", None),
    ("", None),
])
def test_email_found_in_a_message(text, expected):
    assert contact_capture.extract_email(text) == expected


def _customer(**kw):
    return Customer(tenant_id=uuid.uuid4(), whatsapp_number="1", marketing_consent=kw.pop("marketing_consent", False), **kw)


def test_email_is_asked_once_and_offers_only_if_never_asked():
    customer = _customer()
    first = contact_capture.request_email(customer, contact_capture.REASON_APPOINTMENT)
    assert first.startswith("Souhaitez-vous recevoir un rappel par email la veille") and "OFFRES" in first
    assert customer.email_requested_at is not None
    assert contact_capture.request_email(customer, contact_capture.REASON_ORDER) is None  # jamais deux fois


def test_nothing_asked_when_email_known_and_no_offer_question_after_a_refusal():
    assert contact_capture.request_email(_customer(email="a@example.com"), contact_capture.REASON_ORDER) is None
    refused = _customer(marketing_consent_withdrawn_at=datetime.now(timezone.utc))
    text = contact_capture.request_email(refused, contact_capture.REASON_ORDER)
    assert "récapitulatif de votre commande" in text and "OFFRES" not in text


@pytest.mark.parametrize("text, expected", [
    ("OFFRES", True), ("offres", True), ("Oui OFFRES merci", True), ("awa@example.com OFFRES", True),
    ("ok pour les offres", True), ("Offre", True),
    ("Vous avez des offres ?", False), ("quelles offres en ce moment", False), ("oui", False),
    ("awa@example.com", False), ("", False), ("STOP OFFRES", False), ("Offres ?", False),
])
def test_offers_keyword_is_an_explicit_yes_only(text, expected):
    assert contact_capture.is_offers_opt_in(text) is expected


def test_contact_lines_tell_bob_what_not_to_ask_again():
    assert contact_capture.contact_lines(_customer()) == []
    known = contact_capture.contact_lines(_customer(email="a@example.com", marketing_consent=True))
    assert known[0].startswith("Email du client : déjà connu") and "accepté" in known[1]
    asked = contact_capture.contact_lines(_customer(email_requested_at=datetime.now(timezone.utc),
                                                    marketing_consent_withdrawn_at=datetime.now(timezone.utc)))
    assert "déjà demandé" in asked[0] and "ne repose jamais" in asked[1]
    assert all("a@example.com" not in line for line in known)  # l'adresse n'est jamais donnée à l'IA


# --- Outils ---------------------------------------------------------------------------------

async def _setup(db_session, email, business_type=ONLINE_STORE, booking=False, phone_number_id=None):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if phone_number_id:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    if booking:
        db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True,
                                                 opening_hours={str(d): [["06:00", "22:00"]] for d in range(7)}))
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    return tenant, customer, conversation


@pytest.mark.asyncio
async def test_email_tool_saves_only_a_valid_address(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)

    bad = await executor.execute("record_customer_email", {"email": "awa@"})
    assert "invalide" in bad["error"] and customer.email is None

    ok = await executor.execute("record_customer_email", {"email": " Awa@Example.com "})
    assert ok["status"] == "saved"
    assert (customer.email, customer.email_source) == ("awa@example.com", "BOB") and customer.email_collected_at


@pytest.mark.asyncio
async def test_confirmed_appointment_asks_the_email_once(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email, CAR_DEALERSHIP, booking=True)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    slots = (await executor.execute("get_available_slots", {}))["slots"]

    first = await executor.execute("request_appointment", {"kind": "ESSAI", "slot": slots[0]["slot"]})
    second = await executor.execute("request_appointment", {"kind": "VISITE", "slot": slots[1]["slot"]})

    assert first["status"] == "appointment_confirmed" and "ne lui demande pas toi-même son email" in first["instruction"]
    assert "sans changer la date" in first["instruction"]
    assert "email" not in second["instruction"]
    assert len(executor.message_outbox) == 1 and "rappel par email la veille" in executor.message_outbox[0]


@pytest.mark.asyncio
async def test_requested_appointment_also_asks_the_email(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email, CAR_DEALERSHIP)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    result = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi"})
    assert "Ne confirme ni date ni heure" in result["instruction"] and "message automatique" in result["instruction"]
    assert "rappel par email" in executor.message_outbox[0]


@pytest.mark.asyncio
async def test_order_asks_the_email_unless_already_known(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    product = Product(tenant_id=tenant.id, sku="R1", name="Robe", price=15000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)

    result = await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 1}]})
    assert "message automatique" in result["instruction"]
    assert "récapitulatif de votre commande" in executor.message_outbox[0]

    customer.email = "a@example.com"
    other_tenant, other_customer, other_conversation = await _setup(db_session, f"b-{unique_email}")
    other_customer.email = "b@example.com"
    other_product = Product(tenant_id=other_tenant.id, sku="R1", name="Robe", price=1, currency="XOF", stock_quantity=5)
    db_session.add(other_product)
    await db_session.commit()
    known = await ToolExecutor(db_session, other_tenant.id, other_conversation, business_type=ONLINE_STORE).execute(
        "create_order", {"items": [{"product_id": str(other_product.id), "quantity": 1}]})
    assert "order_id" in known and "instruction" not in known


# --- Webhook et mémoire -----------------------------------------------------------------------

@pytest.fixture
def silent(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)


def _payload(phone_number_id, number, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}, "messages": [
        {"from": number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


@pytest.mark.asyncio
async def test_email_written_by_the_customer_is_saved_by_the_code(client, db_session, unique_email, silent):
    tenant, customer, _ = await _setup(db_session, unique_email, phone_number_id="pn-mail-1")
    fake = FakeLLMClient([text_response("Merci !")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-mail-1", customer.whatsapp_number, "Oui : awa@example.com"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    await db_session.refresh(customer)
    assert (customer.email, customer.email_source) == ("awa@example.com", "WHATSAPP_MESSAGE")
    # Bob sait dès ce message que l'email est connu (et ne le voit jamais en entier).
    assert "Email du client : déjà connu" in fake.received_systems[0] and "awa@example.com" not in fake.received_systems[0]


@pytest.mark.asyncio
async def test_a_known_email_is_never_overwritten_by_a_message(client, db_session, unique_email, silent):
    tenant, customer, _ = await _setup(db_session, unique_email, phone_number_id="pn-mail-2")
    customer.email = "vrai@example.com"
    await db_session.commit()
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([text_response("D'accord.")])
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-mail-2", customer.whatsapp_number, "écrivez à contact@marque.com"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    await db_session.refresh(customer)
    assert customer.email == "vrai@example.com"


@pytest.mark.asyncio
async def test_memory_tells_bob_the_contact_state_without_the_address(db_session, unique_email):
    tenant, customer, _ = await _setup(db_session, unique_email)
    assert await build_customer_memory(db_session, tenant.id, customer.id) == ""
    customer.email = "awa@example.com"
    await db_session.commit()
    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert "CONTACT DU CLIENT" in memory and "déjà connu" in memory and "awa@example.com" not in memory


# --- Fiche client -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_can_be_set_and_removed_by_hand(client, db_session, unique_email):
    tenant, customer, _ = await _setup(db_session, unique_email)
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/customers/{customer.id}"

    assert (await client.put(url, json={"email": "pas-un-email"}, headers=headers)).status_code == 422
    saved = (await client.put(url, json={"email": "Awa@Example.com", "notes": "VIP"}, headers=headers)).json()
    assert (saved["email"], saved["email_source"]) == ("awa@example.com", "MANUAL") and saved["email_collected_at"]
    kept = (await client.put(url, json={"notes": "VIP 2"}, headers=headers)).json()
    assert kept["email"] == "awa@example.com"
    removed = (await client.put(url, json={"email": ""}, headers=headers)).json()
    assert removed["email"] is None and removed["email_source"] is None


def test_dashboard_and_privacy_page_mention_the_email():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert 'value="${esc(c.email || "")}"' in html and 'EMAIL_SOURCE_LABELS[c.email_source]' in html
    privacy = open("app/static/legal/privacy.html", encoding="utf-8").read()
    assert "L'adresse email, si le client choisit de la donner" in privacy
    assert "The email address, if the customer chooses to give it" in privacy



# --- Lot 34b : demande en message fixe, réponse « OFFRES » ---------------------------------

@pytest.fixture
def recorder(monkeypatch):
    sent = []

    class _Recorder:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append(body)
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Recorder)
    return sent


@pytest.mark.asyncio
async def test_the_email_request_is_a_fixed_message_sent_after_the_booking(client, db_session, unique_email, recorder, monkeypatch):
    from app.agents.tools import ToolExecutor as _TE  # noqa: F401
    from app.services import booking
    from app.services.local_time import tenant_zone
    from app.tests.fakes import tool_use_response

    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: True)
    tenant, customer, _ = await _setup(db_session, unique_email, CAR_DEALERSHIP, booking=True, phone_number_id="pn-mail-10")
    settings = await booking.load_settings(db_session, tenant.id)
    [slot] = await booking.free_slots(db_session, tenant, settings, tenant_zone(tenant), datetime.now(timezone.utc), limit=1)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([
        tool_use_response("request_appointment", {"kind": "ESSAI", "slot": booking.slot_id(slot)}),
        text_response("Parfait !"),
    ])
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-mail-10", customer.whatsapp_number, "Le premier créneau"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert sent_in_order(recorder) == ["Parfait !", "confirmation", "email"]


def sent_in_order(sent):
    kinds = []
    for body in sent:
        if "est confirmé" in body:
            kinds.append("confirmation")
        elif body.startswith("Souhaitez-vous recevoir un rappel par email"):
            kinds.append("email")
        else:
            kinds.append(body)
    return kinds


@pytest.mark.asyncio
async def test_offres_answer_records_consent_without_the_ai(client, db_session, unique_email, recorder):
    tenant, customer, _ = await _setup(db_session, unique_email, phone_number_id="pn-mail-11")
    fake = FakeLLMClient([])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-mail-11", customer.whatsapp_number, "awa@example.com OFFRES"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    await db_session.refresh(customer)
    assert customer.marketing_consent and customer.marketing_consent_source == "WHATSAPP_OFFRES"
    assert customer.email == "awa@example.com" and fake.call_count == 0
    assert recorder == ["C'est noté ✅ Vous recevrez nos offres par email. Vous pouvez vous désinscrire à tout moment en répondant STOP."]


@pytest.mark.asyncio
async def test_a_question_about_offers_is_not_a_consent(client, db_session, unique_email, recorder):
    tenant, customer, _ = await _setup(db_session, unique_email, phone_number_id="pn-mail-12")
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([text_response("Voici nos offres du moment.")])
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-mail-12", customer.whatsapp_number, "Vous avez des offres ?"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    await db_session.refresh(customer)
    assert customer.marketing_consent is False and customer.marketing_consent_given_at is None
