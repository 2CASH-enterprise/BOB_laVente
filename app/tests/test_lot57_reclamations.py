"""
Lot 57 — courtier (règlement CIMA 01-24) : registre des réclamations et des sinistres (art. 11 : traçabilité,
délai de traitement, plusieurs canaux) et pays du risque (art. 10 : domiciliation des risques).
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.insurance_complaint import InsuranceComplaint
from app.models.quote_request import QuoteRequest
from app.models.user import Role
from app.services import insurance, insurance_complaints as ic
from app.services.business_type import CAR_DEALERSHIP
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _cabinet, _headers, _payload
from app.tests.test_lot55_contrats import _login, _user

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 9, 14, 5, tzinfo=timezone.utc)  # vendredi 9 octobre 2026, 14 h 05 à Abidjan (UTC+0)


def _function(name):
    body = HTML[HTML.index(f"function {name}("):]
    return body[:body.index("\n}\n") + 2]


async def _customer(db, tenant, number=None, first="Awa"):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number or f"2250{uuid.uuid4().int % 10**9:09d}", first_name=first)
    db.add(customer)
    await db.commit()
    return customer


# --- Règles ------------------------------------------------------------------------------------------------

def test_kind_is_guessed_by_keywords():
    for text in ("J'ai eu un accident hier", "On m'a volé ma moto", "Incendie dans la boutique", "dégât des eaux chez moi",
                 "Mon pare-brise est cassé, bris de glace", "Ma voiture a été accidentée", "Il y a eu un décès",
                 "Mon fils est hospitalisé", "Sinistre à déclarer"):
        assert ic.guess_kind(text) == ic.SINISTRE, text
    for text in ("Je ne suis pas content du service", "Personne ne me rappelle depuis une semaine",
                 "Vous m'avez facturé deux fois", "Je veux me plaindre", "volontiers, merci", ""):
        assert ic.guess_kind(text) == ic.RECLAMATION, text


def test_business_days():
    friday = date(2026, 10, 9)
    assert ic.add_business_days(friday, 1) == date(2026, 10, 12)   # lundi
    assert ic.add_business_days(friday, 10) == date(2026, 10, 23)
    assert ic.add_business_days(date(2026, 10, 10), 1) == date(2026, 10, 12)  # samedi → lundi
    assert ic.add_business_days(date(2026, 10, 7), 2) == date(2026, 10, 9)    # mercredi → vendredi
    assert ic.add_business_days(friday, 0) == friday


@pytest.mark.asyncio
async def test_due_date_and_acknowledgement(db_session):
    tenant = await _cabinet(db_session)
    assert tenant.complaint_delay_days == 10
    assert ic.due_date(tenant, ic.RECLAMATION, NOW) == date(2026, 10, 23)
    assert ic.due_date(tenant, ic.SINISTRE, NOW) is None
    tenant.complaint_delay_days = 1
    complaint = InsuranceComplaint(reference="R-2026-0007", kind=ic.RECLAMATION, received_at=NOW)
    assert ic.acknowledgement(tenant, complaint) == (
        "Votre réclamation est bien enregistrée sous la référence R-2026-0007, le 09/10/2026 à 14 h 05. "
        "Le cabinet s'engage à vous répondre sous 1 jour ouvré.")
    tenant.complaint_delay_days = 15
    tenant.complaints_contact = "reclamations@cabinet.ci"
    text = ic.acknowledgement(tenant, complaint)
    assert "sous 15 jours ouvrés." in text and text.endswith("Contact réclamations : reclamations@cabinet.ci.")
    complaint.kind = ic.SINISTRE
    sinistre = ic.acknowledgement(tenant, complaint)
    assert "Votre déclaration est bien enregistrée sous la référence R-2026-0007" in sinistre
    assert "jours ouvrés" not in sinistre and "pris en charge" not in sinistre
    assert not insurance.contains_amount(text) and not insurance.contains_amount(sinistre)


@pytest.mark.asyncio
async def test_references_follow_each_other_per_cabinet_and_year(db_session):
    tenant = await _cabinet(db_session)
    other = await _cabinet(db_session)
    awa = await _customer(db_session, tenant)
    first, created = await ic.record_from_whatsapp(db_session, tenant, awa, None, "Je ne suis pas content", NOW)
    again, created_again = await ic.record_from_whatsapp(db_session, tenant, awa, None, "Toujours rien !", NOW + timedelta(hours=1))
    assert created and not created_again and again.id == first.id and first.follow_ups == 1
    assert first.reference == "R-2026-0001" and first.due_on == date(2026, 10, 23)
    ic.resolve(first, "u", "Remboursement des frais de dossier", NOW + timedelta(days=1))
    second, created = await ic.record_from_whatsapp(db_session, tenant, awa, None, "Nouveau souci", NOW + timedelta(days=2))
    assert created and second.reference == "R-2026-0002"
    other_customer = await _customer(db_session, other)
    elsewhere, _ = await ic.record_from_whatsapp(db_session, other, other_customer, None, "Plainte", NOW)
    assert elsewhere.reference == "R-2026-0001"
    nextyear, _ = await ic.record_from_whatsapp(db_session, tenant, await _customer(db_session, tenant), None, "x",
                                                datetime(2027, 1, 4, 10, tzinfo=timezone.utc))
    assert nextyear.reference == "R-2027-0001"


def test_steps():
    complaint = InsuranceComplaint(status=ic.RECEIVED, kind=ic.RECLAMATION, received_at=NOW)
    ic.start(complaint, "u1", NOW)
    assert complaint.status == ic.IN_PROGRESS and complaint.started_by == "u1"
    with pytest.raises(ic.ComplaintError):
        ic.start(complaint, "u1", NOW)
    with pytest.raises(ic.ComplaintError, match="réponse apportée"):
        ic.resolve(complaint, "u2", "  ok ", NOW)
    ic.resolve(complaint, "u2", "  Erreur corrigée, client rappelé.  ", NOW + timedelta(hours=2))
    assert complaint.status == ic.RESOLVED and complaint.resolution == "Erreur corrigée, client rappelé."
    assert complaint.started_by == "u1" and complaint.resolved_by == "u2"
    with pytest.raises(ic.ComplaintError):
        ic.resolve(complaint, "u2", "encore une fois", NOW)
    direct = InsuranceComplaint(status=ic.RECEIVED, kind=ic.RECLAMATION, received_at=NOW)
    ic.resolve(direct, "u3", "Réglé au téléphone", NOW)
    assert direct.started_at == NOW and direct.started_by == "u3"  # prise en charge implicite, horodatée
    late = InsuranceComplaint(status=ic.IN_PROGRESS, due_on=date(2026, 10, 8))
    assert ic.is_overdue(late, date(2026, 10, 9)) and not ic.is_overdue(late, date(2026, 10, 8))
    late.status = ic.RESOLVED
    assert not ic.is_overdue(late, date(2026, 10, 20))


# --- WhatsApp : enregistrement par le code, accusé de réception -------------------------------------------

@pytest.fixture
def sends(monkeypatch):
    sent = []

    class _Recorder:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            sent.append({"to": to, "body": body})
            return {}

    monkeypatch.setattr("app.integrations.whatsapp.client.WhatsAppClient", _Recorder)
    return sent, _Recorder


@pytest.mark.asyncio
async def test_whatsapp_complaint_is_registered_and_acknowledged(client, db_session, wire, sends, monkeypatch):  # noqa: F811
    tenant = await _cabinet(db_session)
    tenant.complaints_contact = "reclamations@cabinet.ci"
    await db_session.commit()
    sent, recorder = sends
    state = wire({"intents": ["RECLAMATION"], "objections": []}, [
        tool_use_response("handoff_to_human", {"reason": "réclamation"}),
        text_response("Je comprends, je transmets votre réclamation au cabinet."),
    ])
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", recorder)

    first = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250711111111",
                                                                   "Personne ne me rappelle, je ne suis pas content"))
    assert first.status_code == 200
    complaint = (await db_session.execute(select(InsuranceComplaint))).scalar_one()
    assert complaint.reference.startswith("R-") and complaint.kind == "RECLAMATION" and complaint.channel == "WHATSAPP"
    assert complaint.subject == "Personne ne me rappelle, je ne suis pas content" and complaint.created_by == "BOB"
    assert complaint.acknowledged_at is not None and complaint.notified_at is not None and complaint.due_on is not None
    assert [m["body"] for m in sent][0].startswith("Je comprends")  # la réponse de Bob d'abord…
    ack = sent[-1]["body"]  # … puis l'accusé de réception fixe
    assert ack.startswith(f"Votre réclamation est bien enregistrée sous la référence {complaint.reference}")
    assert "10 jours ouvrés" in ack and "reclamations@cabinet.ci" in ack
    stored = (await db_session.execute(select(Message).where(Message.message_type == "complaint_ack"))).scalar_one()
    assert stored.content == ack and stored.sender == MessageSender.SYSTEM
    mails = [m for m in state["outbox"] if m["subject"].startswith("Nouvelle réclamation")]
    assert len(mails) == 1 and mails[0]["to"] == tenant.email and complaint.reference in mails[0]["subject"]
    assert "Message du client :\nPersonne ne me rappelle" in mails[0]["body"] and "/dashboard/?conversation=" in mails[0]["body"]
    # La conversation est passée à un humain : le message suivant se rattache, sans nouvel accusé
    conversation = (await db_session.execute(select(Conversation).execution_options(populate_existing=True))).scalar_one()
    assert conversation.status == ConversationStatus.WAITING_HUMAN
    count = len(sent)
    await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250711111111", "Et toujours rien !"))
    await db_session.refresh(complaint)
    assert complaint.follow_ups == 1 and len(sent) == count
    assert (await db_session.execute(select(func.count(InsuranceComplaint.id)))).scalar_one() == 1
    assert len([m for m in state["outbox"] if m["subject"].startswith("Nouvelle réclamation")]) == 1


@pytest.mark.asyncio
async def test_complaint_while_a_human_has_the_conversation(client, db_session, wire, sends):  # noqa: F811
    tenant = await _cabinet(db_session)
    customer = await _customer(db_session, tenant, number="2250722222222")
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN)
    db_session.add(conversation)
    await db_session.commit()
    sent, _ = sends
    state = wire({"intents": ["RECLAMATION"], "objections": []}, [])
    await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250722222222", "J'ai eu un accident ce matin"))
    complaint = (await db_session.execute(select(InsuranceComplaint))).scalar_one()
    assert complaint.kind == "SINISTRE" and complaint.due_on is None and complaint.acknowledged_at is not None
    assert len(sent) == 1 and sent[0]["body"].startswith(f"Votre déclaration est bien enregistrée sous la référence {complaint.reference}")
    mails = [m for m in state["outbox"] if m["subject"].startswith("Déclaration de sinistre")]
    assert len(mails) == 1 and "À traiter avant le" not in mails[0]["body"]
    assert state["llm"].calls == [] if hasattr(state["llm"], "calls") else True  # Bob ne répond pas : un humain a la main


@pytest.mark.asyncio
async def test_no_register_outside_insurance_or_without_complaint(client, db_session, wire, sends):  # noqa: F811
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    wire({"intents": ["RECLAMATION"], "objections": []}, [text_response("Je transmets.")])
    await client.post("/webhooks/whatsapp", json=_payload(dealer.pnid, "2250733333333", "Je ne suis pas content"))
    tenant = await _cabinet(db_session)
    wire({"intents": ["INFO_PRODUIT"], "objections": []}, [text_response("Bonjour !")])
    await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250744444444", "Bonjour, une assurance auto ?"))
    assert (await db_session.execute(select(func.count(InsuranceComplaint.id)))).scalar_one() == 0


def test_bob_never_gives_the_reference_himself():
    from app.services.handoff_rules import _insurance_rule

    decision = _insurance_rule({"RECLAMATION"}, set())
    assert "Ne donne ni numéro de référence ni délai" in decision.instruction


# --- API ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_complaint_and_follow_up(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _login(client, await _user(db_session, tenant, Role.AGENT))
    made = await client.post("/api/v1/complaints", headers=headers, json={
        "phone": "07 00 00 00 21", "first_name": "Koffi", "kind": "RECLAMATION", "channel": "PHONE",
        "subject": "Attestation jamais reçue", "received_on": "2026-10-08"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["reference"].startswith("R-") and body["channel_label"] == "Téléphone" and body["status_label"] == "Reçue"
    assert body["customer"].startswith("Koffi") and body["due_on"] is not None and not body["created_by_bob"]
    for payload, message in (
        ({"phone": "0700000021", "subject": "  "}, "Décrivez"),
        ({"phone": "0700000021", "subject": "x", "channel": "FAX"}, "Canal inconnu"),
        ({"phone": "0700000021", "subject": "x", "kind": "AUTRE"}, "Type inconnu"),
        ({"phone": "12", "subject": "x"}, "Téléphone"),
        ({"phone": "0700000021", "subject": "x", "received_on": "2099-01-01"}, "date de réception"),
        ({"phone": "0700000021", "subject": "x", "received_on": "hier"}, "Date de réception invalide"),
    ):
        refused = await client.post("/api/v1/complaints", headers=headers, json=payload)
        assert refused.status_code == 422 and message in refused.json()["detail"], (payload, refused.text)

    cid = body["id"]
    todo = (await client.get("/api/v1/complaints?view=todo", headers=headers)).json()
    assert [c["id"] for c in todo["complaints"]] == [cid] and todo["counts"]["todo"] == 1
    assert {c["value"] for c in todo["channels"]} == {"PHONE", "EMAIL", "OFFICE", "OTHER"}
    started = (await client.post(f"/api/v1/complaints/{cid}/start", headers=headers)).json()
    assert started["status"] == "IN_PROGRESS" and started["started_at"]
    assert (await client.post(f"/api/v1/complaints/{cid}/start", headers=headers)).status_code == 409
    kind = (await client.post(f"/api/v1/complaints/{cid}/kind", headers=headers, json={"kind": "SINISTRE"})).json()
    assert kind["kind_label"] == "Sinistre" and kind["due_on"] is None
    kind = (await client.post(f"/api/v1/complaints/{cid}/kind", headers=headers, json={"kind": "RECLAMATION"})).json()
    assert kind["due_on"] is not None
    assert (await client.post(f"/api/v1/complaints/{cid}/resolve", headers=headers, json={"resolution": "ok"})).status_code == 422
    done = (await client.post(f"/api/v1/complaints/{cid}/resolve", headers=headers,
                              json={"resolution": "Attestation renvoyée par email"})).json()
    assert done["status_label"] == "Traitée" and done["resolution"] == "Attestation renvoyée par email"
    assert [c["id"] for c in (await client.get("/api/v1/complaints?view=resolved", headers=headers)).json()["complaints"]] == [cid]
    assert (await client.get("/api/v1/complaints?view=xyz", headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_late_view_counts_and_export(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    awa = await _customer(db_session, tenant)
    late = InsuranceComplaint(tenant_id=tenant.id, customer_id=awa.id, reference="R-2026-0001", kind="RECLAMATION",
                              channel="WHATSAPP", subject="Ancienne; plainte \"guillemets\"", status="IN_PROGRESS",
                              received_at=datetime.now(timezone.utc) - timedelta(days=30),
                              due_on=date.today() - timedelta(days=5), created_by="BOB")
    fresh = InsuranceComplaint(tenant_id=tenant.id, customer_id=awa.id, reference="R-2026-0002", kind="SINISTRE",
                               channel="PHONE", subject="Accident", status="RECEIVED",
                               received_at=datetime.now(timezone.utc), created_by="u")
    db_session.add_all([late, fresh])
    await db_session.commit()
    listing = (await client.get("/api/v1/complaints?view=late", headers=headers)).json()
    assert [c["reference"] for c in listing["complaints"]] == ["R-2026-0001"] and listing["complaints"][0]["overdue"]
    assert listing["counts"] == {"todo": 1, "progress": 1, "late": 1}
    from app.services.notifications import TITLES, task_counts

    assert (await task_counts(db_session, tenant))["complaints"] == 2  # à traiter + en cours mais en retard
    assert list(TITLES)[1] == "complaints"
    export = await client.get("/api/v1/complaints/export.csv", headers=headers)
    assert export.status_code == 200 and "registre_reclamations.csv" in export.headers["content-disposition"]
    lines = export.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Référence;Type;Canal;Client") and len(lines) == 3
    assert lines[1].startswith("R-2026-0001;Réclamation;WhatsApp;") and ";oui;" in lines[1]
    assert '"Ancienne; plainte ""guillemets"""' in lines[1]


@pytest.mark.asyncio
async def test_settings_rights_and_isolation(client, db_session):
    tenant = await _cabinet(db_session)
    owner = await _headers(client, tenant)
    manager = await _login(client, await _user(db_session, tenant, Role.MANAGER))
    viewer = await _login(client, await _user(db_session, tenant, Role.VIEWER))
    assert (await client.put("/api/v1/complaints/settings", headers=manager, json={"complaint_delay_days": 5})).status_code == 403
    assert (await client.put("/api/v1/complaints/settings", headers=owner, json={"complaint_delay_days": 0})).status_code == 422
    assert (await client.put("/api/v1/complaints/settings", headers=owner, json={"complaint_delay_days": 61})).status_code == 422
    assert (await client.put("/api/v1/complaints/settings", headers=owner, json={"complaint_delay_days": 15})).json() == {
        "complaint_delay_days": 15}
    assert (await client.post("/api/v1/complaints", headers=viewer, json={"phone": "0700000001", "subject": "x"})).status_code == 403
    made = (await client.post("/api/v1/complaints", headers=owner, json={"phone": "0700000001", "subject": "Plainte"})).json()
    other = await _cabinet(db_session)
    other_headers = await _headers(client, other)
    assert (await client.get("/api/v1/complaints?view=all", headers=other_headers)).json()["complaints"] == []
    for path in ("start", "resolve", "kind"):
        payload = {"resolution": "réponse donnée"} if path == "resolve" else {"kind": "SINISTRE"}
        assert (await client.post(f"/api/v1/complaints/{made['id']}/{path}", headers=other_headers, json=payload)).status_code == 404
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    assert (await client.get("/api/v1/complaints", headers=await _headers(client, dealer))).status_code == 403


@pytest.mark.asyncio
async def test_home_lists_complaints_first_after_waiting_clients(db_session):
    from app.services.home_service import home_summary

    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant)
    complaint, _ = await ic.record_from_whatsapp(db_session, tenant, awa, None, "Je veux me plaindre")
    started = InsuranceComplaint(tenant_id=tenant.id, customer_id=awa.id, reference="R-2026-0099", kind="RECLAMATION",
                                 channel="PHONE", subject="Suivi", status="IN_PROGRESS", received_at=datetime.now(timezone.utc),
                                 due_on=date.today() + timedelta(days=3), created_by="u")
    db_session.add(started)
    await db_session.commit()
    todo = [t for t in (await home_summary(db_session, tenant.id))["todo"] if t["kind"] == "COMPLAINT"]
    assert len(todo) == 1 and todo[0]["reason"] == "Réclamation" and complaint.reference in todo[0]["detail"]


# --- Pays du risque (art. 10) -----------------------------------------------------------------------------

def test_country_parsing_and_phone_prefix():
    assert insurance.parse_country("Sénégal") == "SN" and insurance.parse_country("sn") == "SN"
    assert insurance.parse_country("Côte d'Ivoire") == "CI" and insurance.parse_country("RDC") == "CD"
    assert insurance.parse_country("Chine") == "Chine" and insurance.parse_country("  ") is None
    assert insurance.country_name("SN") == "Sénégal" and insurance.country_name("Chine") == "Chine"
    assert insurance.country_from_phone("221771234567") == "SN"
    assert insurance.country_from_phone("2250700000001") == "CI"
    assert insurance.country_from_phone("33612345678") == "FR"
    assert insurance.country_from_phone("2241234") is None  # trop court
    assert insurance.country_from_phone("999999999999") is None
    from app.services.countries import COUNTRY_CODES
    from app.services.insurance_contracts import DIAL_CODES

    prefixes = list(DIAL_CODES.values())
    assert set(DIAL_CODES) == set(COUNTRY_CODES)  # chaque pays proposé a son indicatif
    assert not [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a)]  # aucun n'est le début d'un autre


@pytest.mark.asyncio
async def test_risk_country_in_the_request_and_the_email(db_session):
    tenant = await _cabinet(db_session)
    customer = await _customer(db_session, tenant, number="221771234567")
    result = await insurance.update_request(db_session, tenant.id, customer.id, None, {
        "branch": "AUTO", "vehicle": "Corolla", "vehicle_year": 2018, "usage": "privé", "risk_country": "Sénégal",
        "consent": True})
    request = result["request"]
    assert request.details["risk_country"] == "SN" and request.status == "SUBMITTED"
    assert "Pays du risque : Sénégal" in insurance.request_lines(request)
    warning = insurance.domiciliation_warning(tenant, request)
    assert warning.startswith("Risque situé hors de Côte d'Ivoire (Sénégal)") and "art. 10" in warning
    subject, body = insurance.quote_email(tenant, customer, request, "https://x/dashboard/?conversation=1")
    assert "⚠️ Risque situé hors de Côte d'Ivoire (Sénégal)" in body
    request.details = {**request.details, "risk_country": "CI"}
    assert insurance.domiciliation_warning(tenant, request) is None
    assert "Risque situé" not in insurance.quote_email(tenant, customer, request, "l")[1]


@pytest.mark.asyncio
async def test_bob_asks_where_the_risk_is_for_a_foreign_number(db_session):
    from app.services.customer_memory_service import build_customer_memory

    tenant = await _cabinet(db_session)
    foreign = await _customer(db_session, tenant, number="221771234567")
    local = await _customer(db_session, tenant, number="2250700000001")
    memory = await build_customer_memory(db_session, tenant.id, foreign.id)
    assert "PAYS DU CLIENT" in memory and "Sénégal" in memory and "risk_country" in memory and "Ne refuse jamais" in memory
    assert "PAYS DU CLIENT" not in await build_customer_memory(db_session, tenant.id, local.id)
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    other = await _customer(db_session, dealer, number="221771234567")
    assert "PAYS DU CLIENT" not in await build_customer_memory(db_session, dealer.id, other.id)


@pytest.mark.asyncio
async def test_quote_api_shows_the_warning(client, db_session):
    from app.agents.prompts import build_system_prompt
    from app.agents.tool_definitions import TOOL_DEFINITIONS

    tenant = await _cabinet(db_session)
    customer = await _customer(db_session, tenant)
    db_session.add(QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, branch="HABITATION", client_type="PARTICULIER",
                                details={"risk_country": "Chine"}, status="SUBMITTED", submitted_at=datetime.now(timezone.utc)))
    await db_session.commit()
    rows = (await client.get("/api/v1/quote-requests?view=todo", headers=await _headers(client, tenant))).json()
    assert "Chine" in rows[0]["warning"]
    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_insurance_request")
    assert "risk_country" in tool["input_schema"]["properties"]
    assert "18. Si le client indique que ce qu'il veut assurer se trouve dans un autre pays" in build_system_prompt(tenant, [], "")


# --- Tableau de bord ----------------------------------------------------------------------------------------

def test_dashboard_complaints_page():
    assert 'id="nav-complaints"' in HTML and 'id="tab-complaints"' in HTML
    nav = HTML[HTML.index('id="nav-complaints"') - 200:HTML.index('id="nav-complaints"') + 200]
    assert "insurance-only" in nav and "complaints: [\"complaints\"]" in HTML
    assert '"customers", "complaints", "products"' in _function("showTab") and "loadComplaints(currentComplaintView)" in _function("showTab")
    render = _function("renderComplaints")
    for piece in ("esc(c.reference)", "esc(c.customer)", "esc(c.kind_label)", "esc(c.subject)", "esc(c.resolution)",
                  "steps.map(esc)"):
        assert piece in render, piece
    for name in ("openComplaintDialog", "startComplaint", "resolveComplaint", "changeComplaintKind", "editComplaintDelay",
                 "exportComplaints"):
        assert f"function {name}(" in HTML, name
    assert 'if (item.kind === "COMPLAINT")' in _function("todoAction")
    assert "q.warning ? `<p class=\"quote-warning\">⚠️ ${esc(q.warning)}</p>`" in HTML
    block = HTML[HTML.index("// ---- Lot 57"):HTML.index("// Lot 55 — suivi des demandes de cotation")]
    assert "confirm(" not in block.replace("bobConfirm(", "") and "prompt(" not in block
    import re

    assert not re.search(r"^(?:const|let) ", block, re.M)  # rien de déclaré au niveau du script en const (démarrage, lot 56)
