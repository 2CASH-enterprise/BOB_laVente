"""
Lot 98 — parcours à boutons des courtiers : devis guidé par branche (2 ou 3 questions), envoi au cabinet seulement
avec l'accord du client (bouton), « Mon contrat » (registre, sans la prime), réclamation enregistrée avec l'accusé de
réception officiel, « Être rappelé ». Jamais de prix ; aucun appel d'IA.
"""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.insurance_complaint import InsuranceComplaint
from app.models.insurance_contract import InsuranceContract
from app.models.product import Product
from app.models.quote_request import QuoteRequest
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import assu_flow as asf
from app.services import guided_flow as gf
from app.services import insurance
from app.tests.test_booking import ALL_DAY
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _WA, _tap, _text, guided_on  # noqa: F401 — fixture


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


async def _cabinet(db, email, pnid=None, branches=None, booking=True, **fields):
    tenant = Tenant(name="Cabinet Kouassi", country="CI", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type="INSURANCE_BROKER", **fields)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if pnid:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    if booking:
        db.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=ALL_DAY, slot_minutes=60, capacity=1))
    if branches is None:
        await insurance.seed_products(db, tenant)
    else:
        for code in branches:
            db.add(Product(tenant_id=tenant.id, sku=f"ASSUR-{code}", name=insurance.BRANCHES[code][0], price=0,
                           currency="XOF", stock_quantity=0, active=True))
    await db.commit()
    return tenant


async def _client(db, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    return customer, conversation


async def _go(db, tenant, customer, conv, reply_id=None, text=None):
    return await gf.handle(db, tenant, text, reply_id, customer=customer, conversation=conv)


def _titles(action):
    return [b[1] for b in action["buttons"]]


# --- Accueil et branches --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_greeting_offers_quote_contract_complaint(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [menu] = (await _go(db_session, tenant, customer, conv, text="Bonjour")).actions
    assert menu["body"].startswith("Bonjour et bienvenue au *Cabinet Kouassi* !")
    assert [r[1] for r in menu["rows"]] == ["Demander un devis", "Mon contrat", "Faire une réclamation", "Parler à un conseiller"]
    assert await _go(db_session, tenant, customer, conv, text="Bonjour, c'est combien une assurance auto ?") is None  # → l'IA


@pytest.mark.asyncio
async def test_branches_are_the_ones_the_cabinet_offers(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [side] = (await _go(db_session, tenant, customer, conv, "g:qd")).actions
    assert _titles(side) == ["Pour moi", "Pour mon entreprise"]
    [mine] = (await _go(db_session, tenant, customer, conv, "g:qc:P")).actions
    assert [r[1] for r in mine["rows"]] == ["Auto", "Moto", "Santé", "Habitation", "Voyage", "Vie et prévoyance",
                                            "Scolaire", "Autre besoin"]
    [pro] = (await _go(db_session, tenant, customer, conv, "g:qc:E")).actions
    assert [r[1] for r in pro["rows"]] == ["RC professionnelle", "Flotte automobile", "Multirisque pro", "Marchandises",
                                           "Autre besoin"]
    other = await _cabinet(db_session, "x" + unique_email, branches=["SANTE", "AUTO"])
    [direct] = (await _go(db_session, other, customer, conv, "g:qd")).actions  # que des particuliers : question sautée
    assert sorted(r[1] for r in direct["rows"]) == ["Auto", "Autre besoin", "Santé"] and direct["rows"][-1][1] == "Autre besoin"


# --- Devis ----------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_quote_in_three_questions_then_consent(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email, privacy_policy_url="https://kouassi.ci/confidentialite")
    customer, conv = await _client(db_session, tenant)
    [q1] = (await _go(db_session, tenant, customer, conv, "g:qb:AUTO")).actions
    assert q1["body"] == "Question 1 sur 3 : le véhicule sert pour…"
    assert _titles(q1) == ["Usage privé", "Travail", "Taxi ou transport"]
    [q2] = (await _go(db_session, tenant, customer, conv, q1["buttons"][0][0])).actions
    assert q2["body"] == "Question 2 sur 3 : quelle formule vous intéresse ?" and _titles(q2) == ["Tiers", "Tous risques", "Je ne sais pas"]
    [q3] = (await _go(db_session, tenant, customer, conv, q2["buttons"][1][0])).actions
    assert q3["type"] == "text" and q3["body"].startswith("Question 3 sur 3 : la marque, le modèle et l'année")
    assert await gf.expects_text(db_session, tenant, customer, "Toyota Corolla 2018")
    [ok] = (await _go(db_session, tenant, customer, conv, text="Toyota Corolla 2018")).actions
    request = (await db_session.execute(select(QuoteRequest))).scalar_one()
    assert request.details["vehicle"] == "Toyota Corolla" and request.details["vehicle_year"] == 2018
    assert request.details["usage"] == "Usage privé" and request.details["coverage"] == "Tous risques"
    assert request.status == insurance.STATUS_DRAFT and request.consent_at is None  # rien ne part sans l'accord
    assert "Assurance : Assurance automobile" in ok["body"] and "Je les lui transmets ?" in ok["body"]
    assert "https://kouassi.ci/confidentialite" in ok["body"]
    assert _titles(ok) == ["Envoyer au cabinet", "Un conseiller"]
    [done] = (await _go(db_session, tenant, customer, conv, "g:qs:AUTO")).actions
    await db_session.refresh(request)
    assert request.status == insurance.STATUS_SUBMITTED and request.consent_at is not None
    assert done["body"].startswith("✅ *Demande de devis envoyée*") and _titles(done) == ["Être rappelé", "Autre demande"]
    assert "XOF" not in str(done) and "%" not in ok["body"]
    trace = (await db_session.execute(select(Message).where(Message.message_type == "quote_request"))).scalar_one()
    assert trace.content == "Demande de cotation transmise au cabinet : Assurance automobile"


@pytest.mark.asyncio
async def test_missing_year_is_asked_and_bad_answers_are_reasked(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, "g:qb:AUTO")
    await _go(db_session, tenant, customer, conv, "g:qa:AUTO:usage:1")
    await _go(db_session, tenant, customer, conv, "g:qa:AUTO:coverage:2")
    [year] = (await _go(db_session, tenant, customer, conv, text="Peugeot 308")).actions
    assert year["body"] == "Et l'année du véhicule ?"
    [again] = (await _go(db_session, tenant, customer, conv, text="je sais plus trop")).actions
    assert again["body"] == "Je n'ai pas compris cette réponse.\nEt l'année du véhicule ?"
    [ok] = (await _go(db_session, tenant, customer, conv, text="2016")).actions
    assert _titles(ok)[0] == "Envoyer au cabinet"
    request = (await db_session.execute(select(QuoteRequest))).scalar_one()
    assert request.details["coverage"] == "À conseiller" and request.details["vehicle_year"] == 2016


@pytest.mark.asyncio
async def test_other_branches_follow_their_script(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [q1] = (await _go(db_session, tenant, customer, conv, "g:qb:SANTE")).actions
    assert q1["type"] == "list" and [r[1] for r in q1["rows"]][-1] == "6 ou plus"
    await _go(db_session, tenant, customer, conv, "g:qa:SANTE:persons_count:3")
    await _go(db_session, tenant, customer, conv, text="40, 38, 12, 9")
    [ok] = (await _go(db_session, tenant, customer, conv, "g:qa:SANTE:coverage:0")).actions
    assert _titles(ok)[0] == "Envoyer au cabinet"
    [q] = (await _go(db_session, tenant, customer, conv, "g:qb:FLOTTE")).actions
    assert q["body"] == "Question 1 sur 3 : le nom de votre entreprise ?"
    await _go(db_session, tenant, customer, conv, text="Transports Kanga SARL")
    await _go(db_session, tenant, customer, conv, text="12 véhicules")
    [ok] = (await _go(db_session, tenant, customer, conv, "g:qa:FLOTTE:usage:1")).actions
    flotte = (await db_session.execute(select(QuoteRequest).where(QuoteRequest.branch == "FLOTTE"))).scalar_one()
    assert flotte.client_type == "ENTREPRISE" and flotte.details["vehicles_count"] == 12
    [one] = (await _go(db_session, tenant, customer, conv, "g:qb:AUTRE")).actions
    assert one["body"] == "Décrivez votre besoin en une phrase."
    for code, script in asf.SCRIPTS.items():
        assert {s[0] for s in script} >= set(insurance.BRANCHES[code][2]) - {"vehicle_year", "company_name"} or code == "AUTRE"
        for _, kind, _, choices in script:
            for label, _ in choices or []:
                assert len(label) <= (20 if kind == "btn" else 24), label


@pytest.mark.asyncio
async def test_a_question_during_the_quote_goes_to_the_ai(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, "g:qb:VOYAGE")
    assert await _go(db_session, tenant, customer, conv, text="Ça couvre le Covid ?") is None
    assert not await gf.expects_text(db_session, tenant, customer, "France")
    assert (await db_session.execute(select(QuoteRequest))).first() is None


@pytest.mark.asyncio
async def test_callback_on_a_free_slot(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [slots] = (await _go(db_session, tenant, customer, conv, "g:ap")).actions
    assert slots["body"] == "Un conseiller du cabinet vous appelle. Quand êtes-vous disponible ?"
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [appointment] = turn.booked
    assert appointment.kind == "APPEL" and appointment.status == "CONFIRMED"
    assert "vous appelle le" in turn.actions[0]["body"] and _titles(turn.actions[1]) == ["Autre demande", "Un conseiller"]


# --- Mon contrat ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_my_contract_shows_the_expiry_never_the_premium(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [none] = (await _go(db_session, tenant, customer, conv, "g:mc")).actions
    assert none["body"].startswith("Je ne trouve pas de contrat à votre numéro")
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, branch="AUTO", insurer="SUNU Assurances",
                                 policy_number="12345", premium=185000, currency="XOF", expires_on=date(2026, 12, 15),
                                 status="ACTIVE")
    db_session.add(contract)
    await db_session.flush()
    [card] = (await _go(db_session, tenant, customer, conv, "g:mc")).actions
    assert card["body"] == ("Votre contrat *automobile* (SUNU Assurances, n° 12345) arrive à échéance le *15 décembre 2026*.\n"
                            "Que souhaitez-vous faire ?")
    assert "185" not in card["body"] and _titles(card) == ["Le renouveler", "Le modifier", "Un conseiller"]
    turn = await _go(db_session, tenant, customer, conv, f"g:mr:{contract.id}:R")
    assert turn.handoff and turn.handoff_reason == ("Le client souhaite renouveler son contrat Assurance automobile n° 12345 "
                                                    "(échéance le 15 décembre 2026)")
    assert turn.handoff_text.startswith("C'est noté : un conseiller prépare votre renouvellement")
    db_session.add(InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, branch="HABITATION",
                                     expires_on=date(2027, 3, 1), status="ACTIVE"))
    await db_session.flush()
    [several] = (await _go(db_session, tenant, customer, conv, "g:mc")).actions
    assert [r[1:] for r in several["rows"]] == [("Automobile", "échéance le 15 décembre 2026"),
                                                ("Habitation", "échéance le 1 mars 2027")]
    other, _ = await _client(db_session, tenant)
    turn = await _go(db_session, tenant, other, conv, f"g:mr:{contract.id}:R")  # jamais le contrat d'un autre client
    assert not turn.handoff
    contract.status = "RENEWED"  # un contrat déjà renouvelé n'est plus proposé
    await db_session.flush()
    [only] = (await _go(db_session, tenant, customer, conv, "g:mc")).actions
    assert only["body"].startswith("Votre contrat *habitation*")


@pytest.mark.asyncio
async def test_a_branch_the_cabinet_does_not_offer_is_never_started(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email, branches=["SANTE"])
    customer, conv = await _client(db_session, tenant)
    [back] = (await _go(db_session, tenant, customer, conv, "g:qb:AUTO")).actions
    assert [r[1] for r in back["rows"]] == ["Santé", "Autre besoin"]
    [other] = (await _go(db_session, tenant, customer, conv, "g:qb:AUTRE")).actions
    assert other["body"] == "Décrivez votre besoin en une phrase."


# --- Réclamation ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complaint_is_registered_with_the_official_acknowledgement(db_session, unique_email):
    tenant = await _cabinet(db_session, unique_email, complaints_contact="reclamations@kouassi.ci")
    customer, conv = await _client(db_session, tenant)
    [ask] = (await _go(db_session, tenant, customer, conv, "g:rc")).actions
    assert ask["body"].startswith("Décrivez votre réclamation ou votre sinistre en un message")
    assert await gf.expects_text(db_session, tenant, customer, "Pourquoi mon remboursement n'est toujours pas fait ?")
    turn = await _go(db_session, tenant, customer, conv, text="Pourquoi mon remboursement n'est toujours pas fait ?")
    complaint = (await db_session.execute(select(InsuranceComplaint))).scalar_one()
    [ack] = turn.actions
    assert ack["message_type"] == "complaint_ack" and ack["body"].startswith(
        f"Votre réclamation est bien enregistrée sous la référence {complaint.reference}")
    assert "reclamations@kouassi.ci" in ack["body"] and complaint.notified_at is not None
    [(subject, _)] = turn.alerts
    assert complaint.reference in subject
    assert complaint.acknowledged_at is None
    ack["after_sent"]()
    assert complaint.acknowledged_at is not None


# --- Webhook --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_quote_is_emailed_to_the_cabinet_once(client, db_session, unique_email, guided_on):
    tenant = await _cabinet(db_session, unique_email, pnid="pn-l98-1", branches=["HABITATION"])
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2250700009801"
    for payload in (_tap("pn-l98-1", number, "g:qb:HABITATION", "Habitation", "list_reply"),
                    _tap("pn-l98-1", number, "g:qa:HABITATION:occupancy:1", "Locataire"),
                    _tap("pn-l98-1", number, "g:qa:HABITATION:housing_type:0", "Appartement"),
                    _tap("pn-l98-1", number, "g:qs:HABITATION", "Envoyer au cabinet")):
        r = await client.post("/webhooks/whatsapp", json=payload)
        assert r.json()["guided"] is True
    assert len(state["outbox"]) == 1 and state["outbox"][0]["subject"].startswith("Nouvelle demande de cotation — Assurance habitation")
    await client.post("/webhooks/whatsapp", json=_tap("pn-l98-1", number, "g:qs:HABITATION", "Envoyer au cabinet"))
    assert len(state["outbox"]) == 1  # jamais deux fois


@pytest.mark.asyncio
async def test_whatsapp_complaint_alerts_and_acknowledges(client, db_session, unique_email, guided_on):
    tenant = await _cabinet(db_session, unique_email, pnid="pn-l98-2")
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2250700009802"
    await client.post("/webhooks/whatsapp", json=_tap("pn-l98-2", number, "g:rc", "Faire une réclamation", "list_reply"))
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l98-2", number, "Ma voiture a été accidentée hier soir"))
    assert r.json()["guided"] is True
    complaint = (await db_session.execute(select(InsuranceComplaint))).scalar_one()
    await db_session.refresh(complaint)
    assert complaint.acknowledged_at is not None and len(state["outbox"]) == 1
    trace = (await db_session.execute(select(Message).where(Message.message_type == "complaint_ack"))).scalar_one()
    assert complaint.reference in trace.content
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None
    assert AppointmentRequest and timedelta and Tenant
