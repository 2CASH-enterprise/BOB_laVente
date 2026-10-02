"""Lot 37b — tâches à faire (pastille rouge) et notifications sur l'appareil du commerçant."""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.push_subscription import NotificationState, PushSubscription
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import notifications
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE
from app.services.handoff_rules import RULE_LABELS
from app.services.home_service import home_summary

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
FCM = "https://fcm.googleapis.com/fcm/send/"
KEYS = {"p256dh": "B" + "A" * 86, "auth": "c2VjcmV0LWF1dGgxMjM"}
DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard"


@pytest.fixture
def push_on(monkeypatch):
    from app.scripts.generate_vapid_keys import generate

    public, private = generate()
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", public)
    monkeypatch.setattr(settings, "vapid_private_key", private)
    return public, private


class Shop:
    def __init__(self, db, tenant, user):
        self.db, self.tenant, self.user = db, tenant, user

    async def conversation(self, status=ConversationStatus.ACTIVE):
        customer = Customer(tenant_id=self.tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}", first_name="Fatou")
        self.db.add(customer)
        await self.db.flush()
        conv = Conversation(tenant_id=self.tenant.id, customer_id=customer.id, status=status)
        self.db.add(conv)
        await self.db.flush()
        return customer, conv

    def msg(self, conv, when, sender=MessageSender.CUSTOMER, kind="text", content="Bonjour"):
        self.db.add(Message(tenant_id=self.tenant.id, conversation_id=conv.id, sender=sender, message_type=kind,
                            content=content, created_at=when))

    async def outage(self, when):
        _, conv = await self.conversation()
        self.msg(conv, when)
        self.msg(conv, when, sender=MessageSender.SYSTEM, kind="ai_outage",
                 content=f"Panne du service d'IA — {RULE_LABELS['AI_OUTAGE_CALLBACK']}")
        return conv

    async def appointment(self, status="REQUESTED", scheduled=None, outcome=None, conv_status=ConversationStatus.ACTIVE, **extra):
        customer, conv = await self.conversation(conv_status)
        a = AppointmentRequest(tenant_id=self.tenant.id, conversation_id=conv.id, customer_id=customer.id, kind="ESSAI",
                               availability="samedi", status=status, scheduled_at=scheduled, outcome=outcome, **extra)
        self.db.add(a)
        await self.db.flush()
        return a

    async def order(self, status=OrderStatus.PENDING, when=None):
        customer, conv = await self.conversation()
        self.db.add(Order(tenant_id=self.tenant.id, customer_id=customer.id, conversation_id=conv.id, status=status,
                          total_amount=5000, currency="XOF", created_at=when or NOW - timedelta(hours=1)))

    async def counts(self):
        await self.db.commit()
        return await notifications.task_counts(self.db, self.tenant, NOW)


async def _shop(db, business_type=ONLINE_STORE, email=None) -> Shop:
    email = email or f"s{uuid.uuid4().hex[:6]}@example.com"
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db.add(tenant)
    await db.flush()
    user = User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER)
    db.add(user)
    await db.commit()
    return Shop(db, tenant, user)


async def _headers(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --- Compteurs : ce qu'il reste à faire --------------------------------------------------------

@pytest.mark.asyncio
async def test_store_counts_waiting_conversations_and_pending_orders(db_session):
    shop = await _shop(db_session)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await shop.conversation(ConversationStatus.ACTIVE)
    await shop.order(OrderStatus.PENDING)
    await shop.order(OrderStatus.PAID)
    await shop.order(OrderStatus.CANCELLED)

    c = await shop.counts()

    assert c == {"conversations": 2, "appointments": 0, "outcomes": 0, "orders": 1, "callbacks": 0, "total": 3}


@pytest.mark.asyncio
async def test_dealership_counts_appointments_and_never_orders(db_session):
    shop = await _shop(db_session, CAR_DEALERSHIP)
    await shop.appointment("REQUESTED")
    await shop.appointment("REQUESTED", conv_status=ConversationStatus.WAITING_HUMAN)  # comptée une fois (conversation)
    await shop.appointment("CANCELLED")
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(hours=3))                    # passé, sans issue
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(hours=3), outcome="SOLD")    # issue indiquée
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(minutes=30))                 # moins d'1 h
    await shop.appointment("CONFIRMED", scheduled=NOW + timedelta(days=1))                     # à venir
    await shop.order(OrderStatus.PENDING)  # ne devrait pas exister en concession : jamais compté

    c = await shop.counts()

    assert c["appointments"] == 1 and c["conversations"] == 1 and c["outcomes"] == 1
    assert c["orders"] == 0 and c["total"] == 3


@pytest.mark.asyncio
async def test_store_never_counts_appointments(db_session):
    shop = await _shop(db_session)
    await shop.appointment("REQUESTED")
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(hours=3))
    c = await shop.counts()
    assert c["appointments"] == 0 and c["outcomes"] == 0 and c["total"] == 0


@pytest.mark.asyncio
async def test_callbacks_after_outage_until_a_human_answers(db_session):
    shop = await _shop(db_session)
    await shop.outage(NOW - timedelta(hours=3))
    answered = await shop.outage(NOW - timedelta(hours=5))
    shop.msg(answered, NOW - timedelta(hours=4), sender=MessageSender.HUMAN, content="Je vous rappelle")
    await shop.outage(NOW - timedelta(hours=60))  # plus de 48 h : n'est plus affiché à l'accueil
    waiting = await shop.outage(NOW - timedelta(hours=2))
    waiting.status = ConversationStatus.WAITING_HUMAN  # déjà comptée comme conversation

    c = await shop.counts()

    assert c["callbacks"] == 1 and c["conversations"] == 1


@pytest.mark.asyncio
async def test_manual_followups_after_visit_are_callbacks(db_session):
    shop = await _shop(db_session, CAR_DEALERSHIP)
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(days=3), outcome="FOLLOW_UP",
                           followup_channel="TASK", followup_sent_at=NOW - timedelta(days=1))
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(days=12), outcome="NO_SHOW",
                           followup_channel="TASK", followup_sent_at=NOW - timedelta(days=10))  # plus de 7 jours
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(days=3), outcome="FOLLOW_UP",
                           followup_channel="EMAIL", followup_sent_at=NOW - timedelta(days=1))  # déjà relancé par email
    c = await shop.counts()
    assert c["callbacks"] == 1


@pytest.mark.asyncio
async def test_counts_never_include_another_shop(db_session):
    mine = await _shop(db_session)
    other = await _shop(db_session)
    await other.conversation(ConversationStatus.WAITING_HUMAN)
    await other.order()
    await other.outage(NOW - timedelta(hours=1))
    assert (await mine.counts())["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("business_type", [ONLINE_STORE, CAR_DEALERSHIP])
async def test_counts_match_the_home_to_do_list(db_session, business_type):
    """Même règles que « À traiter maintenant » de l'accueil (hors commandes : toutes celles en attente)."""
    shop = await _shop(db_session, business_type)
    for _ in range(2):
        conv = (await shop.conversation(ConversationStatus.WAITING_HUMAN))[1]
        shop.msg(conv, NOW - timedelta(hours=2))
    await shop.outage(NOW - timedelta(hours=3))
    if business_type == CAR_DEALERSHIP:
        await shop.appointment("REQUESTED")
        await shop.appointment("REQUESTED")
        await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(days=3), outcome="FOLLOW_UP",
                               followup_channel="TASK", followup_sent_at=NOW - timedelta(days=1))
    c = await shop.counts()
    todo = (await home_summary(db_session, shop.tenant.id, now=NOW))["todo"]
    kinds = [t["kind"] for t in todo]
    assert c["conversations"] == kinds.count("CONVERSATION")
    assert c["appointments"] == kinds.count("APPOINTMENT")
    assert c["callbacks"] == kinds.count("CALLBACK")


# --- API ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_counts_route_requires_login_and_returns_my_counts(client, db_session):
    shop = await _shop(db_session)
    other = await _shop(db_session)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await other.conversation(ConversationStatus.WAITING_HUMAN)
    await other.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    assert (await client.get("/api/v1/notifications/counts")).status_code == 401
    r = await client.get("/api/v1/notifications/counts", headers=await _headers(client, shop.user.email))
    assert r.status_code == 200 and r.json()["conversations"] == 1 and r.json()["total"] == 1
    state = await db_session.get(NotificationState, shop.tenant.id)
    await db_session.refresh(state)
    assert state.counts["conversations"] == 1  # vu : plus de notification pour celle-ci


@pytest.mark.asyncio
async def test_push_config_without_keys(client, db_session):
    shop = await _shop(db_session)
    r = await client.get("/api/v1/notifications/push-config", headers=await _headers(client, shop.user.email))
    assert r.json() == {"enabled": False, "public_key": None}
    r = await client.post("/api/v1/notifications/subscriptions", headers=await _headers(client, shop.user.email),
                          json={"endpoint": FCM + "abc", "keys": KEYS})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_push_config_gives_only_public_key(client, db_session, push_on):
    public, private = push_on
    shop = await _shop(db_session)
    r = await client.get("/api/v1/notifications/push-config", headers=await _headers(client, shop.user.email))
    assert r.json() == {"enabled": True, "public_key": public}
    assert private not in r.text


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe(client, db_session, push_on):
    shop = await _shop(db_session)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    h = await _headers(client, shop.user.email)
    r = await client.post("/api/v1/notifications/subscriptions", headers=h, json={"endpoint": FCM + "abc", "keys": KEYS})
    assert r.status_code == 204
    [sub] = (await db_session.execute(select(PushSubscription))).scalars().all()
    assert sub.user_id == shop.user.id and sub.tenant_id == shop.tenant.id
    state = await db_session.get(NotificationState, shop.tenant.id)
    assert state.counts["conversations"] == 1  # point de départ : pas d'avalanche pour les tâches déjà là
    # deux fois le même appareil : une seule ligne
    await client.post("/api/v1/notifications/subscriptions", headers=h, json={"endpoint": FCM + "abc", "keys": KEYS})
    assert len((await db_session.execute(select(PushSubscription))).scalars().all()) == 1
    r = await client.post("/api/v1/notifications/subscriptions/delete", headers=h, json={"endpoint": FCM + "abc"})
    assert r.status_code == 204
    assert (await db_session.execute(select(PushSubscription).execution_options(populate_existing=True))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abc",           # pas https
    "https://localhost/push",                           # adresse interne
    "https://169.254.169.254/latest/meta-data",         # adresse interne
    "https://redis:6379/x",
    "https://fcm.googleapis.com.evil.com/x",            # faux domaine
    "https://evilfcm.googleapis.com.example/x",
    "https://fcm.googleapis.com:8443/x",                # autre port
    "https://user:pw@fcm.googleapis.com/x",
    "https://example.com/push",
])
async def test_only_browser_push_services_are_accepted(client, db_session, push_on, endpoint):
    shop = await _shop(db_session)
    r = await client.post("/api/v1/notifications/subscriptions", headers=await _headers(client, shop.user.email),
                          json={"endpoint": endpoint, "keys": KEYS})
    assert r.status_code == 422
    assert (await db_session.execute(select(PushSubscription))).scalars().all() == []


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://web.push.apple.com/QGx",
    "https://wns2-par02p.notify.windows.com/w/?token=abc",
])
def test_known_push_services_are_accepted(endpoint):
    assert notifications.validate_endpoint(endpoint) == endpoint


@pytest.mark.asyncio
async def test_subscription_keys_are_validated(client, db_session, push_on):
    shop = await _shop(db_session)
    h = await _headers(client, shop.user.email)
    for keys in ({"p256dh": "court", "auth": KEYS["auth"]}, {"p256dh": KEYS["p256dh"], "auth": "a b;c<d>e"}):
        r = await client.post("/api/v1/notifications/subscriptions", headers=h, json={"endpoint": FCM + "x", "keys": keys})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_device_moves_to_the_last_account_and_others_cannot_delete_it(client, db_session, push_on):
    a = await _shop(db_session)
    b = await _shop(db_session)
    ha, hb = await _headers(client, a.user.email), await _headers(client, b.user.email)
    await client.post("/api/v1/notifications/subscriptions", headers=ha, json={"endpoint": FCM + "shared", "keys": KEYS})
    await client.post("/api/v1/notifications/subscriptions/delete", headers=hb, json={"endpoint": FCM + "shared"})
    [sub] = (await db_session.execute(select(PushSubscription))).scalars().all()
    assert sub.tenant_id == a.tenant.id  # B ne peut pas retirer l'appareil de A
    await client.post("/api/v1/notifications/subscriptions", headers=hb, json={"endpoint": FCM + "shared", "keys": KEYS})
    [sub] = (await db_session.execute(select(PushSubscription).execution_options(populate_existing=True))).scalars().all()
    assert sub.tenant_id == b.tenant.id and sub.user_id == b.user.id  # même ordinateur, autre compte


@pytest.mark.asyncio
async def test_at_most_ten_devices_per_user(client, db_session, push_on):
    shop = await _shop(db_session)
    h = await _headers(client, shop.user.email)
    for i in range(12):
        await client.post("/api/v1/notifications/subscriptions", headers=h, json={"endpoint": FCM + f"d{i}", "keys": KEYS})
    subs = (await db_session.execute(select(PushSubscription))).scalars().all()
    assert len(subs) == 10
    assert FCM + "d11" in {s.endpoint for s in subs}


# --- Envoi ----------------------------------------------------------------------------------

def _sub(shop, endpoint="abc", user=None):
    sub = PushSubscription(tenant_id=shop.tenant.id, user_id=(user or shop.user).id, endpoint=FCM + endpoint, **KEYS)
    shop.db.add(sub)
    return sub


class Sender:
    def __init__(self, code=201):
        self.code, self.calls = code, []

    def __call__(self, sub, data):
        self.calls.append((sub.endpoint, json.loads(data)))
        return self.code(sub) if callable(self.code) else self.code


@pytest.mark.asyncio
async def test_new_task_notifies_once(db_session, push_on):
    shop = await _shop(db_session)
    _sub(shop)
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())  # point de départ
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    sender = Sender()

    report = await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)

    assert report["sent"] == 1 and report["new"] == ["conversations"]
    [(endpoint, payload)] = sender.calls
    assert payload == {"title": "Un client attend votre réponse", "body": "1 tâche à traiter dans Bob.", "count": 1, "tag": "bob-tasks"}
    again = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=again)
    assert again.calls == []  # même tâche : jamais deux fois


@pytest.mark.asyncio
async def test_finished_task_then_new_one_notifies_again(db_session, push_on):
    shop = await _shop(db_session)
    _sub(shop)
    _, conv = await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    conv.status = ConversationStatus.ACTIVE  # tâche faite
    await db_session.commit()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    conv.status = ConversationStatus.WAITING_HUMAN  # nouvelle demande
    await db_session.commit()
    sender = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_notification_text_is_general_and_titled_by_priority(db_session, push_on):
    shop = await _shop(db_session, CAR_DEALERSHIP)
    _sub(shop)
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    customer, _ = await shop.conversation()
    customer.first_name = "Moussa"
    await shop.appointment("REQUESTED")
    await shop.appointment("CONFIRMED", scheduled=NOW - timedelta(hours=3))
    await db_session.commit()
    sender = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    [(_, payload)] = sender.calls
    assert payload["title"] == "Nouveau rendez-vous à confirmer"
    assert payload["body"] == "2 tâches à traiter dans Bob." and payload["count"] == 2
    assert "Moussa" not in json.dumps(payload) and "Fatou" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_without_keys_nothing_is_sent(db_session):
    shop = await _shop(db_session)
    _sub(shop)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    sender = Sender()
    report = await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    assert sender.calls == [] and report["sent"] == 0


@pytest.mark.asyncio
async def test_only_active_users_of_this_shop_are_notified(db_session, push_on):
    shop = await _shop(db_session)
    other = await _shop(db_session)
    inactive = User(tenant_id=shop.tenant.id, email=f"i{uuid.uuid4().hex[:5]}@example.com", hashed_password="x",
                    full_name="I", role=Role.AGENT, active=False)
    db_session.add(inactive)
    await db_session.flush()
    _sub(shop, "mine")
    _sub(shop, "inactive", user=inactive)
    _sub(other, "other")
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    sender = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    assert [c[0] for c in sender.calls] == [FCM + "mine"]


@pytest.mark.asyncio
async def test_suspended_shop_is_never_notified(db_session, push_on):
    shop = await _shop(db_session)
    _sub(shop)
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    shop.tenant.active = False
    await db_session.commit()
    sender = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    assert sender.calls == []


@pytest.mark.asyncio
async def test_gone_devices_are_forgotten_and_failures_counted(db_session, push_on):
    shop = await _shop(db_session)
    _sub(shop, "gone")
    flaky = _sub(shop, "flaky")
    _sub(shop, "ok")
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    codes = {FCM + "gone": 410, FCM + "flaky": 500, FCM + "ok": 201}
    for i in range(notifications.MAX_FAILURES):
        _, conv = await shop.conversation(ConversationStatus.WAITING_HUMAN)
        await db_session.commit()
        report = await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender(lambda s: codes[s.endpoint]))
        if i == 0:
            assert report == {"sent": 1, "removed": 1, "failed": 1, "new": ["conversations"]}
            await db_session.refresh(flaky)
            assert flaky.failures == 1
    left = {s.endpoint for s in (await db_session.execute(select(PushSubscription))).scalars().all()}
    assert left == {FCM + "ok"}  # 5 échecs de suite : oublié


@pytest.mark.asyncio
async def test_sender_crash_never_blocks_other_devices(db_session, push_on):
    shop = await _shop(db_session)
    _sub(shop, "a")
    _sub(shop, "b")
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    sent = []

    def crashy(sub, data):
        if sub.endpoint.endswith("a"):
            raise RuntimeError("réseau")
        sent.append(sub.endpoint)
        return 201

    report = await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=crashy)
    assert sent == [FCM + "b"] and report["failed"] == 1


@pytest.mark.asyncio
async def test_real_web_push_is_encrypted_and_signed(db_session, push_on, monkeypatch):
    """Envoi réel avec pywebpush (réseau simulé) : le message se déchiffre avec la clé de l'appareil."""
    import http_ece
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from jose import jwt as jose_jwt

    public, _ = push_on
    device_key = ec.generate_private_key(ec.SECP256R1())
    device_pub = device_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth_secret = b"0123456789abcdef"
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    shop = await _shop(db_session)
    sub = PushSubscription(tenant_id=shop.tenant.id, user_id=shop.user.id, endpoint=FCM + "real",
                           p256dh=b64(device_pub), auth=b64(auth_secret))
    captured = {}

    class Resp:
        status_code = 201
        text = ""

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        captured.update(url=url, data=data, headers=headers)
        return Resp()

    import pywebpush

    monkeypatch.setattr(pywebpush.requests, "post", fake_post)
    code = notifications._send_webpush(sub, json.dumps({"title": "Test", "count": 3}))

    assert code == 201 and captured["url"] == FCM + "real"
    clear = http_ece.decrypt(captured["data"], private_key=device_key, auth_secret=auth_secret, version="aes128gcm")
    assert json.loads(clear) == {"title": "Test", "count": 3}
    assert captured["headers"]["ttl"] == str(notifications.TTL_SECONDS)
    authorization = captured["headers"]["Authorization"]
    assert authorization.startswith("vapid t=") and f"k={public}" in authorization
    token = authorization.split("t=", 1)[1].split(",", 1)[0]
    claims = jose_jwt.get_unverified_claims(token)
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"].startswith("mailto:")


def test_generated_keys_have_the_right_format():
    from py_vapid import Vapid

    from app.scripts.generate_vapid_keys import generate

    public, private = generate()
    pub_raw = base64.urlsafe_b64decode(public + "==")
    assert len(pub_raw) == 65 and pub_raw[0] == 4
    assert len(base64.urlsafe_b64decode(private + "=")) == 32
    vapid = Vapid.from_string(private)
    from cryptography.hazmat.primitives import serialization

    assert vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint) == pub_raw


def test_key_script_refuses_to_replace_existing_keys(monkeypatch, capsys):
    from app.scripts import generate_vapid_keys

    monkeypatch.setattr(get_settings(), "vapid_private_key", "deja-la")
    monkeypatch.setattr("sys.argv", ["x"])
    assert generate_vapid_keys.main() == 1
    assert "VAPID_PRIVATE_KEY" not in capsys.readouterr().out


# --- Déclenchement ---------------------------------------------------------------------------

def test_queue_check_only_when_push_is_configured(monkeypatch, push_on):
    from app.workers import notifications as worker

    queued = []
    monkeypatch.setattr(worker.check_tenant_task, "delay", lambda tid: queued.append(tid))
    notifications.queue_check(uuid.UUID(int=1))
    assert queued == [str(uuid.UUID(int=1))]
    monkeypatch.setattr(get_settings(), "vapid_private_key", "")
    notifications.queue_check(uuid.UUID(int=2))
    assert len(queued) == 1


def test_queue_check_never_raises(monkeypatch, push_on):
    from app.workers import notifications as worker

    def boom(tid):
        raise ConnectionError("redis")

    monkeypatch.setattr(worker.check_tenant_task, "delay", boom)
    notifications.queue_check(uuid.UUID(int=1))


@pytest.mark.asyncio
async def test_whatsapp_message_triggers_a_check(client, db_session, monkeypatch):
    from app.tests.test_whatsapp_webhook import _incoming_message_payload, _setup_tenant_with_whatsapp

    tenant = await _setup_tenant_with_whatsapp(db_session, "wa37b@example.com", "PN37B")
    checked = []
    monkeypatch.setattr(notifications, "queue_check", lambda tid: checked.append(tid))
    r = await client.post("/webhooks/whatsapp", json=_incoming_message_payload("PN37B", "221700000099", "Bonjour"))
    assert r.status_code == 200
    assert checked == [tenant.id]


@pytest.mark.asyncio
async def test_periodic_check_covers_only_shops_with_devices(db_session, push_on):
    from app.workers.notifications import check_all

    with_device = await _shop(db_session)
    without = await _shop(db_session)
    _sub(with_device)
    await with_device.conversation(ConversationStatus.WAITING_HUMAN)
    await without.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, class_=AsyncSession)
    sender = Sender()

    report = await check_all(factory, sender=sender)

    assert report == {"tenants": 1, "sent": 1, "failed": 0}
    assert await db_session.get(NotificationState, without.tenant.id) is None


def test_periodic_check_is_scheduled():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.notifications" in TASK_MODULES
    assert celery_app.conf.beat_schedule["notifications-check"]["task"] == "app.workers.notifications.check_all_task"


# --- Appareils oubliés avec les sessions --------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_all_devices_also_stops_notifications(client, db_session, push_on):
    shop = await _shop(db_session)
    other = await _shop(db_session)
    _sub(shop, "phone")
    _sub(other, "other")
    await db_session.commit()
    r = await client.post("/api/v1/auth/sessions/revoke-all", headers=await _headers(client, shop.user.email))
    assert r.status_code == 204
    left = (await db_session.execute(select(PushSubscription.endpoint))).scalars().all()
    assert left == [FCM + "other"]


# --- Tableau de bord et service worker ---------------------------------------------------------

def test_service_worker_shows_notification_and_badge():
    sw = (DASHBOARD / "sw.js").read_text(encoding="utf-8")
    assert 'addEventListener("push"' in sw and "showNotification" in sw and "setAppBadge" in sw
    assert 'addEventListener("notificationclick"' in sw and "openWindow" in sw


def test_dashboard_has_badge_counts_and_push_card():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert "/api/v1/notifications/counts" in html and "navigator.setAppBadge" in html
    assert 'id="push-card"' in html and "enablePush()" in html and "disablePush()" in html
    assert 'id="menu-count"' in html
    assert "forgetPushDevice(token)" in html  # déconnexion : cet appareil ne reçoit plus rien
    assert "userVisibleOnly: true" in html


@pytest.mark.asyncio
async def test_human_reply_before_the_outage_does_not_clear_the_callback(db_session):
    shop = await _shop(db_session)
    conv = await shop.outage(NOW - timedelta(hours=3))
    shop.msg(conv, NOW - timedelta(hours=10), sender=MessageSender.HUMAN, content="Ancienne réponse")
    assert (await shop.counts())["callbacks"] == 1


@pytest.mark.asyncio
async def test_device_is_used_only_for_its_own_shop(db_session, push_on):
    shop = await _shop(db_session)
    other = await _shop(db_session)
    # ligne incohérente (appareil rangé chez une autre boutique) : jamais utilisée
    db_session.add(PushSubscription(tenant_id=other.tenant.id, user_id=shop.user.id, endpoint=FCM + "odd", **KEYS))
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=Sender())
    await shop.conversation(ConversationStatus.WAITING_HUMAN)
    await db_session.commit()
    sender = Sender()
    await notifications.check_tenant(db_session, shop.tenant.id, NOW, sender=sender)
    assert sender.calls == []
