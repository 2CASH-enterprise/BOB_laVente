"""
Lot 92d — les urgences dans le fil WhatsApp du commerçant (validé le 31/10, contrat 1.3.1 inchangé) : nouvelle
commande, rendez-vous à confirmer avec ses détails, rendez-vous réservé par Bob, réclamation et devis (courtier),
client qui attend dès le transfert. Fenêtre fermée → e-mail ; jamais de modèle Meta payant.
"""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.insurance_complaint import InsuranceComplaint
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.quote_request import QuoteRequest
from app.models.tenant import Tenant
from app.services import merchant_assistant as ma
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import NOW, Sent, _linked, _replies, assistant_env  # noqa: F401


async def _people(db, tenant, name="Awa", minutes_ago=2, status=ConversationStatus.ACTIVE):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}", first_name=name)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=status,
                                last_message_at=NOW - timedelta(minutes=minutes_ago))
    db.add(conversation)
    await db.flush()
    return customer, conversation


@pytest.mark.asyncio
async def test_a_new_order_is_announced_at_once_with_vendu_annuler(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)
    customer, conversation = await _people(db_session, tenant)
    product = Product(tenant_id=tenant.id, sku="R", name="Robe wax Awa", price=16500, currency="XOF", stock_quantity=1, active=True)
    db_session.add(product)
    await db_session.flush()
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, status=OrderStatus.PENDING,
                  total_amount=34000, currency="XOF", fulfillment="DELIVERY", delivery_fee=1000, created_at=NOW - timedelta(minutes=1))
    old = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=1, currency="XOF",
                created_at=NOW - timedelta(hours=2))
    db_session.add_all([order, old])
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=2, unit_price=16500, subtotal=33000,
                             variant_label="Taille 42"))
    await db_session.commit()
    report = await ma.scan_alerts(db_session, NOW)
    assert report["orders"] == 1 and (await ma.scan_alerts(db_session, NOW))["orders"] == 0  # une seule fois
    [alert] = await _replies(db_session, tenant)
    assert alert.kind == "ALERT" and alert.ref == f"commande:{order.id}"
    assert alert.payload["texte"].startswith("🛒 Nouvelle commande de Awa : 34 000 XOF (2 × Robe wax Awa (Taille 42)), livraison.")
    assert alert.payload["texte"].endswith("Quand le paiement est reçu, touchez « Vendu ».")
    assert [b["id"] for b in alert.payload["boutons"]] == [f"bob:vendu:{order.id}", f"bob:annuler:{order.id}"]


@pytest.mark.asyncio
async def test_appointment_to_confirm_has_its_details_and_replaces_the_waiting_alert(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)
    customer, conversation = await _people(db_session, tenant, status=ConversationStatus.WAITING_HUMAN)
    appointment = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                     kind="ESSAI", vehicle_label="Toyota RAV4", availability="samedi matin",
                                     status="REQUESTED", created_at=NOW - timedelta(minutes=1))
    db_session.add(appointment)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                           message_type="handoff", content="Transfert vers un humain : Rendez-vous à confirmer",
                           created_at=NOW - timedelta(minutes=1)))
    await db_session.commit()
    report = await ma.scan_alerts(db_session, NOW)
    assert report["appointments"] == 1 and report["waiting"] == 0  # pas deux alertes pour la même demande
    assert (await ma.scan_alerts(db_session, NOW + timedelta(minutes=5)))["waiting"] == 0
    [alert] = await _replies(db_session, tenant)
    assert alert.payload["texte"].startswith("📅 Rendez-vous à confirmer : Essai — Toyota RAV4, demandé par Awa — "
                                            "disponibilités : samedi matin.")
    assert alert.payload["boutons"] == [{"id": f"bob:continue:{conversation.id}", "titre": "Bob continue"}]


@pytest.mark.asyncio
async def test_appointment_booked_by_bob_is_announced_without_button(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)
    customer, conversation = await _people(db_session, tenant)
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="VISITE", vehicle_label="Appartement 3 pièces", availability="x",
                                      status="CONFIRMED", confirmed_by="BOB", confirmed_at=NOW - timedelta(minutes=1),
                                      scheduled_at=NOW + timedelta(days=2)))
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="VISITE", availability="x", status="CONFIRMED", confirmed_by="user-1",
                                      confirmed_at=NOW - timedelta(minutes=1), scheduled_at=NOW + timedelta(days=3)))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["booked"] == 1  # confirmé par un humain : il le sait déjà
    [alert] = await _replies(db_session, tenant)
    assert alert.payload["texte"].startswith("📅 Rendez-vous réservé par Bob : Visite — Appartement 3 pièces, avec Awa, le ")
    assert "boutons" not in alert.payload


@pytest.mark.asyncio
async def test_waiting_alert_from_the_handover_even_if_the_customer_wrote_earlier(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)
    _, conversation = await _people(db_session, tenant, minutes_ago=90, status=ConversationStatus.WAITING_HUMAN)
    _, old = await _people(db_session, tenant, name="Koffi", minutes_ago=90, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                           message_type="handoff", content="Transfert vers un humain : Le client a demandé un conseiller",
                           created_at=NOW - timedelta(minutes=1)))
    db_session.add(Message(tenant_id=tenant.id, conversation_id=old.id, sender=MessageSender.SYSTEM,
                           message_type="handoff", content="Transfert vers un humain : ancien", created_at=NOW - timedelta(hours=2)))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["waiting"] == 1  # Koffi : transfert ancien, déjà connu
    [alert] = await _replies(db_session, tenant)
    assert alert.ref == f"attente:{conversation.id}" and alert.payload["texte"].startswith("Awa vous attend")


@pytest.mark.asyncio
async def test_broker_complaint_and_quote_go_to_the_thread(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)
    tenant = await db_session.get(Tenant, tenant.id)
    tenant.business_type = "INSURANCE_BROKER"
    customer, conversation = await _people(db_session, tenant)
    db_session.add(InsuranceComplaint(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                      reference="REC-2026-0007", kind="RECLAMATION", channel="WHATSAPP",
                                      subject="Remboursement toujours pas fait", status="RECEIVED",
                                      received_at=NOW - timedelta(minutes=1), due_on=date(2026, 11, 10),
                                      last_message_at=NOW, created_by="BOB"))
    db_session.add(QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                branch="AUTO", client_type="PARTICULIER", details={}, status="SUBMITTED",
                                submitted_at=NOW - timedelta(minutes=1)))
    await db_session.commit()
    report = await ma.scan_alerts(db_session, NOW)
    assert report["complaints"] == 1 and report["quotes"] == 1
    texts = sorted(r.payload["texte"] for r in await _replies(db_session, tenant))
    assert texts[0].startswith("⚠️ Réclamation REC-2026-0007 de Awa : « Remboursement toujours pas fait ». "
                               "Réponse attendue avant le 10/11/2026.")
    assert texts[1].startswith("📝 Demande de devis transmise : Assurance automobile, pour Awa.")


@pytest.mark.asyncio
async def test_quote_alerts_are_only_for_brokers(client, db_session, assistant_env):
    tenant, _, _, _ = await _linked(client, db_session)  # une boutique
    customer, conversation = await _people(db_session, tenant)
    db_session.add(QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                branch="AUTO", client_type="PARTICULIER", details={}, status="SUBMITTED",
                                submitted_at=NOW - timedelta(minutes=1)))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["quotes"] == 0


@pytest.mark.asyncio
async def test_closed_window_sends_the_alert_by_email_never_a_paid_template(client, db_session, assistant_env):
    tenant, email, _, _ = await _linked(client, db_session)
    customer, conversation = await _people(db_session, tenant)
    db_session.add(Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                         status=OrderStatus.PENDING, total_amount=5000, currency="XOF", created_at=NOW - timedelta(minutes=1)))
    await db_session.commit()
    await ma.scan_alerts(db_session, NOW)
    closed = Sent(409)
    await ma.send_outbox(db_session, NOW, sender=closed, mailer=assistant_env.mails)
    [alert] = await _replies(db_session, tenant)
    assert alert.status == "EMAILED" and "modele" not in closed.calls[0][1]
    [mail] = assistant_env.mails.sent
    assert mail["to"] == email and mail["subject"] == "Nouvelle commande : Awa"
    assert select and Product
