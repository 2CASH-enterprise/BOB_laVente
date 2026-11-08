"""
Lot 116 — bouton « 📞 Appeler » sous le message de transfert vers la boutique (tous secteurs), seulement pendant les
heures d'ouverture ; numéro « pour les appels » réglable (sinon le numéro WhatsApp) ; page /appel/{jeton} qui compose
le numéro et note le clic dans la conversation.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.appointment_settings import TenantAppointmentSettings
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.whatsapp_account import WhatsAppAccount
from app.services import call_button
from app.services.business_type import INSURANCE_BROKER, ONLINE_STORE, RESTAURANT
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _cabinet, _conversation, _headers
from app.tests.test_lot94_parcours_boutons import _WA, _tap, guided_on  # noqa: F401 — fixture
from app.tests.test_lot107_parcours_restaurant import HTML, _closed_hours


class _Call(_WA):
    async def send_cta_url(self, to, body, display_text, url):
        return await self._log("cta", body=body, label=display_text, url=url)


async def _shop(db, business_type=RESTAURANT, number="+225 07 07 00 00 99", **fields):
    tenant = await _cabinet(db, business_type=business_type)
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one()
    account.display_phone_number = number
    for key, value in fields.items():
        setattr(tenant, key, value)
    await db.commit()
    return tenant


def test_numbers_are_cleaned():
    assert call_button.clean_phone("+225 07 07 00 00 99") == "+2250707000099"
    assert call_button.clean_phone("07 07 00 00 99") == "0707000099"  # numéro local : composé tel quel
    assert call_button.clean_phone("2250707000099") == "+2250707000099"
    assert call_button.clean_phone("12 34") is None and call_button.clean_phone(None) is None
    assert call_button.clean_phone("+1234567890123456") is None


def test_the_link_cannot_target_another_conversation():
    cid = uuid.uuid4()
    value = call_button.token(cid)
    assert call_button.conversation_of(value) == cid and call_button.link(cid).endswith(f"/appel/{value}")
    other = uuid.uuid4().hex + value[32:]
    assert call_button.conversation_of(other) is None and call_button.conversation_of("abc") is None


@pytest.mark.asyncio
async def test_number_and_opening_hours(db_session):
    tenant = await _shop(db_session)
    assert await call_button.number_for(db_session, tenant) == "+2250707000099"
    tenant.call_phone = "27 22 00 00 00"
    assert await call_button.number_for(db_session, tenant) == "2722000000"
    assert await call_button.open_now(db_session, tenant)  # cuisine sans horaires : ouverte
    tenant.kitchen_hours = _closed_hours()
    assert not await call_button.open_now(db_session, tenant)
    shop = await _shop(db_session, business_type=ONLINE_STORE)
    assert await call_button.open_now(db_session, shop)  # pas d'horaires : toujours
    broker = await _shop(db_session, business_type=INSURANCE_BROKER)
    db_session.add(TenantAppointmentSettings(tenant_id=broker.id, online_booking=True, slot_minutes=30,
                                             opening_hours={str(d): [["08:00", "17:00"]] for d in range(7)}))
    await db_session.commit()
    morning = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)  # 10 h à Abidjan
    night = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
    assert await call_button.open_now(db_session, broker, morning) and not await call_button.open_now(db_session, broker, night)
    nobody = await _shop(db_session, number=None)
    assert await call_button.number_for(db_session, nobody) is None


@pytest.mark.asyncio
async def test_talking_to_the_restaurant_comes_with_the_call_button(client, db_session, guided_on, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Call)
    tenant = await _shop(db_session)
    guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_tap(tenant.pnid, "2250102030488", "g:human", "Parler au restaurant"))
    assert r.status_code == 200
    sent = _WA.sent[-1]
    assert sent["kind"] == "cta" and sent["label"] == "📞 Appeler" and "/appel/" in sent["url"]
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id))).scalar_one()
    assert conversation.status == ConversationStatus.WAITING_HUMAN and sent["url"] == call_button.link(conversation.id)
    # Le client réécrit pendant l'attente : pas de nouveau bouton (le transfert était déjà fait).
    _WA.sent.clear()
    await client.post("/webhooks/whatsapp", json=_tap(tenant.pnid, "2250102030488", "g:human", "Parler au restaurant"))
    assert all(m["kind"] != "cta" for m in _WA.sent)


@pytest.mark.asyncio
async def test_no_button_when_closed_or_when_refused(client, db_session, guided_on, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Call)
    tenant = await _shop(db_session, kitchen_hours=_closed_hours())
    guided_on({"intents": [], "objections": []}, [])
    await client.post("/webhooks/whatsapp", json=_tap(tenant.pnid, "2250102030489", "g:human", "Parler au restaurant"))
    assert _WA.sent[-1]["kind"] == "text"  # cuisine fermée : le restaurant répond ici, sans bouton

    class Refuses(_Call):
        async def send_cta_url(self, *a, **kw):
            raise RuntimeError("refusé")

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", Refuses)
    open_one = await _shop(db_session)
    _WA.sent.clear()
    await client.post("/webhooks/whatsapp", json=_tap(open_one.pnid, "2250102030490", "g:human", "Parler au restaurant"))
    assert [m["kind"] for m in _WA.sent] == ["text"]  # bouton refusé : le texte seul part quand même


@pytest.mark.asyncio
async def test_the_call_page_dials_and_notes_the_click(client, db_session):
    tenant = await _shop(db_session, call_phone="+225 27 22 00 00 00")
    customer, conversation = await _conversation(db_session, tenant)
    url = f"/appel/{call_button.token(conversation.id)}"
    r = await client.get(url, headers={"user-agent": "Mozilla/5.0 (Linux; Android 14)"})
    assert r.status_code == 200 and 'href="tel:+2252722000000"' in r.text
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.text
    await client.get(url, headers={"user-agent": "Mozilla/5.0"})  # second clic dans les 10 minutes : une seule note
    await client.get(url, headers={"user-agent": "WhatsApp/2.23.20.0"})  # aperçu du lien : jamais noté
    notes = (await db_session.execute(select(Message).where(Message.conversation_id == conversation.id,
                                                            Message.message_type == "call_click"))).scalars().all()
    assert [n.content for n in notes] == ["📞 Le client a ouvert le bouton d'appel"]
    assert (await client.get("/appel/" + "0" * 48)).status_code == 404
    _, quiet = await _conversation(db_session, tenant)
    preview = await client.get(f"/appel/{call_button.token(quiet.id)}", headers={"user-agent": "facebookexternalhit/1.1"})
    assert preview.status_code == 200
    assert not (await db_session.execute(select(Message).where(Message.conversation_id == quiet.id,
                                                               Message.message_type == "call_click"))).scalars().all()


@pytest.mark.asyncio
async def test_the_call_number_setting(client, db_session):
    tenant = await _shop(db_session)
    headers = await _headers(client, tenant)
    r = await client.put("/api/v1/tenants/me/profile", headers=headers, json={"call_phone": " +225 27 22 00 00 00 "})
    assert r.status_code == 200 and r.json()["call_phone"] == "+225 27 22 00 00 00"
    assert (await client.put("/api/v1/tenants/me/profile", headers=headers, json={"call_phone": "12"})).status_code == 422
    assert (await client.put("/api/v1/tenants/me/profile", headers=headers, json={"call_phone": ""})).json()["call_phone"] is None
    assert 'id="knowledge-call-phone"' in HTML and "Numéro pour les appels" in HTML and "call_phone: document" in HTML


@pytest.mark.asyncio
async def test_the_button_message_sent_to_meta(monkeypatch):
    from app.integrations.whatsapp.client import WhatsAppClient

    posted = []

    async def fake_post(self, path, payload):
        posted.append(payload)
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    client = WhatsAppClient(phone_number_id="pn", system_user_token="t")
    await call_button.send(client, "225", "Le restaurant vous répond ici.", "https://x/appel/abc")
    assert posted[-1]["interactive"] == {"type": "cta_url", "body": {"text": "Le restaurant vous répond ici."},
                                         "action": {"name": "cta_url", "parameters": {"display_text": "📞 Appeler", "url": "https://x/appel/abc"}}}
    await call_button.send(client, "225", "x" * 1200, "https://x/appel/abc")  # trop long : texte, puis court message à bouton
    assert posted[-2]["type"] == "text" and posted[-1]["interactive"]["body"]["text"] == "Vous pouvez aussi appeler directement."


@pytest.mark.asyncio
async def test_every_sector_gets_the_button(client, db_session, guided_on, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Call)
    guided_on({"intents": [], "objections": []}, [])
    for i, sector in enumerate((ONLINE_STORE, INSURANCE_BROKER)):
        tenant = await _shop(db_session, business_type=sector)
        _WA.sent.clear()
        await client.post("/webhooks/whatsapp", json=_tap(tenant.pnid, f"22501020305{i}0", "g:human", "Un conseiller"))
        assert _WA.sent and _WA.sent[-1]["kind"] == "cta", sector
