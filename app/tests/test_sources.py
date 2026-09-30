"""Lot 28 — sources des clients : pubs Meta « clic vers WhatsApp », canal des liens, tableau par canal."""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.integrations.whatsapp.client import parse_whatsapp_message
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, Message
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.acquisition import (
    LINK_CHANNELS,
    ad_context_for_ai,
    attribution_from_referral,
    channel_of,
    parse_referral,
)
from app.tests.fakes import FakeLLMClient, text_response

API = "/api/v1/contact-points"

IG_AD = {"source_url": "https://www.instagram.com/p/abc", "source_id": "120210001", "source_type": "ad",
         "headline": "Peugeot 5008 à saisir", "body": "SUV 7 places, 2021", "media_type": "image",
         "image_url": "https://scontent.example/img.jpg", "ctwa_clid": "SECRET-CLICK-ID"}
FB_AD = {"source_url": "https://fb.me/xyz", "source_id": "987", "source_type": "ad", "headline": "Promo rentrée"}


# --- Lecture de la référence Meta --------------------------------------------------------

def test_instagram_and_facebook_ads_are_told_apart():
    assert attribution_from_referral(parse_referral(IG_AD)) == ("AD_INSTAGRAM", "Peugeot 5008 à saisir")
    assert attribution_from_referral(parse_referral(FB_AD)) == ("AD_FACEBOOK", "Promo rentrée")
    unknown = parse_referral({"source_url": "https://example.com/x", "source_id": "5", "source_type": "ad"})
    assert attribution_from_referral(unknown) == ("AD_META", "Publicité 5")


def test_facebook_post_is_not_an_ad():
    ref = parse_referral({"source_url": "https://www.facebook.com/page/posts/1", "source_type": "post", "headline": "Arrivage"})
    assert attribution_from_referral(ref) == ("FACEBOOK", "Arrivage")
    assert channel_of("FACEBOOK") == "FACEBOOK"


def test_only_useful_fields_are_kept_and_bounded():
    ref = parse_referral({**IG_AD, "headline": "Ligne 1\nLigne 2 " + "x" * 500, "body": "b" * 900})
    assert set(ref) == {"is_ad", "network", "ad_id", "headline", "body"}  # ni URL de média, ni identifiant de clic
    assert "\n" not in ref["headline"] and len(ref["headline"]) <= 120 and len(ref["body"]) <= 300


@pytest.mark.parametrize("raw", [None, "texte", 42, [], {}, {"source_type": "ad"}, {"headline": 12}])
def test_garbage_referral_is_ignored(raw):
    assert parse_referral(raw) is None


def test_parser_passes_the_referral_through():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "p"}, "messages": [
        {"from": "1", "id": "m", "type": "text", "text": {"body": "Bonjour"}, "referral": IG_AD}]}}]}]}
    assert parse_whatsapp_message(payload)["referral"] == IG_AD


def test_ai_context_presents_the_ad_as_data_and_asks_to_check():
    context = ad_context_for_ai(parse_referral(IG_AD))
    assert "Peugeot 5008 à saisir" in context and "SUV 7 places" in context
    assert "vérifie toujours les prix" in context
    assert ad_context_for_ai(parse_referral({"source_id": "5", "source_type": "ad"})) is None


def test_channel_of_every_source():
    assert channel_of("LINK", "GOOGLE") == "GOOGLE"
    assert channel_of("LINK", None) == "OTHER"
    assert channel_of("LINK", "HACKED") == "OTHER"
    assert channel_of("QR") == "QR"
    assert channel_of("AD_INSTAGRAM") == "AD_INSTAGRAM"
    assert channel_of("NAOMY") == "NAOMY"
    assert channel_of(None) == channel_of("ORGANIC") == channel_of("inconnu") == "DIRECT"


def test_naomy_is_reserved_not_selectable_for_a_link():
    assert "NAOMY" not in LINK_CHANNELS and channel_of("NAOMY") == "NAOMY"


# --- Webhook ---------------------------------------------------------------------------

async def _shop(db_session, email, phone_number_id, business_type="ONLINE_STORE"):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id,
                                   system_user_token="t", display_phone_number="+221 70 111 22 33"))
    await db_session.commit()
    return tenant


def _payload(phone_number_id, number, text, referral=None):
    message = {"from": number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}
    if referral is not None:
        message["referral"] = referral
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}, "messages": [message]}}]}]}


async def _customer(db_session, number, tenant_id):
    return (await db_session.execute(select(Customer).where(Customer.whatsapp_number == number, Customer.tenant_id == tenant_id)
                                     .execution_options(populate_existing=True))).scalar_one()


@pytest.fixture
def silent(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: True)


@pytest.fixture
def llm():
    def use(responses):
        fake = FakeLLMClient(responses)
        app.dependency_overrides[get_llm_client] = lambda: fake
        return fake

    yield use
    app.dependency_overrides.pop(get_llm_client, None)


@pytest.mark.asyncio
async def test_new_customer_from_instagram_ad(client, db_session, unique_email, silent, llm):
    tenant = await _shop(db_session, unique_email, "pn-src-1")
    fake = llm([text_response("Bonjour ! La 5008 est disponible.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-src-1", "221700002001", "Bonjour, est-ce disponible ?", IG_AD))

    customer = await _customer(db_session, "221700002001", tenant.id)
    assert (customer.acquisition_source, customer.acquisition_detail, customer.acquisition_ad_id) == (
        "AD_INSTAGRAM", "Peugeot 5008 à saisir", "120210001")
    sent = repr(fake.received_messages[0])
    assert "Peugeot 5008 à saisir" in sent and "Bonjour, est-ce disponible ?" in sent
    # Le contexte de l'annonce n'est jamais enregistré comme un message du client.
    stored = (await db_session.execute(select(Message.content).where(Message.tenant_id == tenant.id))).scalars().all()
    assert "Bonjour, est-ce disponible ?" in stored and not any("publicité" in m for m in stored)
    assert "SECRET-CLICK-ID" not in sent


@pytest.mark.asyncio
async def test_known_customer_keeps_his_source_but_bob_gets_the_ad(client, db_session, unique_email, silent, llm):
    tenant = await _shop(db_session, unique_email, "pn-src-2")
    llm([text_response("Bonjour !")])
    await client.post("/webhooks/whatsapp", json=_payload("pn-src-2", "221700002002", "Bonjour"))
    fake = llm([text_response("Oui, toujours disponible.")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-src-2", "221700002002", "Et celle-ci ?", FB_AD))

    customer = await _customer(db_session, "221700002002", tenant.id)
    assert customer.acquisition_source is None and customer.acquisition_ad_id is None
    assert "Promo rentrée" in repr(fake.received_messages[0])


@pytest.mark.asyncio
async def test_meta_ad_wins_over_a_link_tag_for_the_source(client, db_session, unique_email, silent, llm):
    tenant = await _shop(db_session, unique_email, "pn-src-3")
    cp = ContactPoint(tenant_id=tenant.id, code="moussa3", name="Moussa", greeting="Bonjour",
                      owner_name="Moussa", owner_email="moussa@example.com", channel="COMMERCIAL")
    db_session.add(cp)
    await db_session.commit()
    llm([text_response("Bonjour !")])

    await client.post("/webhooks/whatsapp", json=_payload("pn-src-3", "221700002003", "Bonjour [W:moussa3]", IG_AD))

    customer = await _customer(db_session, "221700002003", tenant.id)
    assert customer.acquisition_source == "AD_INSTAGRAM" and customer.acquisition_contact_point_id is None
    assert customer.referred_contact_point_id == cp.id  # le commercial reste prévenu (lot 27)


@pytest.mark.asyncio
async def test_message_without_referral_is_unchanged(client, db_session, unique_email, silent, llm):
    tenant = await _shop(db_session, unique_email, "pn-src-4")
    fake = llm([text_response("Bonjour !")])
    await client.post("/webhooks/whatsapp", json=_payload("pn-src-4", "221700002004", "Bonjour"))
    assert fake.received_messages[0][-1] == {"role": "user", "content": "Bonjour"}
    assert (await _customer(db_session, "221700002004", tenant.id)).acquisition_source is None


# --- Canal des liens -------------------------------------------------------------------

async def _auth(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_link_channel_is_chosen_changed_and_cleared(client, db_session, unique_email):
    await _shop(db_session, unique_email, "pn-src-10")
    headers = await _auth(client, unique_email)
    cp = (await client.post(API, json={"name": "Pub Google 5008", "channel": "google"}, headers=headers)).json()
    assert (cp["channel"], cp["channel_label"]) == ("GOOGLE", "Google")

    changed = (await client.patch(f"{API}/{cp['id']}", json={"channel": "YOUTUBE"}, headers=headers)).json()
    assert changed["channel"] == "YOUTUBE"
    kept = (await client.patch(f"{API}/{cp['id']}", json={"greeting": "Salut"}, headers=headers)).json()
    assert kept["channel"] == "YOUTUBE"
    cleared = (await client.patch(f"{API}/{cp['id']}", json={"channel": ""}, headers=headers)).json()
    assert cleared["channel"] is None and cleared["channel_label"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["NAOMY", "AD_INSTAGRAM", "<script>", "X" * 40])
async def test_unknown_link_channel_rejected(client, db_session, unique_email, channel):
    await _shop(db_session, unique_email, "pn-src-11")
    r = await client.post(API, json={"name": "X", "channel": channel}, headers=await _auth(client, unique_email))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_commercial_link_defaults_to_commercial_channel(client, db_session, unique_email):
    await _shop(db_session, unique_email, "pn-src-12")
    headers = await _auth(client, unique_email)
    default = (await client.post(API, json={"name": "Moussa", "owner_email": "moussa@example.com"}, headers=headers)).json()
    chosen = (await client.post(API, json={"name": "Moussa TikTok", "owner_email": "moussa@example.com", "channel": "TIKTOK"},
                                headers=headers)).json()
    plain = (await client.post(API, json={"name": "Divers"}, headers=headers)).json()
    assert (default["channel"], chosen["channel"], plain["channel"]) == ("COMMERCIAL", "TIKTOK", None)


# --- Fiche client et tableau par canal ---------------------------------------------------

async def _data(db_session, tenant, other_tenant=None):
    google = ContactPoint(tenant_id=tenant.id, code=f"g{uuid.uuid4().hex[:6]}", name="Pub Google 5008", greeting="B", channel="GOOGLE")
    db_session.add(google)
    await db_session.flush()
    old = datetime.now(timezone.utc) - timedelta(days=200)
    people = [
        Customer(tenant_id=tenant.id, whatsapp_number="1", acquisition_source="LINK", acquisition_contact_point_id=google.id),
        Customer(tenant_id=tenant.id, whatsapp_number="2", acquisition_source="LINK", acquisition_contact_point_id=google.id),
        Customer(tenant_id=tenant.id, whatsapp_number="3", acquisition_source="AD_INSTAGRAM", acquisition_detail="Peugeot 5008 à saisir"),
        Customer(tenant_id=tenant.id, whatsapp_number="4"),
        Customer(tenant_id=tenant.id, whatsapp_number="5", acquisition_source="AD_INSTAGRAM", acquisition_detail="Vieille pub", created_at=old),
    ]
    db_session.add_all(people)
    await db_session.flush()
    conv = Conversation(tenant_id=tenant.id, customer_id=people[2].id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=people[2].id, kind="ESSAI", availability="samedi"))
    db_session.add(Order(tenant_id=tenant.id, customer_id=people[0].id, status=OrderStatus.PAID, total_amount=15000, currency="XOF"))
    db_session.add(Order(tenant_id=tenant.id, customer_id=people[1].id, status=OrderStatus.PENDING, total_amount=9000, currency="XOF"))
    if other_tenant is not None:
        db_session.add(Customer(tenant_id=other_tenant.id, whatsapp_number="9", acquisition_source="AD_FACEBOOK", acquisition_detail="Autre boutique"))
    await db_session.commit()
    return google


@pytest.mark.asyncio
async def test_sources_table_by_channel_and_by_ad(client, db_session, unique_email):
    tenant = await _shop(db_session, unique_email, "pn-src-20")
    other = await _shop(db_session, f"autre-{unique_email}", "pn-src-20b")
    await _data(db_session, tenant, other)
    headers = await _auth(client, unique_email)

    data = (await client.get("/api/v1/analytics/sources?period=30", headers=headers)).json()

    by = {r["channel"]: r for r in data["channels"]}
    assert data["total_customers"] == 4 and data["currency"] == "XOF"
    assert (by["GOOGLE"]["customers"], by["GOOGLE"]["paid_orders"], by["GOOGLE"]["revenue"]) == (2, 1, 15000)
    assert (by["AD_INSTAGRAM"]["customers"], by["AD_INSTAGRAM"]["appointments"]) == (1, 1)
    assert by["DIRECT"]["customers"] == 1 and by["DIRECT"]["label"] == "Direct"
    assert "AD_FACEBOOK" not in by  # jamais les clients d'une autre boutique
    labels = {r["label"] for r in data["details"]}
    assert {"Lien « Pub Google 5008 »", "Peugeot 5008 à saisir"} <= labels and "Vieille pub" not in labels

    everything = (await client.get("/api/v1/analytics/sources?period=all", headers=headers)).json()
    assert everything["total_customers"] == 5
    assert (await client.get("/api/v1/analytics/sources?period=7", headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_changing_a_link_channel_updates_its_old_customers(client, db_session, unique_email):
    tenant = await _shop(db_session, unique_email, "pn-src-21")
    google = await _data(db_session, tenant)
    headers = await _auth(client, unique_email)
    await client.patch(f"{API}/{google.id}", json={"channel": "YOUTUBE"}, headers=headers)

    customers = (await client.get("/api/v1/customers", headers=headers)).json()
    labels = sorted(c["acquisition_channel_label"] for c in customers)
    assert labels.count("YouTube") == 2 and "Pub Instagram" in labels and "Direct" in labels
    one = next(c for c in customers if c["acquisition_channel"] == "AD_INSTAGRAM")
    detail = (await client.get(f"/api/v1/customers/{one['id']}", headers=headers)).json()
    assert detail["acquisition_channel_label"] == "Pub Instagram"


def test_dashboard_channels_match_the_server_and_are_escaped():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    js = re.search(r"const LINK_CHANNELS = \[(.*?)\];", html, re.S).group(1)
    assert re.findall(r'\["([A-Z]+)", "([^"]+)"\]', js) == list(LINK_CHANNELS.items())
    assert "${esc(r.label)}" in html and "${esc(c.acquisition_channel_label || acquisitionLabel(c.acquisition_source))}" in html
    assert "loadSources(" in html and 'id="cp-channel"' in html


@pytest.mark.asyncio
async def test_link_of_another_shop_never_leaks_into_the_table(client, db_session, unique_email):
    tenant = await _shop(db_session, unique_email, "pn-src-22")
    other = await _shop(db_session, f"autre-{unique_email}", "pn-src-22b")
    foreign = ContactPoint(tenant_id=other.id, code="foreign22", name="Lien secret", greeting="B", channel="TIKTOK")
    db_session.add(foreign)
    await db_session.flush()
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="7", acquisition_source="LINK", acquisition_contact_point_id=foreign.id))
    await db_session.commit()

    text = (await client.get("/api/v1/analytics/sources?period=all", headers=await _auth(client, unique_email))).text

    assert "Lien secret" not in text and "TIKTOK" not in text
