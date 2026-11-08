"""
Lot 114 — « Réserver une table » en formulaire WhatsApp (Flow) : nombre de personnes d'abord (liste habituelle), puis
un formulaire d'un écran (jour parmi les 7 prochains avec de la place, heures libres du jour, précision). Sans serveur de
formulaire : les données partent avec le message. Éteint par défaut (plateforme), essai possible sur un restaurant ;
tout refus de Meta → la réservation à boutons du lot 107.
"""
import json
import uuid

import pytest
from sqlalchemy import select

from app.integrations.whatsapp.client import MetaGraphError, parse_whatsapp_message
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.cart import Cart
from app.models.whatsapp_flow import WhatsAppFlow
from app.services import guided_flow, wa_flows
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _conversation
from app.tests.test_lot68_modeles_whatsapp import _admin
from app.tests.test_lot94_parcours_boutons import guided_on  # noqa: F401 — fixture
from app.tests.test_lot106_restaurant import SERVICES, _resto
from app.tests.test_lot107_parcours_restaurant import _guided, _tap  # noqa: F401 (fixture)


class FakeFlows:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result or {"id": "FLOW1", "success": True}, error

    async def create(self, name, flow, category="APPOINTMENT_BOOKING"):
        self.calls.append((name, flow, category))
        if self.error:
            raise self.error
        return self.result


async def _booking_resto(db, mode="ON"):
    tenant = await _resto(db, wa_flows=mode)
    db.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=SERVICES, slot_minutes=30, capacity=40))
    await db.commit()
    return tenant


async def _published(db, tenant):
    db.add(WhatsAppFlow(tenant_id=tenant.id, key="booking", version=wa_flows.VERSION, status="PUBLISHED", meta_id="FLOW1"))
    await db.commit()


def test_the_form_has_one_screen_a_day_list_and_the_times_of_the_chosen_day():
    flow = wa_flows.flow_json()
    assert flow["version"] == "7.0" and len(flow["screens"]) == 1
    screen = flow["screens"][0]
    assert screen["id"] == "RESERVER" and screen["terminal"] is True and screen["title"] == "Réserver une table"
    form = screen["layout"]["children"][0]["children"]
    types = [c["type"] for c in form]
    assert types == ["TextBody", "Dropdown", "Switch", "TextArea", "Footer"]
    switch = form[2]
    assert switch["value"] == "${form.day}" and sorted(switch["cases"]) == [f"d{i}" for i in range(7)]
    assert switch["cases"]["d3"] == [{"type": "Dropdown", "name": "time_d3", "label": "Heure", "required": True,
                                      "data-source": "${data.t3}"}]
    assert all(c["type"] != "Footer" for case in switch["cases"].values() for c in case)  # pied hors du Switch
    payload = form[4]["on-click-action"]["payload"]
    assert form[4]["on-click-action"]["name"] == "complete" and payload["day"] == "${form.day}" and payload["t6"] == "${form.time_d6}"
    assert form[3]["required"] is False and form[3]["max-length"] == 200
    assert set(screen["data"]) == {"intro", "days"} | {f"t{i}" for i in range(7)}
    assert screen["data"]["t0"]["type"] == "array" and screen["data"]["t0"]["__example__"]
    json.dumps(flow)


@pytest.mark.asyncio
async def test_switches_off_by_default_trial_and_platform(db_session):
    tenant = await _booking_resto(db_session, mode=None)
    assert not await wa_flows.active_for(db_session, tenant) and wa_flows.tenant_mode(tenant) == "AUTO"
    tenant.wa_flows = "ON"
    assert await wa_flows.active_for(db_session, tenant)
    tenant.wa_flows = None
    await wa_flows.set_enabled(db_session, True)
    assert await wa_flows.active_for(db_session, tenant)
    tenant.wa_flows = "OFF"
    assert not await wa_flows.active_for(db_session, tenant)
    tenant.wa_flows, tenant.is_demo = None, True
    assert not await wa_flows.active_for(db_session, tenant)  # jamais pour une démo
    shop = await _resto(db_session)
    shop.business_type = "ONLINE_STORE"
    assert not await wa_flows.active_for(db_session, shop)
    await wa_flows.set_enabled(db_session, False)


@pytest.mark.asyncio
async def test_the_form_is_created_once_and_a_refusal_is_retried_hourly(db_session):
    tenant = await _booking_resto(db_session)
    fake = FakeFlows()
    assert await wa_flows.ensure(db_session, tenant, client=fake) == "FLOW1"
    assert await wa_flows.ensure(db_session, tenant, client=fake) == "FLOW1" and len(fake.calls) == 1
    assert fake.calls[0][0] == "bob_reservation_v1"
    row = (await db_session.execute(select(WhatsAppFlow).where(WhatsAppFlow.tenant_id == tenant.id))).scalar_one()
    assert row.status == "PUBLISHED" and row.meta_id == "FLOW1"

    other = await _booking_resto(db_session)
    refused = FakeFlows(result={"id": "F2", "validation_errors": [{"message": "Switch non pris en charge"}]})
    assert await wa_flows.ensure(db_session, other, client=refused) is None
    row = (await db_session.execute(select(WhatsAppFlow).where(WhatsAppFlow.tenant_id == other.id))).scalar_one()
    assert row.status == "ERROR" and row.reason == "Switch non pris en charge"
    assert await wa_flows.ensure(db_session, other, client=refused) is None and len(refused.calls) == 1  # pas avant 1 h
    down = FakeFlows(error=MetaGraphError("Meta a répondu 400 (permission)"))
    assert await wa_flows.ensure(db_session, other, client=down, force=True) is None and len(down.calls) == 1
    assert "permission" in row.reason


@pytest.mark.asyncio
async def test_party_then_the_form_with_free_days_and_times(db_session):
    tenant = await _booking_resto(db_session)
    await _published(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    form = (await _tap(db_session, tenant, customer, conversation, "fb:4")).actions[0]
    assert form["type"] == "flow" and form["flow_id"] == "FLOW1" and form["cta"] == "Choisir l'heure"
    assert form["body"].startswith("Table pour 4 personnes.") and form["screen"] == "RESERVER"
    data = form["data"]
    assert 1 <= len(data["days"]) <= 7 and data["days"][0]["id"] == "d0" and "Table pour 4 personnes" in data["intro"]
    for i in range(7):
        assert data[f"t{i}"], i  # jamais une liste vide (Meta refuserait)
    first = data["t0"][0]
    assert len(first["id"]) == 12 and "h" in first["title"] and ("(midi)" in first["title"] or "(soir)" in first["title"])
    assert wa_flows.party_of(form["flow_token"]) == 4
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert cart.meta["flow"] == form["flow_token"]
    fallback = guided_flow.degraded(form)
    assert fallback["type"] == "list" and fallback["button"] == "Choisir le jour"  # repli : la réservation à boutons
    assert guided_flow._trace(form).endswith("[Formulaire : Choisir l'heure]")


@pytest.mark.asyncio
async def test_off_or_unpublished_keeps_the_buttons(db_session):
    tenant = await _booking_resto(db_session, mode="OFF")
    customer, conversation = await _conversation(db_session, tenant)
    assert (await _tap(db_session, tenant, customer, conversation, "fb:2")).actions[0]["type"] == "list"
    trial = await _booking_resto(db_session)
    trial_row = WhatsAppFlow(tenant_id=trial.id, key="booking", version=wa_flows.VERSION, status="ERROR", reason="refus")
    db_session.add(trial_row)
    await db_session.commit()
    customer2, conversation2 = await _conversation(db_session, trial)
    assert (await _tap(db_session, trial, customer2, conversation2, "fb:2")).actions[0]["type"] == "list"


@pytest.mark.asyncio
async def test_the_answer_books_the_table_with_the_note(db_session):
    tenant = await _booking_resto(db_session)
    await _published(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    form = (await _tap(db_session, tenant, customer, conversation, "fb:3")).actions[0]
    stamp = form["data"]["t0"][0]["id"]
    answer = {"flow_token": form["flow_token"], "day": "d0", "t0": stamp, "t1": "", "note": "  Anniversaire,   chaise bébé "}
    turn = await guided_flow.handle(db_session, tenant, "📅", None, None, False, customer, conversation, entry={"flow": answer})
    await db_session.commit()
    assert turn.actions[0]["message_type"] == "appointment_confirmed" and "3 personnes" in turn.actions[0]["body"]
    appointment = (await db_session.execute(select(AppointmentRequest).where(AppointmentRequest.tenant_id == tenant.id))).scalar_one()
    assert appointment.party_size == 3 and appointment.kind == "TABLE" and "Précision du client : Anniversaire, chaise bébé" in appointment.notes
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert "flow" not in (cart.meta or {})
    # Réponse incomplète ou d'un autre formulaire : la liste des jours, ou rien.
    broken = await guided_flow.handle(db_session, tenant, "x", None, None, False, customer, conversation,
                                      entry={"flow": {"flow_token": form["flow_token"], "day": "d0", "t0": "demain"}})
    assert broken.actions[0]["type"] in ("list", "flow")
    assert wa_flows.party_of("autre:2:x") is None and wa_flows.party_of("bobt:0:x") is None
    assert wa_flows.chosen_slot({"day": "d9"}) is None and wa_flows.note_of({"note": ""}) is None


def test_the_reply_and_the_payload_formats():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn"}, "messages": [{
        "from": "2250700000001", "id": "wamid.1", "type": "interactive", "interactive": {"type": "nfm_reply", "nfm_reply": {
            "name": "flow", "body": "Envoyé", "response_json": json.dumps({"flow_token": "bobt:2:abc", "day": "d1", "t1": "202611141930"})}}}]}}]}]}
    parsed = parse_whatsapp_message(payload)
    assert parsed["flow_reply"] == {"flow_token": "bobt:2:abc", "day": "d1", "t1": "202611141930"} and parsed["reply_id"] is None
    assert wa_flows.chosen_slot(parsed["flow_reply"]) == "202611141930"
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["interactive"]["nfm_reply"]["response_json"] = "{pas du json"
    assert parse_whatsapp_message(payload)["flow_reply"] is None


@pytest.mark.asyncio
async def test_the_flow_message_sent_to_meta(monkeypatch):
    from app.integrations.whatsapp.client import WhatsAppClient

    posted = []

    async def fake_post(self, path, payload):
        posted.append(payload)
        return {"messages": [{"id": "wamid.x"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    action = wa_flows.flow_action("FLOW1", "bobt:2:abc", "Table pour 2 personnes.", {"intro": "x", "days": []})
    await guided_flow._send(WhatsAppClient(phone_number_id="pn", system_user_token="t"), "225", action)
    interactive = posted[-1]["interactive"]
    assert interactive["type"] == "flow" and interactive["action"]["name"] == "flow"
    params = interactive["action"]["parameters"]
    assert params == {"flow_message_version": "3", "flow_token": "bobt:2:abc", "flow_id": "FLOW1", "flow_cta": "Choisir l'heure",
                      "flow_action": "navigate", "flow_action_payload": {"screen": "RESERVER", "data": {"intro": "x", "days": []}}}


@pytest.mark.asyncio
async def test_the_webhook_takes_the_form_answer(client, db_session, guided_on):  # noqa: F811
    from app.models.conversation import Message
    from app.tests.test_lot94_parcours_boutons import _WA

    tenant = await _booking_resto(db_session)
    await _published(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant, number="2250102030466")
    form = (await _tap(db_session, tenant, customer, conversation, "fb:2")).actions[0]
    guided_on({"intents": [], "objections": []}, [])
    stamp = form["data"]["t0"][0]["id"]
    response = json.dumps({"flow_token": form["flow_token"], "day": "d0", "t0": stamp, "note": "Terrasse"})
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": tenant.pnid}, "messages": [{
        "from": "2250102030466", "id": f"wamid.{uuid.uuid4().hex}", "type": "interactive", "timestamp": "1",
        "interactive": {"type": "nfm_reply", "nfm_reply": {"name": "flow", "body": "Envoyé", "response_json": response}}}]}}]}]}
    r = await client.post("/webhooks/whatsapp", json=payload)
    assert r.json()["guided"] is True
    assert "2 personnes" in _WA.sent[-1]["body"]
    trace = (await db_session.execute(select(Message).where(Message.tenant_id == tenant.id, Message.message_type == "interactive"))).scalars().all()
    assert any(m.content.startswith(f"📅 Formulaire de réservation envoyé pour le {stamp[6:8]}/{stamp[4:6]}") and "Terrasse" in m.content
               for m in trace)


@pytest.mark.asyncio
async def test_superadmin_switch_trial_and_sync(client, db_session, monkeypatch):
    headers = await _admin(client, db_session)
    assert (await client.get("/api/v1/superadmin/wa-flows/settings")).status_code == 401
    assert (await client.get("/api/v1/superadmin/wa-flows/settings", headers=headers)).json()["enabled"] is False
    tenant = await _booking_resto(db_session, mode=None)
    d = (await client.get(f"/api/v1/superadmin/tenants/{tenant.id}/wa-flows", headers=headers)).json()
    assert d == {"restaurant": True, "mode": "AUTO", "platform": False, "active": False, "status": "NOT_CREATED", "reason": None,
                 "version": None, "order_status": "NOT_CREATED", "order_reason": None}
    d = (await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/wa-flows/mode", headers=headers, json={"mode": "ON"})).json()
    assert d["mode"] == "ON" and d["active"] is True
    assert (await client.get("/api/v1/superadmin/wa-flows/settings", headers=headers)).json()["trial"] == 1
    monkeypatch.setattr(wa_flows, "MetaFlowsClient", lambda *a, **kw: FakeFlows())
    d = (await client.post(f"/api/v1/superadmin/tenants/{tenant.id}/wa-flows/sync", headers=headers)).json()
    assert d["status"] == "PUBLISHED"
    d = (await client.put("/api/v1/superadmin/wa-flows/settings", headers=headers, json={"enabled": True})).json()
    assert d["enabled"] is True and d["published"] == 1
    await client.put("/api/v1/superadmin/wa-flows/settings", headers=headers, json={"enabled": False})
    assert (await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/wa-flows/mode", headers=headers,
                             json={"mode": "MAYBE"})).status_code == 422
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
    assert 'id="flows-switch-card"' in html and "loadFlowsSwitch();" in html and "Essai : allumé ici" in html
    assert "Créer / vérifier les formulaires" in html and "loadShopFlows(t.id)" in html


@pytest.mark.asyncio
async def test_days_without_room_get_a_placeholder_list(db_session, monkeypatch):
    from datetime import datetime, timezone

    from app.services import booking

    tenant = await _booking_resto(db_session)
    calls = []

    async def only_first_day(db, tenant, settings, zone, now, day=None, part=None, limit=10, party=None):
        calls.append(day)
        return [datetime(2026, 11, 13, 19, 30, tzinfo=timezone.utc)] if len(calls) == 1 else []

    monkeypatch.setattr(booking, "free_slots", only_first_day)
    data = await wa_flows.screen_data(db_session, tenant, 2)
    assert data["days"] == [{"id": "d0", "title": data["days"][0]["title"]}] and data["t0"][0]["id"] == "202611131930"
    assert all(data[f"t{i}"] == [{"id": "none", "title": "—"}] for i in range(1, 7))  # jamais une liste vide
    monkeypatch.setattr(booking, "free_slots", lambda *a, **kw: _none())
    assert await wa_flows.screen_data(db_session, tenant, 2) is None


async def _none():
    return []
