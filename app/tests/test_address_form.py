"""Lot 38 — tutoiement / vouvoiement des clients, réglé par la boutique en ligne."""
import re
import uuid

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.agents.prompts import build_system_prompt
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import address_form, contact_capture, handoff_rules, plan_limits, promise_guard
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.order_service import build_cancellation_message
from app.services.receipt_service import generate_order_confirmation_text
from app.tests.fakes import FakeLLMClient, text_response

VOUS_WORDS = re.compile(r"\b(vous|votre|vos)\b", re.IGNORECASE)
TU_WORDS = re.compile(r"\b(tu|te|t'|ton|ta|tes|toi)\b|\bt'", re.IGNORECASE)


def _tu_text(text):
    assert not VOUS_WORDS.search(text), f"« vous » dans la version tutoiement : {text}"
    assert TU_WORDS.search(text), f"aucun tutoiement : {text}"


def _vous_text(text):
    assert VOUS_WORDS.search(text), text
    assert not re.search(r"\b(tu|ton|ta|tes)\b", text, re.IGNORECASE), f"« tu » dans la version vouvoiement : {text}"


def _tenant(business_type=ONLINE_STORE, form="VOUS"):
    return Tenant(name="Boutique Awa", country="SN", currency="XOF", email="x@example.com",
                  business_type=business_type, address_form=form)


# --- Qui tutoie ? ---------------------------------------------------------------------------

def test_only_an_online_store_that_chose_tu_uses_tu():
    assert address_form.uses_tu(_tenant(form="TU")) is True
    assert address_form.uses_tu(_tenant(form="VOUS")) is False
    assert address_form.uses_tu(_tenant(CAR_DEALERSHIP, "TU")) is False  # concession : toujours vous
    assert address_form.uses_tu(None) is False


@pytest.mark.asyncio
async def test_new_shops_use_vous_by_default(db_session):
    tenant = Tenant(name="B", country="SN", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@example.com")
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    assert tenant.address_form == "VOUS" and address_form.uses_tu(tenant) is False


# --- Consigne de l'IA ---------------------------------------------------------------------------

def test_ai_is_told_to_use_tu_or_vous():
    tu_prompt = build_system_prompt(_tenant(form="TU"))
    vous_prompt = build_system_prompt(_tenant(form="VOUS"))
    assert "Tutoie TOUJOURS le client" in tu_prompt and "Vouvoie" not in tu_prompt
    assert "Vouvoie TOUJOURS le client" in vous_prompt and "Tutoie" not in vous_prompt


def test_dealership_prompt_always_vouvoie():
    prompt = build_system_prompt(_tenant(CAR_DEALERSHIP, "TU"))
    assert "Vouvoie TOUJOURS" in prompt and "Tutoie" not in prompt


# --- Messages fixes : chaque version dans le bon registre ------------------------------------------

def test_transfer_and_outage_messages():
    from app.services.handoff_rules import OUTAGE_CALLBACK, HandoffSettingsView

    for rule in (None, "HUMAN_REQUEST", "MISSING_CONDITIONS"):
        _tu_text(handoff_rules.transfer_message(rule, tu=True))
        _vous_text(handoff_rules.transfer_message(rule))
    assert handoff_rules.transfer_message("MISSING_CONDITIONS", tu=True) == handoff_rules.MISSING_CONDITIONS_MESSAGE_TU
    for policy in (OUTAGE_CALLBACK, "RETRY_LATER"):
        view = HandoffSettingsView(ai_outage_policy=policy)
        tu_text, tu_rule = handoff_rules.outage_message(view, tu=True)
        vous_text, vous_rule = handoff_rules.outage_message(view)
        _tu_text(tu_text)
        _vous_text(vous_text)
        assert tu_rule == vous_rule  # même règle tracée, seul le texte change


def test_quota_opt_in_opt_out_and_neutral_messages():
    _tu_text(plan_limits.FREEMIUM_QUOTA_MESSAGE_TU.format(company_name="Boutique Awa"))
    _vous_text(plan_limits.FREEMIUM_QUOTA_MESSAGE.format(company_name="Boutique Awa"))
    for known in (True, False):
        _tu_text(contact_capture.opt_in_reply(known, tu=True))
        _vous_text(contact_capture.opt_in_reply(known))
    _tu_text(contact_capture.OPT_OUT_REPLY_TU)
    _vous_text(contact_capture.OPT_OUT_REPLY)
    _tu_text(promise_guard.NEUTRAL_REPLACEMENT_TU)
    assert promise_guard.remove_human_promises("Un conseiller va te recontacter.", tu=True) == promise_guard.NEUTRAL_REPLACEMENT_TU
    assert promise_guard.remove_human_promises("Un conseiller va vous recontacter.") == promise_guard.NEUTRAL_REPLACEMENT


def _customer(**kw):
    return Customer(tenant_id=uuid.uuid4(), whatsapp_number="221700000001", **kw)


def test_email_request_follows_the_setting():
    text = contact_capture.request_email(_customer(), contact_capture.REASON_ORDER, tu=True)
    _tu_text(text)
    assert "OFFRES" in text and "ajoute le mot OFFRES à ta réponse" in text
    vous = contact_capture.request_email(_customer(), contact_capture.REASON_ORDER)
    _vous_text(vous)
    assert "ajoutez le mot OFFRES" in vous
    # rendez-vous : la concession vouvoie toujours (aucune version tu)
    _vous_text(contact_capture.request_email(_customer(), contact_capture.REASON_APPOINTMENT, tu=True))


def _order():
    return Order(id=uuid.uuid4(), tenant_id=uuid.uuid4(), customer_id=uuid.uuid4(), status=OrderStatus.PENDING,
                 total_amount=25000, currency="XOF")


def test_order_confirmation_and_cancellation():
    order = _order()
    for link in ("https://pay.example.com/x", None):
        tu = generate_order_confirmation_text(order, ["Robe x1 — 25 000 XOF"], "Boutique Awa", link, tu=True)
        vous = generate_order_confirmation_text(order, ["Robe x1 — 25 000 XOF"], "Boutique Awa", link)
        _tu_text(tu)
        _vous_text(vous)
        assert tu.split("\n")[:5] == vous.split("\n")[:5]  # référence, articles, total : identiques
    _tu_text(build_cancellation_message(order, tu=True))
    _vous_text(build_cancellation_message(order))
    assert str(order.id)[:8].upper() in build_cancellation_message(order, tu=True)


def test_previews_are_in_the_right_register():
    for line in address_form.preview("TU"):
        _tu_text(line)
    for line in address_form.preview("VOUS"):
        _vous_text(line)


# --- Branchement réel : outils, annulation, webhook ------------------------------------------------

async def _shop(db, form="TU", business_type=ONLINE_STORE, phone_number_id=None):
    email = f"s{uuid.uuid4().hex[:6]}@example.com"
    tenant = Tenant(name="Boutique Awa", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT,
                    business_type=business_type, address_form=form)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER))
    if phone_number_id:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id, system_user_token="t"))
    await db.commit()
    return tenant, email


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["TU", "VOUS"])
async def test_order_created_by_bob_uses_the_setting(db_session, form):
    tenant, _ = await _shop(db_session, form)
    product = Product(tenant_id=tenant.id, sku="R1", name="Robe wax", price=25000, currency="XOF", stock_quantity=5)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000011")
    db_session.add_all([product, customer])
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)

    await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 1}]})
    await db_session.commit()

    confirmation = (await db_session.execute(select(Message).where(Message.message_type == "order_confirmation"))).scalar_one()
    check = _tu_text if form == "TU" else _vous_text
    check(confirmation.content)
    assert executor.message_outbox, "la demande d'email part après la commande"
    check(executor.message_outbox[0])


@pytest.mark.asyncio
async def test_cancellation_message_uses_the_setting(client, db_session, monkeypatch):
    tenant, email = await _shop(db_session, "TU", phone_number_id="PN38C")
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000012")
    db_session.add(customer)
    await db_session.flush()
    from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender

    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="text", content="Bonjour"))  # lot 49 : fenêtre de 20 h ouverte
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=1000, currency="XOF")
    db_session.add(order)
    await db_session.commit()
    sent = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append(body)

    monkeypatch.setattr("app.api.orders.routes.WhatsAppClient", _Client)
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    r = await client.put(f"/api/v1/orders/{order.id}/cancel", json={"notify_customer": True},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    [body] = sent
    _tu_text(body)


def _payload(phone_number_id, from_number, text):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}],
    }}]}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected", [
    ("STOP", contact_capture.OPT_OUT_REPLY_TU),
    ("OFFRES", contact_capture.opt_in_reply(False, tu=True)),
])
async def test_webhook_fixed_replies_use_tu(client, db_session, text, expected):
    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)
    fake = FakeLLMClient([text_response("jamais appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000013", text))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.json()["ai_reply"] == expected


@pytest.mark.asyncio
async def test_webhook_quota_message_uses_tu(client, db_session):
    from app.services.plan_limits import FREEMIUM_MAX_CONVERSATIONS_PER_MONTH

    pn = f"PN38{uuid.uuid4().hex[:6]}"
    tenant, _ = await _shop(db_session, "TU", phone_number_id=pn)
    tenant.plan = TenantPlan.FREE
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000014")
    db_session.add(customer)
    await db_session.flush()
    for _ in range(FREEMIUM_MAX_CONVERSATIONS_PER_MONTH + 1):
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.CLOSED))
    await db_session.commit()
    fake = FakeLLMClient([text_response("jamais appelé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000014", "Bonjour"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.json()["ai_reply"].startswith(plan_limits.FREEMIUM_QUOTA_MESSAGE_TU.format(company_name="Boutique Awa"))


@pytest.mark.asyncio
async def test_webhook_gives_the_tu_rule_to_bob(client, db_session):
    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)
    fake = FakeLLMClient([text_response("Salut ! Tu cherches quoi ?")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000015", "Bonjour"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert "Tutoie TOUJOURS le client" in fake.received_systems[0]


@pytest.mark.asyncio
async def test_webhook_loop_transfer_uses_tu(client, db_session, monkeypatch):
    from app.api.webhooks import whatsapp as webhook

    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)

    async def looping(**kw):
        return "…", webhook.FAILURE_LOOP

    monkeypatch.setattr(webhook, "generate_ai_reply_detailed", looping)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([])
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000016", "Bonjour"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.json()["ai_reply"] == handoff_rules.TRANSFER_MESSAGE_TU


@pytest.mark.asyncio
async def test_webhook_outage_uses_tu(client, db_session, monkeypatch):
    from app.api.webhooks import whatsapp as webhook

    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)

    async def down(**kw):
        return "", webhook.FAILURE_OUTAGE

    monkeypatch.setattr(webhook, "generate_ai_reply_detailed", down)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([])
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000017", "Bonjour"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    _tu_text(r.json()["ai_reply"])


# --- Réglage dans le tableau de bord --------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_store_reads_and_changes_the_setting(client, db_session):
    tenant, email = await _shop(db_session, "VOUS")
    h = await _headers(client, email)
    r = await client.get("/api/v1/tenants/me/address-form", headers=h)
    assert r.status_code == 200 and r.json()["address_form"] == "VOUS"
    assert [o["code"] for o in r.json()["options"]] == ["VOUS", "TU"]
    r = await client.put("/api/v1/tenants/me/address-form", headers=h, json={"address_form": "TU"})
    assert r.status_code == 200 and r.json()["address_form"] == "TU"
    await db_session.refresh(tenant)
    assert tenant.address_form == "TU"
    entry = (await db_session.execute(select(AuditLog).where(AuditLog.action == "ADDRESS_FORM_UPDATED"))).scalar_one()
    assert entry.details == {"from": "VOUS", "to": "TU"}


@pytest.mark.asyncio
async def test_invalid_value_is_refused(client, db_session):
    tenant, email = await _shop(db_session, "VOUS")
    r = await client.put("/api/v1/tenants/me/address-form", headers=await _headers(client, email), json={"address_form": "toi"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_dealership_cannot_use_the_setting(client, db_session):
    tenant, email = await _shop(db_session, "VOUS", CAR_DEALERSHIP)
    h = await _headers(client, email)
    assert (await client.get("/api/v1/tenants/me/address-form", headers=h)).status_code == 403
    r = await client.put("/api/v1/tenants/me/address-form", headers=h, json={"address_form": "TU"})
    assert r.status_code == 403
    await db_session.refresh(tenant)
    assert tenant.address_form == "VOUS"


@pytest.mark.asyncio
async def test_only_admins_change_the_setting(client, db_session):
    tenant, _ = await _shop(db_session, "VOUS")
    email = f"agent{uuid.uuid4().hex[:5]}@example.com"
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="A", role=Role.AGENT))
    await db_session.commit()
    r = await client.put("/api/v1/tenants/me/address-form", headers=await _headers(client, email), json={"address_form": "TU"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_setting_never_touches_another_shop(client, db_session):
    mine, email = await _shop(db_session, "VOUS")
    other, _ = await _shop(db_session, "VOUS")
    await client.put("/api/v1/tenants/me/address-form", headers=await _headers(client, email), json={"address_form": "TU"})
    await db_session.refresh(other)
    assert other.address_form == "VOUS"


def test_dashboard_has_the_setting_for_stores_only():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'class="card store-only" style="max-width: 480px; flex: 1; min-width: 300px;" id="address-form-card"' in html
    assert "/api/v1/tenants/me/address-form" in html and 'name="address-form" value="TU"' in html
    assert "esc(line)" in html  # aperçu échappé


@pytest.mark.asyncio
@pytest.mark.parametrize("rule, expected", [
    ("HUMAN_REQUEST", handoff_rules.TRANSFER_MESSAGE_TU),
    ("MISSING_CONDITIONS", handoff_rules.MISSING_CONDITIONS_MESSAGE_TU),
])
async def test_webhook_immediate_transfer_uses_tu(client, db_session, monkeypatch, rule, expected):
    from app.api.webhooks import whatsapp as webhook
    from app.services.handoff_rules import TRANSFER_NOW, TurnDecision

    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)

    async def now(db, tenant, signal):
        return TurnDecision(mode=TRANSFER_NOW, rule=rule)

    monkeypatch.setattr(webhook, "decide_turn", now)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([])
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000018", "Je veux parler à quelqu'un"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.json()["ai_reply"] == expected


@pytest.mark.asyncio
async def test_webhook_removed_promise_uses_tu(client, db_session, monkeypatch):
    from app.api.webhooks import whatsapp as webhook
    from app.services.handoff_rules import FORBID_TRANSFER, TurnDecision

    pn = f"PN38{uuid.uuid4().hex[:6]}"
    await _shop(db_session, "TU", phone_number_id=pn)

    async def forbid(db, tenant, signal):
        return TurnDecision(mode=FORBID_TRANSFER, rule="DISCOUNT_FIXED_PRICES")

    monkeypatch.setattr(webhook, "decide_turn", forbid)
    fake = FakeLLMClient([text_response("Un conseiller va te recontacter.")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        r = await client.post("/webhooks/whatsapp", json=_payload(pn, "221700000019", "Une remise ?"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.json()["ai_reply"] == promise_guard.NEUTRAL_REPLACEMENT_TU
