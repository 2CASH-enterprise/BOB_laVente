"""
Lot 52 — brancher Bob sur un numéro déjà utilisé dans l'application WhatsApp Business (coexistence) :
connexion, contacts du téléphone (servent seulement à nommer les clients), historique des 6 derniers mois
(rangé à part, mémoire de Bob, jamais une conversation).
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.main import app
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.phone_history import PhoneContact, PhoneHistoryMessage
from app.models.whatsapp_account import WhatsAppAccount
from app.services import coexistence
from app.tests.test_lot51_pauses import LLM, _shop, customer_payload

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
T0 = 1759400000  # 2 octobre 2025


def contacts_payload(pnid, *items):
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "smb_app_state_sync", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid}, "state_sync": list(items)}}]}]}


def contact(number, full_name=None, first_name=None, action="add"):
    body = {"phone_number": number}
    if full_name:
        body["full_name"] = full_name
    if first_name:
        body["first_name"] = first_name
    return {"type": "contact", "contact": body, "action": action, "metadata": {"timestamp": "1"}}


def history_payload(pnid, threads=None, progress=100, errors=None):
    chunk = {"metadata": {"phase": 0, "chunk_order": 1, "progress": progress}, "threads": threads or []}
    if errors is not None:
        chunk = {"errors": errors}
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "history", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid}, "history": [chunk]}}]}]}


def hmsg(sender, text, offset=0, msg_id=None, kind="text", extra=None):
    m = {"from": sender, "id": msg_id or f"wamid.h{uuid.uuid4().hex[:10]}", "timestamp": str(T0 + offset), "type": kind,
         "history_context": {"status": "READ"}}
    if kind == "text":
        m["text"] = {"body": text}
    m.update(extra or {})
    return m


SHOP_NUMBER = "2250700000000"


# --- Lecture des webhooks -------------------------------------------------------------------------------------

def test_parse_contacts():
    payload = contacts_payload("pn", contact("+225 07 11 22 33 44", "Awa Koné", "Awa"), contact("2250711", action="remove"),
                               {"type": "other"}, contact(""), contact("2250799", "Sans prénom"))
    assert coexistence.parse_contacts(payload) == [
        {"phone_number_id": "pn", "action": "add", "wa_number": "2250711223344", "full_name": "Awa Koné", "first_name": "Awa"},
        {"phone_number_id": "pn", "action": "remove", "wa_number": "2250711", "full_name": None, "first_name": None},
        {"phone_number_id": "pn", "action": "add", "wa_number": "2250799", "full_name": "Sans prénom", "first_name": None},
    ]
    assert coexistence.parse_contacts(contacts_payload(None, contact("2250711", "A"))) == []
    other_kind = {"type": "label", "contact": {"phone_number": "2250711", "full_name": "Étiquette"}, "action": "add"}
    assert coexistence.parse_contacts(contacts_payload("pn", other_kind)) == []
    assert coexistence.parse_contacts(customer_payload("pn", "225")) == [] and coexistence.parse_contacts([]) == []


def test_parse_history():
    threads = [{"id": "2250711111111", "messages": [
        hmsg("+2250711111111", "La Corolla est dispo ?", 0, "w1"),
        hmsg(SHOP_NUMBER, "Oui, passez demain", 60, "w2"),
        hmsg(SHOP_NUMBER, "", 70, "w3", kind="image", extra={"image": {"caption": "La voici"}}),
        hmsg("2250711111111", "", 80, "w4", kind="reaction", extra={"reaction": {"emoji": "👍"}}),
        {"from": "2250711111111", "id": "w5", "type": "text", "text": {"body": "sans date"}},
        {"from": "2250711111111", "timestamp": "1", "type": "text", "text": {"body": "sans identifiant"}},
    ]}, {"id": "", "messages": [hmsg("1", "x")]}]
    [chunk] = coexistence.parse_history(history_payload("pn", threads, progress=55))
    assert (chunk["phone_number_id"], chunk["declined"], chunk["progress"]) == ("pn", False, 55)
    assert [(m["wa_message_id"], m["from_business"], m["content"]) for m in chunk["messages"]] == [
        ("w1", False, "La Corolla est dispo ?"), ("w2", True, "Oui, passez demain"), ("w3", True, "📷 Photo : La voici")]
    assert chunk["messages"][0]["sent_at"] == datetime.fromtimestamp(T0, tz=timezone.utc)
    assert chunk["messages"][0]["wa_number"] == "2250711111111"

    [declined] = coexistence.parse_history(history_payload("pn", errors=[{"code": 2593109, "title": "off"}]))
    assert declined["declined"] and declined["errors"] and declined["messages"] == []
    [other] = coexistence.parse_history(history_payload("pn", errors=[{"code": 1, "title": "autre"}]))
    assert not other["declined"] and other["errors"]
    assert coexistence.parse_history(history_payload(None, threads)) == []
    assert coexistence.parse_history(customer_payload("pn", "225")) == [] and coexistence.parse_history({}) == []
    [no_progress] = coexistence.parse_history(history_payload("pn", [], progress="x"))
    assert no_progress["progress"] is None


def test_names_never_replace_a_known_name():
    c = Customer(first_name=None)
    assert coexistence.apply_contact_name(c, PhoneContact(full_name="Awa Koné Traoré", first_name="Awa"))
    assert (c.first_name, c.last_name) == ("Awa", "Koné Traoré")
    c2 = Customer(first_name=None)
    assert coexistence.apply_contact_name(c2, PhoneContact(full_name="Moussa", first_name=None))
    assert (c2.first_name, c2.last_name) == ("Moussa", None)
    c3 = Customer(first_name=None)
    assert coexistence.apply_contact_name(c3, PhoneContact(full_name="Ali Diallo", first_name=None))
    assert (c3.first_name, c3.last_name) == ("Ali", "Diallo")
    c4 = Customer(first_name=None)
    assert coexistence.apply_contact_name(c4, PhoneContact(full_name="Koné Awa", first_name="Awa"))
    assert (c4.first_name, c4.last_name) == ("Awa", None)  # le nom complet ne commence pas par le prénom
    known = Customer(first_name="Fatou", last_name=None)
    assert not coexistence.apply_contact_name(known, PhoneContact(full_name="Autre Nom", first_name="Autre"))
    assert known.first_name == "Fatou" and known.last_name is None
    assert not coexistence.apply_contact_name(Customer(first_name=None), PhoneContact(full_name=None, first_name=None))
    assert not coexistence.apply_contact_name(None, PhoneContact(full_name="A")) and not coexistence.apply_contact_name(Customer(), None)
    kept = Customer(first_name=None, last_name="Déjà")
    coexistence.apply_contact_name(kept, PhoneContact(full_name="Awa Koné", first_name="Awa"))
    assert kept.last_name == "Déjà"


# --- Contacts du téléphone -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contacts_name_customers_and_never_create_any(client, db_session):
    shop, other = await _shop(db_session), await _shop(db_session)
    unnamed = Customer(tenant_id=shop.id, whatsapp_number="2250711111111")
    named = Customer(tenant_id=shop.id, whatsapp_number="2250722222222", first_name="Fatou")
    elsewhere = Customer(tenant_id=other.id, whatsapp_number="2250711111111")
    db_session.add_all([unnamed, named, elsewhere])
    await db_session.commit()

    r = await client.post("/webhooks/whatsapp", json=contacts_payload(
        shop.pnid, contact("+225 07 11 11 11 11", "Awa Koné", "Awa"), contact("2250722222222", "Fatou Bamba", "Fatou B"),
        contact("2250733333333", "Maman", "Maman")))

    assert r.json() == {"status": "phone_contacts_recorded", "recorded": 3}
    await db_session.refresh(unnamed), await db_session.refresh(named), await db_session.refresh(elsewhere)
    assert (unnamed.first_name, unnamed.last_name) == ("Awa", "Koné")
    assert named.first_name == "Fatou" and elsewhere.first_name is None
    assert (await db_session.execute(select(func.count(Customer.id)))).scalar_one() == 3  # aucune fiche créée
    numbers = (await db_session.execute(select(PhoneContact.wa_number).where(PhoneContact.tenant_id == shop.id))).scalars().all()
    assert sorted(numbers) == ["2250711111111", "2250722222222", "2250733333333"]

    # Modification puis suppression sur le téléphone.
    await client.post("/webhooks/whatsapp", json=contacts_payload(shop.pnid, contact("2250733333333", "Maman Chérie", "Maman")))
    row = (await db_session.execute(select(PhoneContact).where(PhoneContact.wa_number == "2250733333333"))).scalar_one()
    await db_session.refresh(row)
    assert row.full_name == "Maman Chérie"
    r = await client.post("/webhooks/whatsapp", json=contacts_payload(shop.pnid, contact("2250733333333", action="remove"),
                                                                       contact("2250744444444", action="remove")))
    assert r.json()["recorded"] == 2
    assert (await db_session.execute(select(PhoneContact).where(PhoneContact.wa_number == "2250733333333"))).first() is None
    await db_session.refresh(unnamed)
    assert unnamed.first_name == "Awa"  # supprimer le contact ne touche jamais la fiche


@pytest.mark.asyncio
async def test_contacts_for_an_unknown_number_or_without_name(client, db_session):
    shop = await _shop(db_session)
    r = await client.post("/webhooks/whatsapp", json=contacts_payload("pn-inconnu", contact("2250711", "A", "A")))
    assert r.json()["recorded"] == 0
    r = await client.post("/webhooks/whatsapp", json=contacts_payload(shop.pnid, contact("2250711")))
    assert r.json()["recorded"] == 0 and (await db_session.execute(select(PhoneContact))).first() is None


@pytest.mark.asyncio
async def test_a_new_customer_gets_the_name_from_the_phone(client, db_session):
    shop = await _shop(db_session)
    db_session.add(PhoneContact(tenant_id=shop.id, wa_number="2250755555555", full_name="Ibrahim Touré", first_name="Ibrahim"))
    await db_session.commit()
    with LLM("Bonjour Ibrahim !"):
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, "2250755555555"))
    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "2250755555555"))).scalar_one()
    assert (customer.first_name, customer.last_name) == ("Ibrahim", "Touré")


# --- Historique du téléphone -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_is_stored_apart_once_and_tracks_progress(client, db_session):
    shop = await _shop(db_session)
    threads = [{"id": "2250711111111", "messages": [hmsg("2250711111111", "Bonjour", 0, "w1"), hmsg(SHOP_NUMBER, "Bonjour !", 5, "w2")]}]
    r = await client.post("/webhooks/whatsapp", json=history_payload(shop.pnid, threads, progress=40))
    assert r.json() == {"status": "phone_history_recorded", "recorded": 2}
    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == shop.id))).scalar_one()
    await db_session.refresh(account)
    assert (account.history_sync_status, account.history_progress) == ("IN_PROGRESS", 40)

    more = [{"id": "2250711111111", "messages": [hmsg("2250711111111", "Bonjour", 0, "w1"), hmsg("2250711111111", "Merci", 9, "w3")]}]
    r = await client.post("/webhooks/whatsapp", json=history_payload(shop.pnid, more, progress=100))
    assert r.json()["recorded"] == 1
    await db_session.refresh(account)
    assert (account.history_sync_status, account.history_progress) == ("DONE", 100)
    await client.post("/webhooks/whatsapp", json=history_payload(shop.pnid, [], progress=30))  # un morceau en retard
    await db_session.refresh(account)
    assert (account.history_sync_status, account.history_progress) == ("DONE", 100)

    rows = (await db_session.execute(select(PhoneHistoryMessage).order_by(PhoneHistoryMessage.sent_at))).scalars().all()
    assert [(r.wa_message_id, r.from_business, r.tenant_id) for r in rows] == [("w1", False, shop.id), ("w2", True, shop.id), ("w3", False, shop.id)]
    # Jamais une conversation ni une fiche client : rien dans les statistiques, le quota ou les relances.
    assert (await db_session.execute(select(Conversation))).first() is None
    assert (await db_session.execute(select(Customer))).first() is None


@pytest.mark.asyncio
async def test_history_declined_and_unknown_numbers(client, db_session):
    shop = await _shop(db_session)
    await client.post("/webhooks/whatsapp", json=history_payload(shop.pnid, errors=[{"code": 2593109, "title": "off"}]))
    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == shop.id))).scalar_one()
    await db_session.refresh(account)
    assert account.history_sync_status == "DECLINED"
    r = await client.post("/webhooks/whatsapp", json=history_payload("pn-inconnu", [{"id": "1", "messages": [hmsg("1", "x")]}]))
    assert r.json()["recorded"] == 0


@pytest.mark.asyncio
async def test_same_message_id_in_two_shops_is_kept_for_each(client, db_session):
    a, b = await _shop(db_session), await _shop(db_session)
    threads = [{"id": "2250711111111", "messages": [hmsg("2250711111111", "Bonjour", 0, "same")]}]
    await client.post("/webhooks/whatsapp", json=history_payload(a.pnid, threads))
    await client.post("/webhooks/whatsapp", json=history_payload(b.pnid, threads))
    assert (await db_session.execute(select(func.count(PhoneHistoryMessage.id)))).scalar_one() == 2


async def _history(db, shop, number, count, start=0):
    for i in range(count):
        db.add(PhoneHistoryMessage(tenant_id=shop.id, wa_number=number, from_business=i % 2 == 1, content=f"ancien {i}",
                                   wa_message_id=f"h{uuid.uuid4().hex}", sent_at=datetime(2026, 8, 12, 10, tzinfo=timezone.utc) + timedelta(minutes=start + i)))
    await db.commit()


@pytest.mark.asyncio
async def test_bob_remembers_the_phone_history(client, db_session):
    shop = await _shop(db_session)
    number = "2250766666666"
    db_session.add(PhoneHistoryMessage(tenant_id=shop.id, wa_number=number, from_business=True, content="Message du vendeur en premier",
                                       wa_message_id="first", sent_at=datetime(2026, 8, 12, 9, tzinfo=timezone.utc)))
    await _history(db_session, shop, number, 3)
    other = await _shop(db_session)
    await _history(db_session, other, number, 2, start=100)

    with LLM("Ravi de vous revoir !") as llm:
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number, "Elle est toujours là ?"))

    sent = llm.received_messages[0]
    assert sent[0] == {"role": "user", "content": "[Échange du 12/08/2026, avant Bob] ancien 0"}  # commence par le client
    assert sent[1] == {"role": "assistant", "content": "[Échange du 12/08/2026, avant Bob] ancien 1"}
    assert sent[2]["content"].endswith("ancien 2")
    assert all("Message du vendeur en premier" not in str(m["content"]) for m in sent)
    assert sent[-1]["role"] == "user" and "Elle est toujours là ?" in str(sent[-1]["content"])
    assert len(sent) == 4  # 3 anciens messages de CETTE boutique + le nouveau


@pytest.mark.asyncio
async def test_memory_is_capped_and_skipped_after_a_transfer(client, db_session):
    shop = await _shop(db_session)
    number = "2250777777777"
    await _history(db_session, shop, number, 30)
    with LLM("Oui.") as llm:
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number))
    sent = llm.received_messages[0]
    assert len(sent) <= 21 and sent[0]["role"] == "user" and "ancien" in sent[0]["content"]

    # Après un transfert à un humain, la mémoire repart de zéro : pas d'ancien historique.
    conv = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == shop.id))).scalar_one()
    db_session.add(Message(tenant_id=shop.id, conversation_id=conv.id, sender=MessageSender.SYSTEM, message_type="release",
                           content="rendue", created_at=datetime.now(timezone.utc)))
    db_session.add(Message(tenant_id=shop.id, conversation_id=conv.id, sender=MessageSender.SYSTEM, message_type="handoff",
                           content="Transmis", created_at=datetime.now(timezone.utc)))
    await db_session.commit()
    with LLM("D'accord.") as llm:
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number, "Encore moi"))
    assert all("ancien" not in str(m["content"]) for m in llm.received_messages[0])


@pytest.mark.asyncio
async def test_memory_messages_limits():
    assert await coexistence.memory_messages(None, uuid.uuid4(), "1", None, 0) == []


@pytest.mark.asyncio
async def test_conversation_shows_the_phone_history(client, db_session):
    shop = await _shop(db_session)
    number = "2250788888888"
    await _history(db_session, shop, number, 2)
    other = await _shop(db_session)
    await _history(db_session, other, number, 3)
    await _history(db_session, shop, "2250799999999", 1)
    with LLM("Bonjour"):
        await client.post("/webhooks/whatsapp", json=customer_payload(shop.pnid, number))
    conv = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == shop.id))).scalar_one()
    token = (await client.post("/api/v1/auth/login", data={"username": shop.email, "password": "x"})).json()["access_token"]

    detail = (await client.get(f"/api/v1/conversations/{conv.id}", headers={"Authorization": f"Bearer {token}"})).json()

    assert [(h["from_business"], h["content"]) for h in detail["phone_history"]] == [(False, "ancien 0"), (True, "ancien 1")]
    assert detail["phone_history"][0]["sent_at"].startswith("2026-08-12T10:00")


# --- Connexion en coexistence --------------------------------------------------------------------------------

class FakeMeta:
    def __init__(self, numbers=None, fail_sync=False):
        self.numbers = [{"id": "pn-coex", "display_phone_number": "+225 07 11 22 33 44"}] if numbers is None else numbers
        self.fail_sync, self.calls = fail_sync, []

    async def exchange_code_for_token(self, code):
        return "jeton"

    async def subscribe_app_to_waba(self, waba_id, token):
        self.calls.append(("subscribe", waba_id))

    async def list_phone_numbers(self, waba_id, token):
        self.calls.append(("list", waba_id))
        return self.numbers

    async def request_smb_sync(self, phone_number_id, sync_type, token):
        if self.fail_sync:
            raise RuntimeError("Meta indisponible")
        self.calls.append(("sync", phone_number_id, sync_type, token))
        return {"request_id": "r"}


async def _connect(client, shop_email, fake, **body):
    token = (await client.post("/api/v1/auth/login", data={"username": shop_email, "password": "x"})).json()["access_token"]
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        return await client.post("/api/v1/whatsapp/embedded-signup/callback", headers={"Authorization": f"Bearer {token}"},
                                 json={"code": "c", "waba_id": "waba-1", **body}), token
    finally:
        app.dependency_overrides.pop(get_meta_oauth_client, None)


async def _bare_shop(db):
    from app.core.security import hash_password
    from app.models.tenant import Tenant, TenantPlan
    from app.models.user import Role, User

    email = f"c{uuid.uuid4().hex[:8]}@l52.ci"
    tenant = Tenant(name="InfinX", country="CI", currency="XOF", email=email, plan=TenantPlan.PRO)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db.commit()
    return tenant


@pytest.mark.asyncio
async def test_coexistence_connection_finds_the_number_and_asks_for_contacts_then_history(client, db_session):
    shop = await _bare_shop(db_session)
    fake = FakeMeta()
    r, token = await _connect(client, shop.email, fake, coexistence=True)

    assert r.status_code == 200
    body = r.json()
    assert (body["phone_number_id"], body["display_phone_number"], body["coexistence_mode"]) == ("pn-coex", "2250711223344", True)
    assert body["history_sync_status"] == "REQUESTED"
    assert fake.calls == [("subscribe", "waba-1"), ("list", "waba-1"),
                          ("sync", "pn-coex", "smb_app_state_sync", "jeton"), ("sync", "pn-coex", "history", "jeton")]
    account = (await db_session.execute(select(WhatsAppAccount))).scalar_one()
    assert account.contacts_sync_requested_at is not None and account.history_sync_requested_at is not None
    status = (await client.get("/api/v1/whatsapp/account", headers={"Authorization": f"Bearer {token}"})).json()
    assert status["coexistence_mode"] is True and status["history_sync_status"] == "REQUESTED"


@pytest.mark.asyncio
async def test_classic_connection_never_asks_for_phone_data(client, db_session):
    shop = await _bare_shop(db_session)
    fake = FakeMeta()
    r, _ = await _connect(client, shop.email, fake, phone_number_id="pn-new")
    assert r.status_code == 200 and r.json()["coexistence_mode"] is False and r.json()["history_sync_status"] is None
    assert fake.calls == [("subscribe", "waba-1")]


@pytest.mark.asyncio
async def test_missing_number(client, db_session):
    shop = await _bare_shop(db_session)
    r, _ = await _connect(client, shop.email, FakeMeta())  # parcours classique sans numéro
    assert r.status_code == 400 and "aucun numéro" in r.json()["detail"]
    r, _ = await _connect(client, shop.email, FakeMeta(numbers=[]), coexistence=True)
    assert r.status_code == 400
    assert (await db_session.execute(select(WhatsAppAccount))).first() is None


@pytest.mark.asyncio
async def test_a_number_already_used_by_another_shop_is_refused(client, db_session):
    first, second = await _bare_shop(db_session), await _bare_shop(db_session)
    assert (await _connect(client, first.email, FakeMeta(), coexistence=True))[0].status_code == 200
    r, _ = await _connect(client, second.email, FakeMeta(), coexistence=True)
    assert r.status_code == 409 and "déjà connecté à un autre compte" in r.json()["detail"]


@pytest.mark.asyncio
async def test_failed_sync_requests_are_retried_within_24_hours(client, db_session):
    from app.workers.coexistence_sync import retry_syncs

    shop = await _bare_shop(db_session)
    r, _ = await _connect(client, shop.email, FakeMeta(fail_sync=True), phone_number_id="pn-x", coexistence=True)
    assert r.status_code == 200 and r.json()["history_sync_status"] is None  # la connexion réussit quand même

    account = (await db_session.execute(select(WhatsAppAccount))).scalar_one()
    connected = account.connected_at.replace(tzinfo=timezone.utc) if account.connected_at.tzinfo is None else account.connected_at
    late = FakeMeta()
    assert await retry_syncs(db_session, connected + timedelta(hours=25), late) == {}
    assert late.calls == []

    fake = FakeMeta()
    report = await retry_syncs(db_session, connected + timedelta(hours=3), fake)
    assert report == {"pn-x": ["smb_app_state_sync", "history"]}
    assert await retry_syncs(db_session, connected + timedelta(hours=4), fake) == {}  # plus rien à demander
    await db_session.refresh(account)
    assert account.history_sync_status == "REQUESTED"


@pytest.mark.asyncio
async def test_request_syncs_only_for_coexistence_within_the_window(db_session):
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    fake = FakeMeta()
    classic = WhatsAppAccount(phone_number_id="p1", coexistence_mode=False, connected_at=now, system_user_token="t")
    assert await coexistence.request_syncs(db_session, classic, fake, now) == []
    old = WhatsAppAccount(phone_number_id="p2", coexistence_mode=True, connected_at=now - timedelta(hours=24, seconds=1), system_user_token="t")
    assert await coexistence.request_syncs(db_session, old, fake, now) == []
    edge = WhatsAppAccount(phone_number_id="p3", coexistence_mode=True, connected_at=(now - timedelta(hours=24)).replace(tzinfo=None),
                           system_user_token="t", history_sync_status="DONE")
    assert await coexistence.request_syncs(db_session, edge, fake, now) == ["smb_app_state_sync", "history"]
    assert edge.history_sync_status == "DONE"  # une reprise déjà finie n'est jamais remise à « demandée »
    assert fake.calls == [("sync", "p3", "smb_app_state_sync", "t"), ("sync", "p3", "history", "t")]
    half = WhatsAppAccount(phone_number_id="p4", coexistence_mode=True, connected_at=now, system_user_token="t",
                           contacts_sync_requested_at=now)
    assert await coexistence.request_syncs(db_session, half, fake, now) == ["history"]  # seulement ce qui manque


# --- Pages et tâche planifiée ---------------------------------------------------------------------------------

def test_dashboard_connection_choices():
    assert 'onclick="connectWhatsApp(true)">J\'ai déjà WhatsApp Business sur mon téléphone</button>' in DASHBOARD
    assert 'onclick="connectWhatsApp(false)">Utiliser un nouveau numéro</button>' in DASHBOARD
    assert 'featureType: waCoexistence ? "whatsapp_business_app_onboarding" : ""' in DASHBOARD
    assert 'data.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"' in DASHBOARD
    assert "coexistence: waCoexistence," in DASHBOARD and "(!waSessionInfo.phone_number_id && !waCoexistence)" in DASHBOARD
    info = DASHBOARD[DASHBOARD.index("function coexistenceInfo(account) {"):]
    info = info[:info.index("\n}\n")]
    assert "${esc(history)}" in info and "tous les 14 jours" in info
    block = DASHBOARD[DASHBOARD.index("function phoneHistoryBlock(rows) {"):]
    block = block[:block.index("\n}\n")]
    assert "waFormat(r.content)" in block and "Historique du téléphone, avant Bob" in block
    assert "phoneHistoryBlock(c.phone_history) + c.messages" in DASHBOARD


def test_retry_task_is_registered():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.coexistence_sync" in TASK_MODULES
    entry = celery_app.conf.beat_schedule["coexistence-sync-retry"]
    assert entry["task"] == "app.workers.coexistence_sync.retry_syncs_task" and entry["schedule"].minute == set(range(0, 60, 15))
