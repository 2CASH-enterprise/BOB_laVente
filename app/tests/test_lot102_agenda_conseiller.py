"""
Lot 102 — le rendez-vous dans l'agenda du conseiller : invitation d'agenda (.ics) dans les e-mails de rendez-vous
(réservé par Bob, déplacé ou annulé par le client, confirmé ou annulé depuis la page Rendez-vous), même identifiant
pour toute la vie du rendez-vous. Rien à connecter, aucun coût, jamais au client.
"""
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_string

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.services import calendar_invite
from app.services.email_service import build_message
from app.tests.test_booking import _auth, _dealer
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import guided_on  # noqa: F401 — fixture
from app.tests.test_lot99_boutons_relances import _rdv, _tap


def _event(**extra):
    start = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
    return {"uid": "rdv-1@agenc-ai.com", "start": start, "end": start + timedelta(minutes=60),
            "summary": "Essai — Toyota RAV4 — Awa Koné", "description": "Rendez-vous pris avec Bob.\nOuvrir : https://x/y",
            "location": "12 rue de la République, Lyon", "cancelled": False, "sequence": 5, **extra}


# --- Le fichier .ics --------------------------------------------------------------------------------------------------

def test_ics_is_a_standard_invitation():
    text = calendar_invite.ics(_event(), "conseiller@agence.fr", "bob@agenc-ai.com",
                               now=datetime(2026, 11, 3, 15, tzinfo=timezone.utc))
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and "METHOD:REQUEST" in lines and "UID:rdv-1@agenc-ai.com" in lines
    assert "DTSTART:20261107T090000Z" in lines and "DTEND:20261107T100000Z" in lines and "SEQUENCE:5" in lines
    assert "SUMMARY:Essai — Toyota RAV4 — Awa Koné" in lines
    assert "LOCATION:12 rue de la République\\, Lyon" in lines  # virgule échappée (RFC 5545)
    assert any(line.startswith("ORGANIZER;CN=Bob:mailto:bob@agenc-ai.com") for line in lines)
    assert any("mailto:conseiller@agence.fr" in line for line in text.replace("\r\n ", "").split("\r\n"))
    assert "BEGIN:VALARM" in lines and "TRIGGER:-PT1H" in lines and text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)  # lignes repliées


def test_a_cancellation_removes_the_event():
    text = calendar_invite.ics(_event(cancelled=True), "conseiller@agence.fr", "bob@agenc-ai.com")
    assert "METHOD:CANCEL" in text and "STATUS:CANCELLED" in text and "VALARM" not in text


def test_email_carries_the_invitation_and_the_ics_file(monkeypatch):
    monkeypatch.setattr(get_settings(), "smtp_from_email", "bob@agenc-ai.com")
    msg = message_from_string(build_message("conseiller@agence.fr", "Nouveau rendez-vous", "Bonjour", invite=_event()).as_string())
    assert msg.get_content_type() == "multipart/mixed"
    parts = list(msg.walk())
    calendar = next(p for p in parts if p.get_content_type() == "text/calendar")
    assert calendar.get_param("method") == "REQUEST" and "BEGIN:VEVENT" in calendar.get_payload(decode=True).decode()
    attachment = next(p for p in parts if p.get_filename() == "invitation.ics")
    assert attachment.get_content_type() == "application/ics"
    plain = message_from_string(build_message("a@b.fr", "Sujet", "Texte").as_string())
    assert plain.get_content_type() == "multipart/alternative"  # sans invitation : rien ne change


# --- L'événement d'un rendez-vous ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_follows_the_appointment(db_session, unique_email):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    event = await calendar_invite.event_for(db_session, tenant, appointment, "Awa", "https://x/c")
    assert event["uid"] == f"rdv-{appointment.id}@agenc-ai.com" and event["end"] - event["start"] == timedelta(minutes=60)
    assert event["summary"].endswith(" — Awa") and event["summary"][0].isupper() and "RAV4" in event["summary"]
    assert not event["cancelled"]
    appointment.status = "CANCELLED"
    assert (await calendar_invite.event_for(db_session, tenant, appointment, "Awa", "x"))["cancelled"]
    appointment.scheduled_at = None
    assert await calendar_invite.event_for(db_session, tenant, appointment, "Awa", "x") is None  # pas d'heure, pas d'agenda


# --- Tous les e-mails de rendez-vous du conseiller -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_moving_or_cancelling_by_the_client_updates_the_advisors_agenda(db_session, unique_email, monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    [slots] = (await _tap(db_session, tenant, customer, conversation, f"g:rm:{appointment.id}")).actions
    turn = await _tap(db_session, tenant, customer, conversation, slots["rows"][0][0])
    [(subject, _, extra)] = turn.alerts
    assert subject.startswith("Rendez-vous déplacé par le client")
    assert extra["invite"]["uid"] == f"rdv-{appointment.id}@agenc-ai.com" and extra["invite"]["start"] == appointment.scheduled_at
    turn = await _tap(db_session, tenant, customer, conversation, f"g:rny:{appointment.id}")
    [(subject, _, extra)] = turn.alerts
    assert subject.startswith("Rendez-vous annulé par le client") and extra["invite"]["cancelled"]


@pytest.mark.asyncio
async def test_bob_booking_on_whatsapp_sends_the_invitation(client, db_session, unique_email, guided_on):
    from app.tests.test_lot94_parcours_boutons import _WA, _tap as _wa_tap
    from app.tests.test_lot96_parcours_concession import _concession

    tenant, items = await _concession(db_session, unique_email, pnid="pn-l102-1")
    state = guided_on({"intents": [], "objections": []}, [])
    await client.post("/webhooks/whatsapp", json=_wa_tap("pn-l102-1", "2210700010201", f"g:e:{items[0].id}", "Réserver un essai"))
    slot = _WA.sent[-1]["rows"][0][0]
    await client.post("/webhooks/whatsapp", json=_wa_tap("pn-l102-1", "2210700010201", slot, "Lundi", "list_reply"))
    [mail] = state["outbox"]
    appointment = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert mail["subject"].startswith("Nouveau rendez-vous") and mail["invite"]["uid"] == f"rdv-{appointment.id}@agenc-ai.com"


@pytest.mark.asyncio
async def test_confirming_or_cancelling_from_the_appointments_page_mails_the_advisor(client, db_session, unique_email, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: sent.append(kw) or True)
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    appointment.status, appointment.scheduled_at = "REQUESTED", None
    await db_session.commit()
    headers = await _auth(client, unique_email)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None, microsecond=0)
    r = await client.post(f"/api/v1/appointments/{appointment.id}/confirm", headers=headers,
                          json={"scheduled_local": when.isoformat(), "notify_customer": False})
    assert r.status_code == 200, r.text
    [mail] = sent
    assert mail["to"] == unique_email and mail["subject"].startswith("Rendez-vous confirmé : ")
    assert mail["invite"]["uid"] == f"rdv-{appointment.id}@agenc-ai.com" and not mail["invite"]["cancelled"]
    r = await client.post(f"/api/v1/appointments/{appointment.id}/cancel", headers=headers, json={"notify_customer": False})
    assert r.status_code == 200 and sent[-1]["invite"]["cancelled"] and sent[-1]["subject"].startswith("Rendez-vous annulé : ")
    assert uuid
