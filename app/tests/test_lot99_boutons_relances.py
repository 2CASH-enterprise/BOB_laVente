"""
Lot 99 — boutons sur les relances de commande, les rappels de rendez-vous et la livraison ; priorité des alertes
du commerçant quand le plafond de 30 messages approche (5 derniers gardés pour les alertes). Aucun appel d'IA.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryStatus
from app.models.order import OrderStatus
from app.services import followups
from app.services import guided_flow as gf
from app.services import merchant_assistant as ma
from app.services import order_reminders as rem
from app.tests.test_booking import _dealer
from app.tests.test_lot63_relances_commandes import NOW, _conversation, _customer, _order, _shop
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import Sent, _linked, assistant_env  # noqa: F401


@pytest.fixture
def guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


def _titles(action):
    return [b[1] for b in action["buttons"]]


async def _tap(db, tenant, customer, conversation, reply_id):
    return await gf.handle(db, tenant, None, reply_id, customer=customer, conversation=conversation)


# --- Commande non payée ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_reminder_carries_three_buttons_only_with_the_guided_flow(db_session, monkeypatch):
    tenant = await _shop(db_session)
    fatou = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, fatou, wrote_hours_ago=4)
    order = await _order(db_session, tenant, fatou, hours_ago=3.9, conversation=conversation)
    calls = []

    async def send_whatsapp(db, tenant, conversation, customer, text, buttons=None):
        calls.append(buttons)
        return True

    await rem.run_for_tenant(db_session, tenant, NOW, lambda **m: True, send_whatsapp)
    assert calls == [None]  # parcours coupé : le texte part comme avant
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)
    order.reminders_sent, order.created_at = 0, NOW - timedelta(hours=3.9)
    await db_session.commit()
    await rem.run_for_tenant(db_session, tenant, NOW, lambda **m: True, send_whatsapp)
    assert calls[1] == [(f"g:op:{order.id}", "J'ai payé"), (f"g:oq:{order.id}", "Une question"), (f"g:ox:{order.id}", "Annuler")]


@pytest.mark.asyncio
async def test_order_buttons_answer_without_ai(db_session, guided):
    tenant = await _shop(db_session)
    fatou = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, fatou)
    order = await _order(db_session, tenant, fatou, hours_ago=4, conversation=conversation)
    ref = str(order.id)[:8].upper()
    [paid] = (await _tap(db_session, tenant, fatou, conversation, f"g:op:{order.id}")).actions
    assert paid["body"].startswith("Merci ! Envoyez ici la capture d'écran du paiement")
    note = (await db_session.execute(select(Message).where(Message.message_type == "client_action"))).scalar_one()
    assert note.content == f"Le client indique avoir payé la commande n° {ref}"
    question = await _tap(db_session, tenant, fatou, conversation, f"g:oq:{order.id}")
    assert question.handoff and question.handoff_reason == f"Question sur la commande n° {ref}"
    [confirm] = (await _tap(db_session, tenant, fatou, conversation, f"g:ox:{order.id}")).actions
    assert confirm["body"] == f"Annuler la commande n° {ref} (15 000 XOF) ?" and _titles(confirm) == ["Oui, annuler", "Non, la garder"]
    cancel = await _tap(db_session, tenant, fatou, conversation, f"g:oxy:{order.id}")
    assert cancel.handoff and cancel.handoff_reason == f"Le client demande l'annulation de la commande n° {ref} (15 000 XOF)"
    assert order.status == OrderStatus.PENDING  # seul un humain annule, dans le tableau de bord
    [keep] = (await _tap(db_session, tenant, fatou, conversation, f"g:oxn:{order.id}")).actions
    assert keep["body"] == "Parfait, votre commande reste enregistrée."
    other = await _customer(db_session, tenant, first="Koffi")
    turn = await _tap(db_session, tenant, other, conversation, f"g:oxy:{order.id}")  # jamais la commande d'un autre
    assert not turn.handoff
    order.status = OrderStatus.PAID
    [done] = (await _tap(db_session, tenant, fatou, conversation, f"g:ox:{order.id}")).actions
    assert done["body"] == f"La commande n° {ref} n'est plus en attente de paiement."


# --- Livraison ------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_buttons_and_answers(db_session, guided):
    tenant = await _shop(db_session)
    fatou = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, fatou)
    order = await _order(db_session, tenant, fatou, hours_ago=30, conversation=conversation)
    ref = str(order.id)[:8].upper()
    assert followups.delivery_buttons(tenant, order, "SHIPPED") == [("g:s", "Suivre ma commande"), (f"g:dp:{order.id}", "Un problème")]
    assert [t for _, t in followups.delivery_buttons(tenant, order, "DELIVERED")] == ["Bien reçue", "Pas reçue", "Racheter"]
    [thanks] = (await _tap(db_session, tenant, fatou, conversation, f"g:dk:{order.id}")).actions
    assert thanks["body"].startswith("Merci !")
    missing = await _tap(db_session, tenant, fatou, conversation, f"g:dn:{order.id}")
    assert missing.handoff and missing.handoff_reason == f"Commande n° {ref} marquée livrée mais pas reçue par le client"
    problem = await _tap(db_session, tenant, fatou, conversation, f"g:dp:{order.id}")
    assert problem.handoff_reason == f"Problème de livraison signalé : commande n° {ref}"


@pytest.mark.asyncio
async def test_delivery_notice_is_sent_with_its_buttons(db_session, guided, monkeypatch):
    from app.services import delivery_notice

    tenant = await _shop(db_session)
    fatou = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, fatou, wrote_hours_ago=1)
    order = await _order(db_session, tenant, fatou, hours_ago=30, conversation=conversation)
    delivery = Delivery(tenant_id=tenant.id, order_id=order.id, status=DeliveryStatus.DELIVERED)
    db_session.add(delivery)
    await db_session.flush()
    seen = {}

    async def fake_send(db, tenant, conversation, customer, text, user_id, message_type, buttons=None):
        seen["buttons"] = buttons
        return True, None

    monkeypatch.setattr("app.services.appointment_service.send_fixed_message", fake_send)
    result = await delivery_notice.notify(db_session, tenant, order, delivery, "u1", now=NOW)
    assert result["channel"] == "WHATSAPP" and [t for _, t in seen["buttons"]] == ["Bien reçue", "Pas reçue", "Racheter"]


# --- Rendez-vous ----------------------------------------------------------------------------------------------------

async def _rdv(db, tenant, hours=20):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}", first_name="Awa")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    from datetime import datetime, timezone

    start = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    appointment = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                     kind="ESSAI", vehicle_label="Toyota RAV4", availability="demain", status="CONFIRMED",
                                     scheduled_at=start, confirmed_by="BOB")
    db.add(appointment)
    await db.commit()
    return customer, conversation, appointment


@pytest.mark.asyncio
async def test_appointment_reminder_buttons(db_session, unique_email, guided):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    assert [t for _, t in followups.appointment_buttons(tenant, appointment)] == ["Je confirme", "Déplacer", "Annuler"]
    [ok] = (await _tap(db_session, tenant, customer, conversation, f"g:rk:{appointment.id}")).actions
    assert ok["body"] == "Parfait, c'est noté. À bientôt !"
    note = (await db_session.execute(select(Message).where(Message.message_type == "client_action"))).scalar_one()
    assert note.content.startswith("Le client a confirmé sa présence")


@pytest.mark.asyncio
async def test_the_reminder_worker_sends_the_three_buttons(guided):
    from app.tests.test_prospect_appointments import _eve_setup
    from app.workers import appointment_reminders

    engine, factory, evening = await _eve_setup(3)
    seen = []

    async def fake_whatsapp(db, tenant, conversation, customer, text, buttons=None):
        seen.append(buttons)
        return True

    report = await appointment_reminders.send_due_reminders(session_factory=factory, now=evening, send=lambda **kw: True,
                                                            send_whatsapp=fake_whatsapp)
    assert report["whatsapp"] == 1 and [t for _, t in seen[0]] == ["Je confirme", "Déplacer", "Annuler"]
    assert seen[0][0][0].startswith("g:rk:")
    await engine.dispose()


@pytest.mark.asyncio
async def test_move_an_appointment_to_a_free_slot(db_session, unique_email, guided):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    before = appointment.scheduled_at
    [slots] = (await _tap(db_session, tenant, customer, conversation, f"g:rm:{appointment.id}")).actions
    assert slots["type"] == "list" and slots["rows"][-1][1] == "Autre moment"
    turn = await _tap(db_session, tenant, customer, conversation, slots["rows"][0][0])
    await db_session.refresh(appointment)
    assert appointment.scheduled_at != before and appointment.rescheduled_by == "CLIENT"
    assert turn.actions[0]["message_type"] == "appointment_changed" and len(turn.alerts) == 1
    other = await _tap(db_session, tenant, customer, conversation, f"g:rs:{appointment.id}:x")
    assert other.handoff and other.handoff_reason.startswith("Le client souhaite déplacer son rendez-vous")


@pytest.mark.asyncio
async def test_cancel_an_appointment_after_confirmation(db_session, unique_email, guided):
    tenant = await _dealer(db_session, unique_email)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    [ask] = (await _tap(db_session, tenant, customer, conversation, f"g:rn:{appointment.id}")).actions
    assert _titles(ask) == ["Oui, annuler", "Plutôt déplacer", "Non, je viens"]
    assert appointment.status == "CONFIRMED"
    [keep] = (await _tap(db_session, tenant, customer, conversation, f"g:rnn:{appointment.id}")).actions
    assert keep["body"].startswith("Parfait, votre rendez-vous est maintenu")
    turn = await _tap(db_session, tenant, customer, conversation, f"g:rny:{appointment.id}")
    await db_session.refresh(appointment)
    assert appointment.status == "CANCELLED" and appointment.cancelled_by == "CLIENT" and len(turn.alerts) == 1
    [gone] = (await _tap(db_session, tenant, customer, conversation, f"g:rk:{appointment.id}")).actions
    assert gone["body"].startswith("Ce rendez-vous n'est plus prévu")


@pytest.mark.asyncio
async def test_without_online_booking_moving_goes_to_an_advisor(db_session, unique_email, guided):
    tenant = await _dealer(db_session, unique_email, hours=False)
    customer, conversation, appointment = await _rdv(db_session, tenant)
    turn = await _tap(db_session, tenant, customer, conversation, f"g:rm:{appointment.id}")
    assert turn.handoff and turn.handoff_text.startswith("C'est noté : un conseiller vous propose une autre date")


# --- Envoi d'un message à boutons -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fixed_message_with_buttons_is_sent_and_traced(db_session, monkeypatch, guided):
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.appointment_service import send_fixed_message

    tenant = await _shop(db_session)
    if (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).first() is None:
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-l99", system_user_token="t"))
    fatou = await _customer(db_session, tenant)
    conversation = await _conversation(db_session, tenant, fatou, wrote_hours_ago=1)
    sent = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def send_buttons(self, to, body, buttons, image_link=None):
            sent.append(("buttons", body, buttons))

        async def send_text_message(self, to, body):
            sent.append(("text", body))

    monkeypatch.setattr("app.integrations.whatsapp.client.WhatsAppClient", Client)
    monkeypatch.setattr("app.services.human_reply.ensure_reply_window_open", lambda *a, **k: _none())
    ok, _ = await send_fixed_message(db_session, tenant, conversation, fatou, "Rappel", "BOB", "order_reminder",
                                     buttons=[("g:op:1", "J'ai payé")])
    assert ok and sent == [("buttons", "Rappel", [("g:op:1", "J'ai payé")])]
    trace = (await db_session.execute(select(Message).where(Message.message_type == "order_reminder"))).scalar_one()
    assert trace.content == "Rappel\n[Boutons : J'ai payé]"


async def _none():
    return None


# --- Priorité des alertes du commerçant -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_five_messages_of_the_day_are_kept_for_alerts(client, db_session, assistant_env):
    _, _, _, connection = await _linked(client, db_session)
    replies = [ma.queue(db_session, connection, f"réponse {i}", now=NOW) for i in range(26)]
    alert = ma.queue(db_session, connection, "alerte", kind="ALERT", now=NOW + timedelta(seconds=1))
    await db_session.commit()
    sender = Sent()
    await ma.send_outbox(db_session, NOW, sender=sender, mailer=assistant_env.mails)
    assert sender.calls[0][1]["texte"] == "alerte"  # les alertes d'abord
    assert alert.status == "SENT" and len(sender.calls) == 25
    sent_replies = [r for r in replies if r.status == "SENT"]
    assert len(sent_replies) == 24 and sent_replies[-1].payload["texte"].endswith(ma.RESERVE_NOTICE)
    assert [r.status for r in replies[24:]] == ["SKIPPED", "SKIPPED"]
    late = ma.queue(db_session, connection, "alerte tardive", kind="ALERT", now=NOW)
    await db_session.commit()
    await ma.send_outbox(db_session, NOW, sender=sender, mailer=assistant_env.mails)
    assert late.status == "SENT"  # il reste de la place pour les alertes
