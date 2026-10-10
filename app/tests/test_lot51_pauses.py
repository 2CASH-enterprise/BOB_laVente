"""
Lot 51 — pauses de Bob.
A. Coexistence : le vendeur répond depuis l'application WhatsApp Business de son téléphone → Bob se met en
   pause sur la conversation ; il reprend si le client réécrit 2 h après sans nouvelle réponse du vendeur.
B. Boutique en pause : suspendue par le Super Admin, ou abonnement non renouvelé (1 jour de grâce, emails).
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.dependency import get_llm_client
from app.core.database import Base
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import bob_pause, coexistence
from app.services.handoff_service import reminder_due
from app.tests.fakes import FakeLLMClient, text_response

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
SUPERADMIN = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")


async def _shop(db, email=None, plan=TenantPlan.PRO, role=Role.OWNER, country="CI", paid_until=None, active=True):
    email = email or f"s{uuid.uuid4().hex[:8]}@l51.ci"
    tenant = Tenant(name="Auto Abidjan", country=country, currency="XOF", email=email, plan=plan,
                    paid_until=paid_until, active=active)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    pnid = f"pn-{uuid.uuid4().hex[:8]}"
    db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    await db.commit()
    tenant.pnid = pnid
    return tenant


def echo_payload(pnid, to, text="Oui il est disponible, passez demain", msg_id=None, kind="text", extra=None):
    echo = {"from": "2250700000000", "to": to, "id": msg_id or f"wamid.{uuid.uuid4().hex[:10]}", "timestamp": "1", "type": kind}
    if kind == "text":
        echo["text"] = {"body": text}
    echo.update(extra or {})
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "smb_message_echoes", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid}, "message_echoes": [echo]}}]}]}


def customer_payload(pnid, number, text="Bonjour, la Corolla est toujours là ?"):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": number, "id": f"wamid.{uuid.uuid4().hex[:10]}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


class LLM:
    def __init__(self, *replies):
        self.fake = FakeLLMClient([text_response(r) for r in replies])

    def __enter__(self):
        app.dependency_overrides[get_llm_client] = lambda: self.fake
        return self.fake

    def __exit__(self, *exc):
        app.dependency_overrides.pop(get_llm_client, None)


async def _conversation(db, tenant, number):
    customer = (await db.execute(select(Customer).where(Customer.tenant_id == tenant.id, Customer.whatsapp_number == number))).scalar_one()
    return (await db.execute(select(Conversation).where(Conversation.customer_id == customer.id))).scalar_one()


async def _messages(db, conversation):
    return (await db.execute(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at))).scalars().all()


async def _fresh(db, obj):
    await db.refresh(obj)
    return obj


# --- A. Coexistence : lecture des échos ------------------------------------------------------------------------

def test_parse_echoes_text_media_and_ignored_kinds():
    [text] = coexistence.parse_echoes(echo_payload("pn", "225", "  Bonjour  ", msg_id="m1"))
    assert text == {"phone_number_id": "pn", "to": "225", "wa_message_id": "m1", "type": "text", "text": "Bonjour"}
    [photo] = coexistence.parse_echoes(echo_payload("pn", "225", kind="image", extra={"image": {"id": "i", "caption": "La voici"}}))
    assert photo["text"] == "📷 Photo : La voici"
    [voice] = coexistence.parse_echoes(echo_payload("pn", "225", kind="audio", extra={"audio": {"id": "a"}}))
    assert voice["text"] == "🎤 Message vocal"
    [other] = coexistence.parse_echoes(echo_payload("pn", "225", kind="button", extra={"button": {}}))
    assert other["text"] == "Message"
    for kind in ("edit", "revoke", "reaction", "unsupported"):
        assert coexistence.parse_echoes(echo_payload("pn", "225", kind=kind, extra={kind: {"original_message_id": "x"}})) == []
    assert coexistence.parse_echoes(echo_payload("pn", "", "Bonjour")) == []          # sans destinataire
    assert coexistence.parse_echoes(echo_payload(None, "225", "Bonjour")) == []       # sans numéro de la boutique
    assert coexistence.parse_echoes(echo_payload("pn", "225", "   ")) == []           # texte vide
    assert coexistence.parse_echoes(customer_payload("pn", "225")) == []              # message d'un client
    assert coexistence.parse_echoes({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []
    assert coexistence.parse_echoes([]) == [] and coexistence.parse_echoes({}) == []


# --- A. Coexistence : le webhook -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phone_reply_is_recorded_and_pauses_bob(client, db_session):
    shop = await _shop(db_session)
    with LLM() as llm:
        r = await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, "2250711111111", msg_id="wamid.A"))
    assert r.json() == {"status": "phone_reply_recorded", "recorded": 1} and llm.call_count == 0

    conv = await _conversation(db_session, shop, "2250711111111")
    assert conv.status == ConversationStatus.WAITING_HUMAN and conv.phone_reply_at is not None and conv.assigned_agent is None
    reply, note = await _messages(db_session, conv)
    assert (reply.sender, reply.message_type, reply.content) == (MessageSender.HUMAN, "phone_echo", "Oui il est disponible, passez demain")
    assert reply.message_metadata == {"wa_message_id": "wamid.A", "source": "WHATSAPP_BUSINESS_APP"}
    assert (note.sender, note.message_type, note.content) == (MessageSender.SYSTEM, "phone_pause", coexistence.PAUSE_TEXT)
    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == shop.id))).scalar_one()
    assert (await _fresh(db_session, account)).coexistence_mode is True


@pytest.mark.asyncio
async def test_same_echo_twice_is_recorded_once(client, db_session):
    shop = await _shop(db_session)
    payload = echo_payload(shop.pnid, "2250722222222", msg_id="wamid.B")
    await client.post("/webhooks/whatsapp", json=payload)
    again = await client.post("/webhooks/whatsapp", json=payload)
    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, "2250722222222", "Et en blanc aussi", msg_id="wamid.C"))

    assert again.json()["recorded"] == 0
    conv = await _conversation(db_session, shop, "2250722222222")
    kinds = [m.message_type for m in await _messages(db_session, conv)]
    assert kinds == ["phone_echo", "phone_pause", "phone_echo"]  # une seule annonce de pause


@pytest.mark.asyncio
async def test_unknown_number_records_nothing(client, db_session):
    r = await client.post("/webhooks/whatsapp", json=echo_payload("pn-inconnu", "2250733333333"))
    assert r.json() == {"status": "phone_reply_recorded", "recorded": 0}
    assert (await db_session.execute(select(Customer))).first() is None


@pytest.mark.asyncio
async def test_echo_only_touches_the_shop_of_the_number(client, db_session):
    shop, other = await _shop(db_session), await _shop(db_session)
    other_customer = Customer(tenant_id=other.id, whatsapp_number="2250744444444")
    db_session.add(other_customer)
    await db_session.flush()
    other_conv = Conversation(tenant_id=other.id, customer_id=other_customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(other_conv)
    await db_session.commit()

    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, "2250744444444"))

    assert (await _fresh(db_session, other_conv)).status == ConversationStatus.ACTIVE
    assert await _messages(db_session, other_conv) == []
    mine = await _conversation(db_session, shop, "2250744444444")
    assert mine.tenant_id == shop.id and mine.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_customer_writing_soon_after_gets_no_bob_reply(client, db_session):
    shop = await _shop(db_session)
    number = "2250755555555"
    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, number))
    conv = await _conversation(db_session, shop, number)
    conv.phone_reply_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=55)
    await db_session.commit()

    with LLM("ne doit pas répondre") as llm:
        r = await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number))

    assert llm.call_count == 0 and "ai_reply" not in r.json()
    conv = await _fresh(db_session, conv)
    assert conv.status == ConversationStatus.WAITING_HUMAN and conv.human_alert_sent_at is None  # pas d'email de rappel
    assert [m.sender for m in await _messages(db_session, conv)][-1] == MessageSender.CUSTOMER


@pytest.mark.asyncio
async def test_bob_resumes_when_the_customer_writes_two_hours_later(client, db_session):
    shop = await _shop(db_session)
    number = "2250766666666"
    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, number))
    conv = await _conversation(db_session, shop, number)
    conv.phone_reply_at = datetime.now(timezone.utc) - timedelta(hours=2, minutes=1)
    await db_session.commit()

    with LLM("Oui, elle est toujours disponible.") as llm:
        r = await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number))

    assert llm.call_count == 1 and r.json()["ai_reply"].startswith("Oui, elle est toujours disponible.")
    conv = await _fresh(db_session, conv)
    assert conv.status == ConversationStatus.ACTIVE and conv.phone_reply_at is None
    kinds = [m.message_type for m in await _messages(db_session, conv)]
    assert kinds[:4] == ["phone_echo", "phone_pause", "phone_resume", "text"]


@pytest.mark.asyncio
async def test_takeover_from_the_dashboard_is_never_resumed_automatically(client, db_session):
    shop = await _shop(db_session)
    number = "2250777777777"
    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, number))
    conv = await _conversation(db_session, shop, number)
    user = (await db_session.execute(select(User).where(User.tenant_id == shop.id))).scalar_one()
    conv.assigned_agent = user.id
    conv.phone_reply_at = datetime.now(timezone.utc) - timedelta(hours=5)
    await db_session.commit()

    with LLM() as llm:
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number))
    assert llm.call_count == 0 and (await _fresh(db_session, conv)).status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_phone_reply_after_a_transfer_stops_the_reminders(client, db_session):
    shop = await _shop(db_session)
    customer = Customer(tenant_id=shop.id, whatsapp_number="2250788888888")
    db_session.add(customer)
    await db_session.flush()
    conv = Conversation(tenant_id=shop.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(conv)
    await db_session.commit()
    assert reminder_due(conv)

    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, "2250788888888"))

    conv = await _fresh(db_session, conv)
    assert conv.phone_reply_at is not None and not reminder_due(conv)
    assert [m.message_type for m in await _messages(db_session, conv)] == ["phone_echo"]  # pas de seconde annonce


def test_resume_needs_two_full_hours_and_no_agent():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    conv = Conversation(status=ConversationStatus.WAITING_HUMAN, phone_reply_at=now - timedelta(hours=2))
    assert coexistence.resume_due(conv, now)
    conv.phone_reply_at = now - timedelta(hours=2) + timedelta(seconds=1)
    assert not coexistence.resume_due(conv, now)
    conv.phone_reply_at = (now - timedelta(hours=3)).replace(tzinfo=None)  # SQLite : valeur naïve
    assert coexistence.resume_due(conv, now)
    conv.assigned_agent = uuid.uuid4()
    assert not coexistence.resume_due(conv, now)
    assert not coexistence.resume_due(Conversation(status=ConversationStatus.WAITING_HUMAN, phone_reply_at=None), now)
    assert not coexistence.resume_due(Conversation(status=ConversationStatus.ACTIVE, phone_reply_at=now - timedelta(hours=9)), now)


@pytest.mark.asyncio
async def test_any_agent_can_give_a_phone_paused_conversation_back_to_bob(client, db_session):
    shop = await _shop(db_session, role=Role.AGENT)
    number = "2250799999999"
    await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, number))
    conv = await _conversation(db_session, shop, number)
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]

    r = await client.post(f"/api/v1/conversations/{conv.id}/release", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200 and r.json()["status"] == "ACTIVE"
    assert (await _fresh(db_session, conv)).phone_reply_at is None


@pytest.mark.asyncio
async def test_agent_still_cannot_release_a_transfer_by_bob(client, db_session):
    shop = await _shop(db_session, role=Role.AGENT)
    customer = Customer(tenant_id=shop.id, whatsapp_number="2250700000011")
    db_session.add(customer)
    await db_session.flush()
    conv = Conversation(tenant_id=shop.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(conv)
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]
    r = await client.post(f"/api/v1/conversations/{conv.id}/release", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# --- B. Échéance de l'abonnement : la règle --------------------------------------------------------------------

def _at(day: date, hour=12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def test_billing_stages_around_the_due_date():
    end = date(2026, 10, 15)
    tenant = Tenant(name="A", country="CI", active=True, paid_until=end)
    expected = {-8: None, -7: "WARNING", 0: "WARNING", 1: "GRACE", 2: "PAUSED", 30: "PAUSED"}
    for offset, stage in expected.items():
        assert bob_pause.billing_stage(tenant, _at(end + timedelta(days=offset))) == stage, offset
    assert bob_pause.pause_reason(tenant, _at(end + timedelta(days=1))) is None   # jour de grâce : Bob répond
    assert bob_pause.pause_reason(tenant, _at(end + timedelta(days=2))) == "UNPAID"
    assert bob_pause.pause_starts_on(end) == date(2026, 10, 17)


def test_the_shop_local_date_decides():
    tenant = Tenant(name="A", country="FR", active=True, paid_until=date(2026, 10, 15))
    # 16 octobre 23 h 30 UTC = 17 octobre 1 h 30 à Paris : pause
    assert bob_pause.billing_stage(tenant, datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)) == "PAUSED"
    tenant.country = "CI"  # Abidjan = UTC : encore le jour de grâce
    assert bob_pause.billing_stage(tenant, datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)) == "GRACE"


def test_suspension_and_no_due_date():
    now = _at(date(2026, 10, 3))
    assert bob_pause.pause_reason(Tenant(name="A", country="CI", active=False), now) == "SUSPENDED"
    assert bob_pause.pause_reason(Tenant(name="A", country="CI", active=True, paid_until=None), now) is None
    assert bob_pause.billing_stage(Tenant(name="A", country="CI", active=True, paid_until=None), now) is None
    assert bob_pause.pause_reason(None, now) is None and not bob_pause.is_paused(None, now)


def test_dashboard_messages():
    now = _at(date(2026, 10, 3))
    s = bob_pause.status(Tenant(name="A", country="CI", active=True, paid_until=date(2026, 10, 8)), now)
    assert (s.paused, s.stage) == (False, "WARNING") and "se termine le 8 octobre 2026" in s.message
    s = bob_pause.status(Tenant(name="A", country="CI", active=True, paid_until=date(2026, 10, 2)), now)
    assert s.stage == "GRACE" and "se mettra en pause le 4 octobre 2026" in s.message and not s.paused
    s = bob_pause.status(Tenant(name="A", country="CI", active=True, paid_until=date(2026, 9, 30)), now)
    assert s.paused and s.reason == "UNPAID" and "en pause depuis le 2 octobre 2026" in s.message
    assert s.as_dict() == {"paused": True, "reason": "UNPAID", "stage": "PAUSED", "paid_until": "2026-09-30",
                           "pause_on": "2026-10-02", "message": s.message}
    s = bob_pause.status(Tenant(name="A", country="CI", active=False, paid_until=None), now)
    assert s.paused and "suspendu" in s.message and s.as_dict()["paid_until"] is None
    s = bob_pause.status(Tenant(name="A", country="CI", active=True, paid_until=date(2026, 12, 1)), now)
    assert s.message is None and not s.paused
    assert bob_pause.french_date(date(2026, 11, 1)) == "1er novembre 2026"


# --- B. Bob en pause : webhook, relances, campagnes, rappels -------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("paid_until, active", [(date.today() - timedelta(days=5), True), (None, False)])
async def test_paused_shop_records_the_message_without_any_ai(client, db_session, paid_until, active):
    shop = await _shop(db_session, paid_until=paid_until, active=active)
    with LLM("ne doit pas répondre") as llm:
        r = await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, "2250700000021"))
    assert r.json() == {"status": "received_bob_paused"} and llm.call_count == 0
    conv = await _conversation(db_session, shop, "2250700000021")
    [message] = await _messages(db_session, conv)
    assert message.sender == MessageSender.CUSTOMER and message.content == "Bonjour, la Corolla est toujours là ?"
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None  # pas d'analyse (coût d'IA)


@pytest.mark.asyncio
async def test_bob_still_answers_on_the_grace_day(client, db_session):
    shop = await _shop(db_session, paid_until=date.today() - timedelta(days=1), country="CI")
    with LLM("Oui, elle est là.") as llm:
        r = await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, "2250700000022"))
    assert llm.call_count == 1 and r.json()["ai_reply"].startswith("Oui, elle est là.")


@pytest.mark.asyncio
async def test_phone_replies_are_still_recorded_when_bob_is_paused(client, db_session):
    shop = await _shop(db_session, active=False)
    r = await client.post("/webhooks/whatsapp", json=echo_payload(shop.pnid, "2250700000023"))
    assert r.json()["recorded"] == 1


@pytest.mark.asyncio
async def test_relances_stop_when_bob_is_paused(db_session):
    from app.services.followup_service import run_followups_for_tenant
    from app.tests.test_lot49_email_relances import Mailbox, _customer, _tenant

    tenant, _ = await _tenant(db_session, "p@l51.sn")
    await _customer(db_session, tenant)
    tenant.paid_until = date.today() - timedelta(days=3)
    await db_session.commit()
    mailbox = Mailbox()
    assert await run_followups_for_tenant(db_session, tenant.id, send_email=mailbox) == 0 and mailbox.sent == []

    tenant.paid_until = date.today() + timedelta(days=30)  # renouvelé : les relances repartent
    await db_session.commit()
    assert await run_followups_for_tenant(db_session, tenant.id, send_email=mailbox) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("paid_until, active, message", [
    (date.today() - timedelta(days=3), True, "votre abonnement est à renouveler"),
    (None, False, "votre compte est suspendu"),
])
async def test_campaigns_are_refused_when_bob_is_paused(client, db_session, paid_until, active, message):
    shop = await _shop(db_session, paid_until=paid_until, active=active)
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]
    r = await client.post("/api/v1/campaigns/send", headers={"Authorization": f"Bearer {token}"},
                          json={"subject": "Soldes", "body_text": "Remise", "whatsapp_cta_message": "Je veux"})
    assert r.status_code == 403 and message in r.json()["detail"]


@pytest.mark.asyncio
async def test_campaigns_still_work_on_the_grace_day(client, db_session):
    shop = await _shop(db_session, paid_until=date.today() - timedelta(days=1))
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]
    r = await client.post("/api/v1/campaigns/send", headers={"Authorization": f"Bearer {token}"},
                          json={"subject": "Soldes", "body_text": "Remise", "whatsapp_cta_message": "Je veux"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_appointment_reminders_stop_when_bob_is_paused():
    from app.tests.test_appointments import SAT_10H_PARIS
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    evening = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    async with factory() as db:
        ids = []
        for paid_until in (date(2026, 9, 29), date(2026, 10, 1), None):  # en pause, jour de grâce, sans échéance
            tenant = Tenant(name="A", country="FR", currency="EUR", email=f"{uuid.uuid4().hex[:6]}@l51.fr",
                            plan=TenantPlan.PRO, paid_until=paid_until)
            db.add(tenant)
            await db.flush()
            cust = Customer(tenant_id=tenant.id, whatsapp_number="33600000009")
            db.add(cust)
            await db.flush()
            conv = Conversation(tenant_id=tenant.id, customer_id=cust.id)
            db.add(conv)
            await db.flush()
            db.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=cust.id, kind="VISITE",
                                      availability="x", status="CONFIRMED", scheduled_at=SAT_10H_PARIS))
            ids.append(tenant.email)
        await db.commit()
    sent = []
    report = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening,
                                                            send=lambda **kw: sent.append(kw["to"]) or True)
    assert report["staff"] == 2 and sorted(sent) == sorted(ids[1:])
    await engine.dispose()


# --- B. Emails d'échéance au commerçant ------------------------------------------------------------------------

class Mailbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, **mail):
        self.sent.append(mail)
        return self.ok


@pytest.mark.asyncio
async def test_each_billing_email_goes_once(db_session):
    from app.workers.billing import check_billing

    db_session.add(SuperAdminUser(email="admin@bob.internal", hashed_password="x", full_name="A"))
    end = date(2026, 10, 15)
    shop = await _shop(db_session, paid_until=end)
    mailbox = Mailbox()

    assert await check_billing(db_session, _at(end - timedelta(days=8)), mailbox) == []
    assert await check_billing(db_session, _at(end - timedelta(days=7)), mailbox) == [("Auto Abidjan", "WARNING")]
    assert await check_billing(db_session, _at(end), mailbox) == []
    assert await check_billing(db_session, _at(end + timedelta(days=1)), mailbox) == [("Auto Abidjan", "GRACE")]
    assert await check_billing(db_session, _at(end + timedelta(days=1), 18), mailbox) == []
    assert await check_billing(db_session, _at(end + timedelta(days=2)), mailbox) == [("Auto Abidjan", "PAUSED")]
    assert await check_billing(db_session, _at(end + timedelta(days=9)), mailbox) == []

    subjects = [m["subject"] for m in mailbox.sent]
    # Lot 60 : 7 jours avant, c'est le rapport mensuel qui part, avec le rappel d'échéance.
    assert subjects == ["Votre mois avec Bob",
                        "Dernier rappel : Bob se met en pause demain",
                        "Bob est en pause : abonnement à renouveler"]
    assert all(m["to"] == shop.email and m["reply_to"] == "admin@bob.internal" and m["from_name"] == "Bob" for m in mailbox.sent)
    assert "sans renouvellement, Bob se mettra en pause le 17 octobre 2026" in mailbox.sent[0]["body"]
    assert "Votre abonnement se termine le 15 octobre 2026" in mailbox.sent[0]["html"]
    assert "Bob répond encore à vos clients aujourd'hui" in mailbox.sent[1]["body"]
    assert "Dès le renouvellement, Bob reprend automatiquement" in mailbox.sent[2]["body"]

    # Renouvelé : la nouvelle échéance a ses propres emails.
    shop.paid_until = date(2026, 11, 15)
    await db_session.commit()
    assert await check_billing(db_session, _at(date(2026, 11, 8)), mailbox) == [("Auto Abidjan", "WARNING")]


@pytest.mark.asyncio
async def test_a_date_already_past_sends_only_the_pause_email(db_session):
    from app.workers.billing import check_billing

    await _shop(db_session, paid_until=date(2026, 9, 1))
    mailbox = Mailbox()
    assert await check_billing(db_session, _at(date(2026, 10, 3)), mailbox) == [("Auto Abidjan", "PAUSED")]
    assert len(mailbox.sent) == 1 and mailbox.sent[0]["reply_to"] is None


@pytest.mark.asyncio
async def test_failed_billing_email_is_retried(db_session):
    from app.workers.billing import check_billing

    shop = await _shop(db_session, paid_until=date(2026, 10, 10))
    assert await check_billing(db_session, _at(date(2026, 10, 5)), Mailbox(ok=False)) == []
    assert (await _fresh(db_session, shop)).billing_notice_stage is None

    def boom(**mail):
        raise RuntimeError("SMTP")

    assert await check_billing(db_session, _at(date(2026, 10, 5)), boom) == []
    assert await check_billing(db_session, _at(date(2026, 10, 5)), Mailbox()) == [("Auto Abidjan", "WARNING")]
    shop = await _fresh(db_session, shop)
    assert (shop.billing_notice_stage, shop.billing_notice_for) == ("WARNING", date(2026, 10, 10))


@pytest.mark.asyncio
async def test_no_billing_email_without_date_suspended_or_demo(db_session):
    from app.workers.billing import check_billing

    await _shop(db_session)
    await _shop(db_session, paid_until=date(2026, 10, 1), active=False)
    demo = await _shop(db_session, paid_until=date(2026, 10, 1))
    demo.is_demo = True
    await db_session.commit()
    mailbox = Mailbox()
    assert await check_billing(db_session, _at(date(2026, 10, 3)), mailbox) == [] and mailbox.sent == []


# --- API : tableau de bord et Super Admin ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_gets_bob_status(client, db_session):
    shop = await _shop(db_session, paid_until=date.today() - timedelta(days=4))
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]
    me = (await client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["bob_status"]["paused"] is True and me["bob_status"]["reason"] == "UNPAID"
    assert me["bob_status"]["paid_until"] == (date.today() - timedelta(days=4)).isoformat()


async def _admin(client):
    r = await client.post("/api/v1/superadmin/bootstrap", json={"email": "a@bob.internal", "password": "supersecret123", "full_name": "A"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_superadmin_sets_and_clears_the_paid_until_date(client, db_session):
    shop = await _shop(db_session)
    headers = await _admin(client)

    r = await client.put(f"/api/v1/superadmin/tenants/{shop.id}/paid-until", headers=headers,
                         json={"paid_until": (date.today() - timedelta(days=3)).isoformat()})
    assert r.status_code == 200 and r.json()["bob_status"]["reason"] == "UNPAID"
    listed = {t["id"]: t for t in (await client.get("/api/v1/superadmin/tenants", headers=headers)).json()}
    assert listed[str(shop.id)]["paid_until"] == (date.today() - timedelta(days=3)).isoformat()

    r = await client.put(f"/api/v1/superadmin/tenants/{shop.id}/paid-until", headers=headers, json={"paid_until": None})
    assert r.json()["paid_until"] is None and r.json()["bob_status"]["paused"] is False
    assert (await _fresh(db_session, shop)).paid_until is None

    missing = await client.put(f"/api/v1/superadmin/tenants/{uuid.uuid4()}/paid-until", headers=headers, json={"paid_until": None})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_only_the_superadmin_sets_the_date(client, db_session):
    shop = await _shop(db_session)
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]
    r = await client.put(f"/api/v1/superadmin/tenants/{shop.id}/paid-until", headers={"Authorization": f"Bearer {token}"},
                         json={"paid_until": "2030-01-01"})
    assert r.status_code in (401, 403)
    assert (await _fresh(db_session, shop)).paid_until is None


# --- Pages ----------------------------------------------------------------------------------------------------

def test_dashboard_banner_and_phone_replies():
    assert '<div id="bob-status-banner" class="bob-status-banner hidden" role="status"></div>' in DASHBOARD
    body = DASHBOARD[DASHBOARD.index("function renderBobStatus(st) {"):]
    body = body[:body.index("\n}\n")]
    assert "esc(st.message)" in body and 'classList.toggle("paused", !!st.paused)' in body
    assert "renderBobStatus(t.bob_status);" in DASHBOARD
    assert 'm.message_type === "phone_echo"' in DASHBOARD and "Vendeur · depuis le téléphone" in DASHBOARD


def test_superadmin_page_controls():
    assert "Payé jusqu'au" in SUPERADMIN and "changePaidUntil(" in SUPERADMIN and "toggleActive(" in SUPERADMIN
    assert "/paid-until" in SUPERADMIN and "En pause (impayé)" in SUPERADMIN and "Jour de grâce" in SUPERADMIN
    assert "colspan='10'" in SUPERADMIN and "colspan='9'" not in SUPERADMIN


def test_billing_task_is_registered_hourly():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.billing" in TASK_MODULES
    entry = celery_app.conf.beat_schedule["billing-notices"]
    assert entry["task"] == "app.workers.billing.check_billing_task"
    assert entry["schedule"].minute == {20} and len(entry["schedule"].hour) == 24
