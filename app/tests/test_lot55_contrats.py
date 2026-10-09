"""
Lot 55 — courtier : registre des contrats, échéances (alerte au cabinet, rappel unique au client), suivi des
demandes jusqu'à « souscrit / perdu », lien avec le score. La prime et le numéro de contrat ne sortent jamais
du tableau de bord (CIMA) ; la prime n'est vue que des administrateurs.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.insurance_contract import InsuranceContract
from app.models.quote_request import QuoteRequest
from app.models.user import Role, User
from app.services import insurance, insurance_contracts as ic, insurance_prospect
from app.services.business_type import CAR_DEALERSHIP
from app.tests.test_lot53_courtier import _cabinet, _headers

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)  # vendredi 9 octobre 2026, 10 h à Abidjan (UTC+0)
TODAY = NOW.date()


async def _user(db, tenant, role):
    email = f"{role.value.lower()}{uuid.uuid4().hex[:6]}@l55.ci"
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    await db.commit()
    return email


async def _login(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _customer(db, tenant, first="Awa", email=None, number=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number or f"2250{uuid.uuid4().int % 10**9:09d}",
                        first_name=first, email=email)
    db.add(customer)
    await db.commit()
    return customer


async def _contract(db, tenant, customer, days, term="ANNUEL", **extra):
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, branch=extra.pop("branch", "AUTO"),
                                 insurer=extra.pop("insurer", "SUNU Assurances"), expires_on=TODAY + timedelta(days=days),
                                 term=term, status=extra.pop("status", "ACTIVE"), **extra)
    db.add(contract)
    await db.commit()
    return contract


# --- Délais ----------------------------------------------------------------------------------------------

def test_alert_and_reminder_windows_follow_the_term():
    assert [ic.alert_days(t) for t in ("MENSUEL", "TRIMESTRIEL", "SEMESTRIEL", "ANNUEL", None)] == [15, 45, 45, 45, 45]
    assert [ic.reminder_days(t) for t in ("MENSUEL", "TRIMESTRIEL", "SEMESTRIEL", "ANNUEL", None)] == [7, 22, 30, 30, 30]
    assert ic.add_term(date(2026, 1, 31), "MENSUEL") == date(2026, 2, 28)
    assert ic.add_term(date(2026, 11, 30), "TRIMESTRIEL") == date(2027, 2, 28)
    assert ic.add_term(date(2026, 12, 31), "ANNUEL") == date(2027, 12, 31)
    assert ic.add_term(date(2026, 8, 31), "SEMESTRIEL") == date(2027, 2, 28)


def test_windows_edges():
    c = InsuranceContract(status="ACTIVE", term="ANNUEL", expires_on=TODAY + timedelta(days=45))
    assert ic.in_alert_window(c, TODAY) and ic.needs_attention(c, TODAY)
    c.expires_on = TODAY + timedelta(days=46)
    assert not ic.in_alert_window(c, TODAY)
    c.expires_on = TODAY - timedelta(days=30)
    assert ic.in_alert_window(c, TODAY)
    c.expires_on = TODAY - timedelta(days=31)
    assert not ic.in_alert_window(c, TODAY)
    c.expires_on = TODAY + timedelta(days=30)
    assert ic.reminder_due(c, TODAY)
    c.expires_on = TODAY + timedelta(days=31)
    assert not ic.reminder_due(c, TODAY)
    c.expires_on = TODAY - timedelta(days=1)
    assert not ic.reminder_due(c, TODAY)  # jamais de rappel après l'échéance
    c.expires_on = TODAY
    assert ic.reminder_due(c, TODAY)
    c.renewal_handled_at = NOW
    assert not ic.reminder_due(c, TODAY) and not ic.needs_attention(c, TODAY) and ic.in_alert_window(c, TODAY)
    c.renewal_handled_at, c.status = None, "CANCELLED"
    assert not ic.reminder_due(c, TODAY) and not ic.in_alert_window(c, TODAY)
    monthly = InsuranceContract(status="ACTIVE", term="MENSUEL", expires_on=TODAY + timedelta(days=8))
    assert not ic.reminder_due(monthly, TODAY) and ic.in_alert_window(monthly, TODAY)
    monthly.expires_on = TODAY + timedelta(days=7)
    assert ic.reminder_due(monthly, TODAY)
    monthly.expires_on = TODAY + timedelta(days=16)
    assert not ic.in_alert_window(monthly, TODAY)


# --- Valeurs saisies -------------------------------------------------------------------------------------

def test_phone_normalization():
    assert ic.normalize_phone("07 00 00 00 01", "CI") == "2250700000001"   # le 0 fait partie du numéro ivoirien
    assert ic.normalize_phone("+225 07 00 00 00 01", "CI") == "2250700000001"
    assert ic.normalize_phone("2250700000001", "CI") == "2250700000001"
    assert ic.normalize_phone("0033 6 12 34 56 78", "CI") == "33612345678"
    assert ic.normalize_phone("06 12 34 56 78", "FR") == "33612345678"
    assert ic.normalize_phone("77 123 45 67", "SN") == "221771234567"
    assert ic.normalize_phone("123", "CI") is None and ic.normalize_phone("", "CI") is None
    assert ic.normalize_phone(None, "CI") is None
    assert ic.normalize_phone("+1234567890123456", "CI") is None


def test_parsers():
    assert ic.parse_branch("AUTO") == "AUTO" and ic.parse_branch("Auto") == "AUTO"
    assert ic.parse_branch("Assurance santé") == "SANTE" and ic.parse_branch("Mutuelle famille") == "SANTE"
    assert ic.parse_branch("RC Pro") == "RC_PRO" and ic.parse_branch("Flotte automobile") == "FLOTTE"
    assert ic.parse_branch("Multirisque professionnelle") == "MULTIRISQUE_PRO"
    assert ic.parse_branch("moto") == "MOTO" and ic.parse_branch("Obsèques") == "VIE_PREVOYANCE"
    assert ic.parse_branch("bijoux") is None and ic.parse_branch("") is None
    assert ic.parse_term("annuel") == "ANNUEL" and ic.parse_term("12 mois") == "ANNUEL" and ic.parse_term("Mensuelle") == "MENSUEL"
    assert ic.parse_term("") is None
    with pytest.raises(ic.ContractError):
        ic.parse_term("biennal")
    assert ic.parse_day("31/12/2026", "x") == date(2026, 12, 31) and ic.parse_day("2026-12-31", "x") == date(2026, 12, 31)
    assert ic.parse_day("1.2.2027", "x") == date(2027, 2, 1)
    for bad in ("31/02/2026", "demain", "01/01/1990"):
        with pytest.raises(ic.ContractError):
            ic.parse_day(bad, "Échéance")
    with pytest.raises(ic.ContractError, match="obligatoire"):
        ic.parse_day("", "Échéance", required=True)
    assert ic.parse_premium("85 000") == Decimal("85000.00") and ic.parse_premium("85000 FCFA") == Decimal("85000.00")
    assert ic.parse_premium("1.500.000") == Decimal("1500000.00") and ic.parse_premium("1 250,50") == Decimal("1250.50")
    assert ic.parse_premium("1.250,50") == Decimal("1250.50") and ic.parse_premium("") is None
    assert ic.parse_premium("12.500") == Decimal("12500.00") and ic.parse_premium("12.5") == Decimal("12.50")
    with pytest.raises(ic.ContractError):
        ic.parse_premium("abc")


# --- Registre : API ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_contract_finds_or_creates_the_customer(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    known = await _customer(db_session, tenant, first="Awa", number="2250700000001")
    response = await client.post("/api/v1/contracts", headers=headers, json={
        "phone": "07 00 00 00 01", "first_name": "Autre", "email": "awa@exemple.ci", "branch": "Auto",
        "insurer": "SUNU", "policy_number": "POL-1", "premium": "85 000", "expires_on": "31/12/2026", "term": "annuel"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_id"] == str(known.id) and body["customer"].startswith("Awa")  # nom connu jamais remplacé
    assert body["premium"] == 85000 and body["currency"] == "XOF" and body["premium_visible"]
    assert body["status_label"] == "En cours" and body["term_label"] == "Annuel" and body["expires_on"] == "2026-12-31"
    await db_session.refresh(known)
    assert known.email == "awa@exemple.ci" and known.email_source == "MANUAL"

    new = await client.post("/api/v1/contracts", headers=headers, json={
        "phone": "+225 05 11 22 33 44", "first_name": "Koffi", "last_name": "Yao", "branch": "SANTE",
        "expires_on": "2027-03-01"})
    assert new.status_code == 201
    created = await db_session.get(Customer, uuid.UUID(new.json()["customer_id"]))
    assert created.whatsapp_number == "2250511223344" and created.first_name == "Koffi"
    assert created.acquisition_source == "IMPORT" and created.marketing_consent is False

    again = await client.post("/api/v1/contracts", headers=headers, json={
        "phone": "0700000001", "email": "autre@exemple.ci", "branch": "SANTE", "expires_on": "2027-01-01"})
    assert again.status_code == 201 and again.json()["email"] == "awa@exemple.ci"  # un email connu n'est jamais remplacé

    for payload, message in (
        ({"phone": "12", "branch": "AUTO", "expires_on": "2027-01-01"}, "Téléphone"),
        ({"phone": "0700000001", "branch": "bijoux", "expires_on": "2027-01-01"}, "Branche inconnue"),
        ({"phone": "0700000001", "branch": "AUTO"}, "Échéance"),
        ({"phone": "0700000001", "branch": "AUTO", "expires_on": "2027-01-01", "effective_on": "2027-02-01"}, "précéder"),
        ({"phone": "0700000001", "branch": "AUTO", "expires_on": "2027-01-01", "email": "pas-un-email"}, "Email invalide"),
    ):
        refused = await client.post("/api/v1/contracts", headers=headers, json=payload)
        assert refused.status_code == 422 and message in refused.json()["detail"], refused.text
    assert (await db_session.execute(select(func.count(InsuranceContract.id)))).scalar_one() == 3


@pytest.mark.asyncio
async def test_premium_is_for_admins_only(client, db_session):
    tenant = await _cabinet(db_session)
    owner = await _headers(client, tenant)
    manager = await _login(client, await _user(db_session, tenant, Role.MANAGER))
    agent = await _login(client, await _user(db_session, tenant, Role.AGENT))

    made = await client.post("/api/v1/contracts", headers=manager, json={
        "phone": "0700000002", "branch": "AUTO", "expires_on": "2027-01-01", "premium": "50000"})
    assert made.status_code == 201 and "premium" not in made.json() and not made.json()["premium_visible"]
    contract = await db_session.get(InsuranceContract, uuid.UUID(made.json()["id"]))
    assert contract.premium is None  # un manager ne saisit pas la prime

    await client.patch(f"/api/v1/contracts/{contract.id}", headers=owner, json={"premium": "120000"})
    await client.patch(f"/api/v1/contracts/{contract.id}", headers=manager, json={"premium": "1", "insurer": "NSIA"})
    await db_session.refresh(contract)
    assert contract.premium == Decimal("120000.00") and contract.insurer == "NSIA"

    listing = (await client.get("/api/v1/contracts", headers=agent)).json()
    assert listing["contracts"] and "premium" not in listing["contracts"][0] and listing["premium_visible"] is False
    assert (await client.get("/api/v1/contracts", headers=owner)).json()["contracts"][0]["premium"] == 120000
    assert (await client.post("/api/v1/contracts", headers=agent, json={"phone": "0700000003", "branch": "AUTO",
                                                                         "expires_on": "2027-01-01"})).status_code == 403
    assert (await client.put("/api/v1/contracts/settings", headers=manager,
                             json={"renewal_reminders_enabled": False})).status_code == 403
    assert (await client.post(f"/api/v1/contracts/{contract.id}/handle", headers=agent)).status_code == 200


@pytest.mark.asyncio
async def test_contracts_are_insurance_only_and_tenant_scoped(client, db_session):
    tenant = await _cabinet(db_session)
    other = await _cabinet(db_session)
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    customer = await _customer(db_session, tenant)
    contract = await _contract(db_session, tenant, customer, 20)
    other_headers = await _headers(client, other)
    assert (await client.get("/api/v1/contracts", headers=other_headers)).json()["contracts"] == []
    for method, url in (("patch", f"/api/v1/contracts/{contract.id}"), ("post", f"/api/v1/contracts/{contract.id}/handle"),
                        ("post", f"/api/v1/contracts/{contract.id}/renew"), ("post", f"/api/v1/contracts/{contract.id}/status")):
        kwargs = {"json": {"status": "LOST"}} if url.endswith("status") else {"json": {}}
        assert (await getattr(client, method)(url, headers=other_headers, **kwargs)).status_code == 404
    assert (await client.get("/api/v1/contracts", headers=await _headers(client, dealer))).status_code == 403
    # Un client d'une autre boutique ne peut pas être rattaché
    refused = await client.post("/api/v1/contracts", headers=other_headers, json={
        "customer_id": str(customer.id), "branch": "AUTO", "expires_on": "2027-01-01"})
    assert refused.status_code == 422 and "Client introuvable" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_views_summary_and_search(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    awa = await _customer(db_session, tenant, first="Awa")
    koffi = await _customer(db_session, tenant, first="Koffi")
    await _contract(db_session, tenant, awa, 10)
    await _contract(db_session, tenant, awa, 50, branch="SANTE", insurer="Allianz")
    await _contract(db_session, tenant, koffi, 80)
    await _contract(db_session, tenant, koffi, 200)
    await _contract(db_session, tenant, koffi, 20, status="LOST")
    await _contract(db_session, tenant, koffi, 5, renewal_handled_at=NOW)

    def get(**params):
        return client.get("/api/v1/contracts", headers=headers, params=params)

    all_ = (await get()).json()
    assert len(all_["contracts"]) == 5 and all_["summary"] == {"active": 5, "due30": 2, "due60": 3, "due90": 4, "todo": 1}
    assert [c["days_left"] for c in all_["contracts"]] == sorted(c["days_left"] for c in all_["contracts"])
    assert len((await get(view="due30")).json()["contracts"]) == 2
    todo = (await get(view="todo")).json()["contracts"]
    assert len(todo) == 1 and todo[0]["days_left"] == 10 and todo[0]["to_handle"]
    assert [c["status"] for c in (await get(view="closed")).json()["contracts"]] == ["LOST"]
    assert len((await get(branch="SANTE")).json()["contracts"]) == 1
    assert [c["insurer"] for c in (await get(q="allianz")).json()["contracts"]] == ["Allianz"]
    assert len((await get(q="koffi")).json()["contracts"]) == 3
    assert len((await get(q=awa.whatsapp_number[-6:])).json()["contracts"]) == 2
    assert (await get(view="xyz")).status_code == 422


@pytest.mark.asyncio
async def test_renew_status_and_handle(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    customer = await _customer(db_session, tenant)
    contract = await _contract(db_session, tenant, customer, 20, policy_number="POL-9", premium=Decimal("90000"),
                               currency="XOF", broker_alerted_at=NOW, client_reminded_at=NOW, client_reminder_channel="EMAIL")
    renewed = await client.post(f"/api/v1/contracts/{contract.id}/renew", headers=headers, json={})
    assert renewed.status_code == 201, renewed.text
    successor = renewed.json()
    assert successor["expires_on"] == (TODAY + timedelta(days=20)).replace(year=TODAY.year + 1).isoformat()
    assert successor["effective_on"] == (TODAY + timedelta(days=20)).isoformat() and successor["premium"] == 90000
    assert successor["policy_number"] == "POL-9" and successor["renewed_from_id"] == str(contract.id)
    assert successor["reminder"] is None and not successor["alert"]
    await db_session.refresh(contract)
    assert contract.status == "RENEWED"
    again = await client.post(f"/api/v1/contracts/{contract.id}/renew", headers=headers, json={})
    assert again.status_code == 422 and "en cours" in again.json()["detail"]
    assert (await client.post(f"/api/v1/contracts/{contract.id}/status", headers=headers,
                              json={"status": "ACTIVE"})).status_code == 409

    # Durée inconnue : la nouvelle échéance est demandée ; une échéance antérieure est refusée
    other = await _contract(db_session, tenant, customer, 10, term=None)
    missing = await client.post(f"/api/v1/contracts/{other.id}/renew", headers=headers, json={})
    assert missing.status_code == 422 and "nouvelle échéance" in missing.json()["detail"]
    earlier = await client.post(f"/api/v1/contracts/{other.id}/renew", headers=headers,
                                json={"expires_on": TODAY.isoformat()})
    assert earlier.status_code == 422
    same = await client.post(f"/api/v1/contracts/{other.id}/renew", headers=headers,
                             json={"expires_on": other.expires_on.isoformat()})
    assert same.status_code == 422 and "après l'échéance actuelle" in same.json()["detail"]
    ok = await client.post(f"/api/v1/contracts/{other.id}/renew", headers=headers,
                           json={"expires_on": "2027-06-30", "insurer": "NSIA"})
    assert ok.status_code == 201 and ok.json()["insurer"] == "NSIA" and ok.json()["expires_on"] == "2027-06-30"

    third = await _contract(db_session, tenant, customer, 15)
    lost = await client.post(f"/api/v1/contracts/{third.id}/status", headers=headers, json={"status": "LOST"})
    assert lost.json()["status_label"] == "Perdu"
    assert (await client.post(f"/api/v1/contracts/{third.id}/status", headers=headers,
                              json={"status": "RENEWED"})).status_code == 409  # Renouveler crée la suite
    assert (await client.post(f"/api/v1/contracts/{third.id}/handle", headers=headers)).status_code == 409

    fourth = await _contract(db_session, tenant, customer, 15)
    handled = (await client.post(f"/api/v1/contracts/{fourth.id}/handle", headers=headers)).json()
    assert handled["renewal_handled_at"] and not handled["to_handle"]
    # Changer l'échéance repart à zéro : l'alerte et le rappel redeviennent possibles
    moved = (await client.patch(f"/api/v1/contracts/{fourth.id}", headers=headers, json={"expires_on": "2027-10-01"})).json()
    assert moved["renewal_handled_at"] is None


@pytest.mark.asyncio
async def test_renewals_task_count(db_session):
    from app.services.notifications import KINDS, TITLES, task_counts

    tenant = await _cabinet(db_session)
    customer = await _customer(db_session, tenant)
    await _contract(db_session, tenant, customer, 30)
    await _contract(db_session, tenant, customer, -10)
    await _contract(db_session, tenant, customer, 60)
    await _contract(db_session, tenant, customer, 10, term="MENSUEL")
    await _contract(db_session, tenant, customer, 20, term="MENSUEL")  # mensuel : fenêtre de 15 jours
    await _contract(db_session, tenant, customer, 5, renewal_handled_at=NOW)
    assert "renewals" in KINDS and TITLES["renewals"] == "Échéance de contrat à préparer"
    assert (await task_counts(db_session, tenant, NOW))["renewals"] == 3
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    assert (await task_counts(db_session, dealer, NOW))["renewals"] == 0


# --- Import CSV --------------------------------------------------------------------------------------------

CSV_FR = (
    "Téléphone;Prénom;Nom;Email;Branche;Assureur;N° de contrat;Prime;Devise;Date d'effet;Échéance;Durée;Colonne libre\n"
    "07 00 00 00 11;Awa;Koné;awa@exemple.ci;Auto;SUNU;POL-1;85 000;XOF;01/01/2026;31/12/2026;annuel;x\n"
    "+225 05 00 00 00 12;Koffi;;;Santé;Allianz;;;;;15/11/2026;semestriel;\n"
    "07 00 00 00 13;;;;bijoux;;;;;;15/11/2026;;\n"
    "07 00 00 00 14;;;;Habitation;;;;;;31/02/2026;;\n"
    "12;;;;Auto;;;;;;15/11/2026;;\n"
    ";;;;;;;;;;;;\n"
)


@pytest.mark.asyncio
async def test_csv_import(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    files = {"file": ("contrats.csv", CSV_FR.encode("utf-8-sig"), "text/csv")}
    report = (await client.post("/api/v1/contracts/import-csv", headers=headers, files=files)).json()
    assert report["created"] == 2 and report["customers_created"] == 2 and report["ignored_columns"] == ["Colonne libre"]
    assert [e["line"] for e in report["errors"]] == [4, 5, 6]
    assert "Branche inconnue" in report["errors"][0]["message"] and "date invalide" in report["errors"][1]["message"]
    assert "téléphone" in report["errors"][2]["message"]
    rows = (await db_session.execute(select(InsuranceContract).order_by(InsuranceContract.expires_on))).scalars().all()
    assert [(r.branch, r.term, r.expires_on) for r in rows] == [("SANTE", "SEMESTRIEL", date(2026, 11, 15)),
                                                                ("AUTO", "ANNUEL", date(2026, 12, 31))]
    assert rows[1].premium == Decimal("85000.00") and rows[1].effective_on == date(2026, 1, 1)
    # Une ligne refusée ne laisse aucun client derrière elle
    assert (await db_session.execute(select(func.count(Customer.id)))).scalar_one() == 2

    # Réimport : le numéro de contrat met la ligne à jour ; sans numéro, la même ligne n'est pas dupliquée
    again = CSV_FR.replace("SUNU", "NSIA")
    report = (await client.post("/api/v1/contracts/import-csv", headers=headers,
                                files={"file": ("c.csv", again.encode("utf-8"), "text/csv")})).json()
    assert report["created"] == 0 and report["updated"] == 1 and report["skipped"] == 1 and report["customers_created"] == 0
    assert (await db_session.execute(select(func.count(InsuranceContract.id)))).scalar_one() == 2
    await db_session.refresh(rows[1])
    assert rows[1].insurer == "NSIA"

    # Un numéro de contrat déjà attribué à un autre client est refusé
    clash = "telephone,branche,numero,echeance\n0700000099,Auto,POL-1,2026-12-31\n"
    report = (await client.post("/api/v1/contracts/import-csv", headers=headers,
                                files={"file": ("c.csv", clash.encode(), "text/csv")})).json()
    assert report["created"] == 0 and "autre client" in report["errors"][0]["message"]


@pytest.mark.asyncio
async def test_csv_import_guards(client, db_session):
    tenant = await _cabinet(db_session)
    manager = await _login(client, await _user(db_session, tenant, Role.MANAGER))
    report = (await client.post("/api/v1/contracts/import-csv", headers=manager, files={
        "file": ("c.csv", "telephone;branche;prime;echeance\n0700000001;Auto;50000;2027-01-01\n".encode(), "text/csv")})).json()
    assert report["created"] == 1 and report["premium_ignored"] is True
    assert (await db_session.execute(select(InsuranceContract.premium))).scalar_one() is None
    missing = await client.post("/api/v1/contracts/import-csv", headers=manager, files={
        "file": ("c.csv", b"nom;branche\nAwa;Auto\n", "text/csv")})
    assert missing.status_code == 422 and "téléphone, branche et échéance" in missing.json()["detail"]
    assert (await client.post("/api/v1/contracts/import-csv", headers=manager,
                              files={"file": ("c.xlsx", b"x", "text/csv")})).status_code == 400
    latin = "telephone;branche;echeance;note\n0700000002;Santé;2027-01-01;déjà client\n".encode("cp1252")
    report = (await client.post("/api/v1/contracts/import-csv", headers=manager,
                                files={"file": ("c.csv", latin, "text/csv")})).json()
    assert report["created"] == 1
    template = await client.get("/api/v1/contracts/template.csv", headers=manager)
    assert template.status_code == 200 and "echeance" in template.text and "VOTRE_EMAIL_ICI" in template.text


# --- Rappels -----------------------------------------------------------------------------------------------

def _recorder():
    sent = {"email": [], "whatsapp": []}

    def send_email(**mail):
        sent["email"].append(mail)
        return True

    async def send_whatsapp(db, tenant, conversation, customer, text):
        sent["whatsapp"].append({"to": customer.whatsapp_number, "text": text})
        return True

    return sent, send_email, send_whatsapp


async def _wrote_recently(db, tenant, customer, hours=2):
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                   content="Bonjour", created_at=NOW - timedelta(hours=hours)))
    await db.commit()
    return conversation


@pytest.mark.asyncio
async def test_daily_digest_to_the_cabinet(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa")
    due = await _contract(db_session, tenant, awa, 40, policy_number="POL-7", premium=Decimal("85000"))
    far = await _contract(db_session, tenant, awa, 46)
    sent, send_email, send_whatsapp = _recorder()

    early = await ic.run_for_tenant(db_session, tenant, NOW.replace(hour=7), send_email, send_whatsapp)
    assert early["digest"] == 0 and sent["email"] == []  # avant 8 h, heure du pays
    report = await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert report["digest"] == 1 and len(sent["email"]) == 1
    mail = sent["email"][0]
    assert mail["to"] == tenant.email and mail["subject"] == "1 échéance de contrat à préparer — Cabinet Kouassi"
    assert "Awa" in mail["body"] and "Assurance automobile, SUNU Assurances, n° POL-7" in mail["body"]
    assert "dans 40 jours" in mail["body"] and "/dashboard/?tab=contracts" in mail["body"]
    assert "85000" not in mail["body"] and "85 000" not in mail["body"]  # jamais la prime, même au cabinet par email
    await db_session.refresh(due)
    await db_session.refresh(far)
    assert due.broker_alerted_at is not None and far.broker_alerted_at is None

    # Un nouveau contrat le même jour : pas de second email avant demain ; demain, seulement le nouveau
    await _contract(db_session, tenant, awa, 30)
    await ic.run_for_tenant(db_session, tenant, NOW + timedelta(hours=2), send_email, send_whatsapp)
    assert len(sent["email"]) == 1
    tomorrow = NOW + timedelta(days=1)
    await ic.run_for_tenant(db_session, tenant, tomorrow, send_email, send_whatsapp)
    digests = [m for m in sent["email"] if m["to"] == tenant.email]
    assert len(digests) == 2 and digests[1]["subject"].startswith("2 échéances")  # le nouveau + « far » entré à J-45


@pytest.mark.asyncio
async def test_digest_failure_is_retried(db_session):
    tenant = await _cabinet(db_session)
    await _contract(db_session, tenant, await _customer(db_session, tenant), 40)
    report = await ic.run_for_tenant(db_session, tenant, NOW, lambda **m: False, None)
    assert report["digest"] == 0 and report["failed"] == 1 and tenant.renewal_digest_on is None
    sent, send_email, _ = _recorder()
    assert (await ic.run_for_tenant(db_session, tenant, NOW + timedelta(hours=1), send_email, None))["digest"] == 1


@pytest.mark.asyncio
async def test_client_reminder_whatsapp_when_window_open(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa", email="awa@exemple.ci")
    await _wrote_recently(db_session, tenant, awa)
    contract = await _contract(db_session, tenant, awa, 30, policy_number="POL-1", premium=Decimal("85000"))
    sent, send_email, send_whatsapp = _recorder()
    report = await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert report["whatsapp"] == 1 and report["email"] == 0
    text = sent["whatsapp"][0]["text"]
    assert text == ("Bonjour Awa, ici Cabinet Kouassi. Votre contrat d'assurance automobile (SUNU Assurances) arrive à "
                    "échéance le dimanche 8 novembre 2026. Souhaitez-vous que votre conseiller étudie son "
                    "renouvellement avec vous ? Répondez simplement à ce message.")
    assert "85" not in text and "POL-1" not in text and not insurance.contains_amount(text)
    await db_session.refresh(contract)
    assert contract.client_reminder_channel == "WHATSAPP" and contract.client_reminded_at is not None
    # Une seule fois
    await ic.run_for_tenant(db_session, tenant, NOW + timedelta(hours=1), send_email, send_whatsapp)
    assert len(sent["whatsapp"]) == 1


@pytest.mark.asyncio
async def test_client_reminder_email_then_call(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa", email="awa@exemple.ci")
    conv = await _wrote_recently(db_session, tenant, awa, hours=30)  # fenêtre de 20 h fermée
    by_email = await _contract(db_session, tenant, awa, 25, branch="RC_PRO", policy_number="POL-2", premium=Decimal("99000"))
    koffi = await _customer(db_session, tenant, first="Koffi")
    by_call = await _contract(db_session, tenant, koffi, 25)
    gone = await _customer(db_session, tenant, first="Aya", email="aya@exemple.ci")
    gone.marketing_consent_withdrawn_at = NOW - timedelta(days=3)  # désinscrite par le lien
    await db_session.commit()
    withdrawn = await _contract(db_session, tenant, gone, 25)
    sent, send_email, send_whatsapp = _recorder()

    report = await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert report["whatsapp"] == 0 and report["email"] == 1 and report["call"] == 2 and sent["whatsapp"] == []
    mail = next(m for m in sent["email"] if m["to"] == "awa@exemple.ci")
    assert mail["subject"] == "Cabinet Kouassi : votre contrat de responsabilité civile professionnelle arrive à échéance le 03/11/2026"
    assert "Bonjour Awa," in mail["body"] and "mardi 3 novembre 2026" in mail["body"]
    assert "vous êtes client de Cabinet Kouassi" in mail["body"] and "/unsubscribe/" in mail["body"]
    assert "accepté de recevoir les offres" not in mail["body"]  # message de service, pas une offre
    assert "99000" not in mail["body"] and "99 000" not in mail["body"] and "POL-2" not in mail["body"] and "List-Unsubscribe" in mail["extra_headers"]
    assert mail["reply_to"] == tenant.email and mail["from_name"] == tenant.name
    for contract, channel in ((by_email, "EMAIL"), (by_call, "CALL"), (withdrawn, "CALL")):
        await db_session.refresh(contract)
        assert contract.client_reminder_channel == channel
    note = (await db_session.execute(select(Message).where(Message.conversation_id == conv.id,
                                                           Message.message_type == "renewal_email"))).scalar_one()
    assert "Rappel d'échéance envoyé par email" in note.content
    listed = ic.serialize(by_call, koffi, TODAY, False)
    assert listed["reminder"].startswith("À appeler") and listed["to_handle"]


@pytest.mark.asyncio
async def test_failed_whatsapp_falls_back_to_email_and_failed_email_is_retried(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, email="awa@exemple.ci")
    await _wrote_recently(db_session, tenant, awa)
    contract = await _contract(db_session, tenant, awa, 20)
    emails = []

    async def refused(*args):
        return False

    report = await ic.run_for_tenant(db_session, tenant, NOW,
                                     lambda **m: emails.append(m) or m["to"] != "awa@exemple.ci", refused)
    await db_session.refresh(contract)
    assert report["failed"] == 1 and contract.client_reminded_at is None  # email refusé : nouvel essai plus tard
    await ic.run_for_tenant(db_session, tenant, NOW + timedelta(hours=1), lambda **m: emails.append(m) or True, refused)
    await db_session.refresh(contract)
    assert contract.client_reminder_channel == "EMAIL"


@pytest.mark.asyncio
async def test_no_reminder_when_not_due_handled_disabled_paused_demo_or_night(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, email="awa@exemple.ci")
    early = await _contract(db_session, tenant, awa, 31)
    handled = await _contract(db_session, tenant, awa, 20, renewal_handled_at=NOW)
    monthly = await _contract(db_session, tenant, awa, 8, term="MENSUEL")
    sent, send_email, send_whatsapp = _recorder()

    await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    assert [m for m in sent["email"] if m["to"] == "awa@exemple.ci"] == []
    for contract in (early, handled, monthly):
        await db_session.refresh(contract)
        assert contract.client_reminded_at is None

    due = await _contract(db_session, tenant, awa, 10)
    for hour in (8, 20, 21):  # rappels aux clients entre 9 h et 20 h, heure du pays
        night = await ic.run_for_tenant(db_session, tenant, NOW.replace(hour=hour), send_email, send_whatsapp)
        assert night["email"] == 0 and night["call"] == 0
    tenant.renewal_reminders_enabled = False
    await db_session.commit()
    await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp)
    await db_session.refresh(due)
    assert due.client_reminded_at is None

    tenant.renewal_reminders_enabled, tenant.active = True, False  # boutique suspendue (lot 51)
    await db_session.commit()
    assert await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp) == {
        "digest": 0, "whatsapp": 0, "email": 0, "call": 0, "failed": 0}
    tenant.active, tenant.paid_until = True, TODAY - timedelta(days=5)  # abonnement non renouvelé
    await db_session.commit()
    assert (await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    tenant.paid_until, tenant.is_demo = None, True
    await db_session.commit()
    assert (await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 0
    tenant.is_demo = False
    await db_session.commit()
    assert (await ic.run_for_tenant(db_session, tenant, NOW, send_email, send_whatsapp))["email"] == 1


@pytest.mark.asyncio
async def test_worker_only_runs_insurance_tenants(db_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.workers.celery_app import TASK_MODULES, celery_app
    from app.workers.renewals import check_renewals

    assert "app.workers.renewals" in TASK_MODULES
    assert celery_app.conf.beat_schedule["contract-renewals"]["task"] == "app.workers.renewals.check_renewals_task"
    tenant = await _cabinet(db_session)
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    await _contract(db_session, tenant, await _customer(db_session, tenant, email="a@exemple.ci"), 20)
    await _contract(db_session, dealer, await _customer(db_session, dealer, email="b@exemple.ci"), 20)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, class_=AsyncSession)
    sent, send_email, send_whatsapp = _recorder()
    report = await check_renewals(factory, NOW, send_email, send_whatsapp)
    assert report["digest"] == 1 and report["email"] == 1
    assert {m["to"] for m in sent["email"]} == {tenant.email, "a@exemple.ci"}


# --- Ce que Bob sait ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bob_knows_the_expiry_never_the_premium(db_session):
    from app.agents.prompts import build_system_prompt
    from app.services.customer_memory_service import build_customer_memory

    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant)
    await _contract(db_session, tenant, awa, 30, policy_number="POL-123456", premium=Decimal("85000"),
                    client_reminded_at=NOW, client_reminder_channel="WHATSAPP", note="client difficile")
    await _contract(db_session, tenant, awa, 10, status="LOST", insurer="Perdu SA")
    memory = await build_customer_memory(db_session, tenant.id, awa.id)
    assert "CONTRATS DU CLIENT AU CABINET" in memory and "Assurance automobile chez SUNU Assurances" in memory
    assert "échéance le dimanche 8 novembre 2026" in memory and "rappel d'échéance envoyé au client par WhatsApp" in memory
    for secret in ("85000", "85 000", "POL-123456", "client difficile", "Perdu SA"):
        assert secret not in memory
    assert "ne donne jamais de montant" in memory
    prompt = build_system_prompt(tenant, [], memory)
    assert "POL-123456" not in prompt and "85000" not in prompt
    # Commerce et concession : rien de nouveau
    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    customer = await _customer(db_session, dealer)
    assert "CONTRATS" not in await build_customer_memory(db_session, dealer.id, customer.id)


# --- Score (lot 54) ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registered_expiry_makes_the_client_hot(db_session):
    tenant = await _cabinet(db_session)
    portfolio = await _customer(db_session, tenant, first="Awa")
    await _contract(db_session, tenant, portfolio, 20)
    far = await _customer(db_session, tenant, first="Loin")
    await _contract(db_session, tenant, far, 100)
    monthly = await _customer(db_session, tenant, first="Mensuel")
    await _contract(db_session, tenant, monthly, 20, term="MENSUEL")  # fenêtre « Chaud » de 15 jours
    lost = await _customer(db_session, tenant, first="Perdu")
    await _contract(db_session, tenant, lost, 10, status="LOST")
    sold = await _customer(db_session, tenant, first="Souscrit")
    conversation = Conversation(tenant_id=tenant.id, customer_id=sold.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, customer_id=sold.id, conversation_id=conversation.id,
                                      kind="APPEL", status="CONFIRMED", availability="lundi",
                                      outcome="SOLD", outcome_at=NOW - timedelta(days=300)))
    await db_session.commit()
    await _contract(db_session, tenant, sold, 50)

    prospects = await insurance_prospect.for_customers(db_session, tenant, None, NOW)
    assert set(prospects) == {portfolio.id, sold.id}
    assert prospects[portfolio.id]["score"] == "CHAUD" and prospects[portfolio.id]["renewal"]
    assert prospects[portfolio.id]["reasons"] == ["renouvellement : échéance dans 20 jours (contrat annuel)"]
    assert prospects[sold.id]["score"] == "CHAUD"  # déjà souscrit, mais son contrat est à renouveler
    assert prospects[portfolio.id]["contract_branch"] == "Assurance automobile"


@pytest.mark.asyncio
async def test_quote_statuses_in_the_score(db_session):
    tenant = await _cabinet(db_session)
    customer = await _customer(db_session, tenant)
    request = QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, branch="SANTE", client_type="PARTICULIER",
                           details={}, status="PROPOSAL_SENT")
    db_session.add(request)
    await db_session.commit()
    assert (await insurance_prospect.for_customers(db_session, tenant, None, NOW))[customer.id]["score"] == "TIEDE"
    request.status = "WON"
    await db_session.commit()
    assert (await insurance_prospect.for_customers(db_session, tenant, None, NOW))[customer.id]["score"] == "VENDU"
    request.status = "LOST"
    await db_session.commit()
    assert (await insurance_prospect.for_customers(db_session, tenant, None, NOW))[customer.id]["score"] == "FROID"


# --- Suivi des demandes de cotation ------------------------------------------------------------------------

async def _submitted(db, tenant, customer):
    request = QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, branch="AUTO", client_type="PARTICULIER",
                           details={"vehicle": "Corolla"}, status="SUBMITTED", consent_at=NOW, submitted_at=NOW)
    db.add(request)
    await db.commit()
    return request


def test_transitions():
    request = QuoteRequest(status="SUBMITTED")
    insurance.advance(request, "PROPOSAL_SENT", "u", NOW)
    assert request.handled_at == NOW and request.proposal_sent_at == NOW and request.status == "PROPOSAL_SENT"
    with pytest.raises(insurance.TransitionError, match="Proposition envoyée"):
        insurance.advance(request, "HANDLED", "u", NOW)
    insurance.advance(request, "LOST", "u", NOW, "  trop   cher  " + "x" * 200)
    assert request.lost_reason.startswith("trop cher") and len(request.lost_reason) == insurance.LOST_REASON_MAX
    assert insurance.trace_lines(request)[-1].startswith("Perdu : ")
    with pytest.raises(insurance.TransitionError):
        insurance.advance(request, "WON", "u", NOW)  # perdu : il faut d'abord rouvrir
    insurance.advance(request, "HANDLED", "u", NOW)  # rouvert
    assert request.lost_reason is None and request.closed_at is None and request.status == "HANDLED"
    insurance.advance(request, "WON", "u", NOW)
    assert insurance.trace_lines(request)[-1].startswith("Souscrit : ")
    with pytest.raises(insurance.TransitionError):
        insurance.advance(request, "LOST", "u", NOW)  # souscrit : terminé
    draft = QuoteRequest(status="DRAFT")
    with pytest.raises(insurance.TransitionError):
        insurance.advance(draft, "WON", "u", NOW)  # encore avec Bob
    assert insurance.STATUS_LABELS["SUBMITTED"] == "Reçue" and insurance.STATUS_LABELS["HANDLED"] == "En cotation"


@pytest.mark.asyncio
async def test_quote_follow_up_api(client, db_session):
    tenant = await _cabinet(db_session)
    headers = await _headers(client, tenant)
    customer = await _customer(db_session, tenant)
    request = await _submitted(db_session, tenant, customer)
    url = f"/api/v1/quote-requests/{request.id}/status"

    sent = (await client.post(url, headers=headers, json={"status": "PROPOSAL_SENT"})).json()
    assert sent["status_label"] == "Proposition envoyée" and sent["next_statuses"] == ["WON", "LOST"]
    assert any(line.startswith("Proposition envoyée : ") for line in sent["trace"])
    proposal = (await client.get("/api/v1/quote-requests?view=proposal", headers=headers)).json()
    assert [q["id"] for q in proposal] == [str(request.id)]
    no_reason = await client.post(url, headers=headers, json={"status": "LOST", "lost_reason": "  "})
    assert no_reason.status_code == 422
    assert (await client.post(url, headers=headers, json={"status": "DRAFT"})).status_code == 409
    won = (await client.post(url, headers=headers, json={"status": "WON"})).json()
    assert won["status_label"] == "Souscrit" and won["prospect"]["score"] == "VENDU" and won["customer_id"] == str(customer.id)
    closed = (await client.get("/api/v1/quote-requests?view=closed", headers=headers)).json()
    assert closed[0]["status"] == "WON"
    # Le contrat créé depuis la demande y reste rattaché
    made = await client.post("/api/v1/contracts", headers=headers, json={
        "customer_id": str(customer.id), "quote_request_id": str(request.id), "branch": "AUTO", "expires_on": "2027-10-09"})
    assert made.json()["quote_request_id"] == str(request.id)
    other = await _cabinet(db_session)
    assert (await client.post(url, headers=await _headers(client, other), json={"status": "LOST",
                                                                                "lost_reason": "x"})).status_code == 404
    viewer = await _login(client, await _user(db_session, tenant, Role.VIEWER))
    assert (await client.post(url, headers=viewer, json={"status": "LOST", "lost_reason": "x"})).status_code == 403


# --- Accueil -----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_shows_renewals(db_session):
    from app.services.home_service import home_summary

    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa")
    await _contract(db_session, tenant, awa, 12, client_reminded_at=NOW, client_reminder_channel="CALL")
    koffi = await _customer(db_session, tenant, first="Koffi")
    await _contract(db_session, tenant, koffi, 40, renewal_handled_at=NOW)
    request = await _submitted(db_session, tenant, koffi)
    insurance.advance(request, "WON", "u", NOW - timedelta(days=2))
    monthly = await _customer(db_session, tenant, first="Mensuel")
    await _contract(db_session, tenant, monthly, 20, term="MENSUEL")  # hors de la fenêtre de 15 jours
    sold = await _customer(db_session, tenant, first="Souscrit")
    conversation = Conversation(tenant_id=tenant.id, customer_id=sold.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(AppointmentRequest(tenant_id=tenant.id, customer_id=sold.id, conversation_id=conversation.id, kind="APPEL",
                                      status="CONFIRMED", availability="lundi", outcome="SOLD", outcome_at=NOW - timedelta(days=300)))
    await _contract(db_session, tenant, sold, 50)
    await db_session.commit()

    summary = await home_summary(db_session, tenant.id, now=NOW)
    renewals = [t for t in summary["todo"] if t["kind"] == "RENEWAL"]
    assert len(renewals) == 1 and renewals[0]["customer"].startswith("Awa") and renewals[0]["hot"]
    assert renewals[0]["reason"] == "Échéance dans 12 jours" and "à appeler" in renewals[0]["detail"]
    kinds = [t["kind"] for t in summary["todo"]]
    assert kinds.index("RENEWAL") > kinds.index("QUOTE") if "QUOTE" in kinds else True
    expiries = {e["customer_id"]: e for e in summary["expiries"]}
    assert expiries[str(awa.id)]["renewal"] and expiries[str(awa.id)]["branches"] == ["Assurance automobile"]
    assert expiries[str(koffi.id)]["renewal"]
    assert expiries[str(sold.id)]["renewal"]  # souscrit au rendez-vous, mais à renouveler : toujours montré
    assert str(monthly.id) not in {t.get("customer_id") for t in renewals} and len(renewals) == 1
    assert summary["kpis"]["sold"]["value"] == 1  # demande « Souscrit »


# --- Tableau de bord ---------------------------------------------------------------------------------------

def _function(name):
    body = HTML[HTML.index(f"function {name}("):]
    return body[:body.index("\n}\n")]


def test_dashboard_contracts_page():
    assert 'id="nav-contracts"' in HTML and 'id="tab-contracts"' in HTML
    nav = HTML[HTML.index('id="nav-contracts"') - 200:HTML.index('id="nav-contracts"') + 200]
    assert "insurance-only" in nav
    assert '"contracts"' in HTML[HTML.index("function showTab("):HTML.index("async function loadTenant(")]
    assert "contracts: [\"renewals\"]" in HTML
    render = _function("renderContracts")
    for piece in ("esc(c.customer)", "esc(c.branch_label)", "esc(c.insurer", "esc(c.reminder)", "esc(c.note)"):
        assert piece in render, piece
    assert "esc(c.when_label" in _function("contractWhenTag")
    assert "handleQuote(id)" in _function("advanceQuote")  # « Prise en charge » garde le chemin du lot 53
    assert "c.premium" in render and "premium_visible" in render
    for name in ("loadContracts", "openContractDialog", "saveContract", "renewContract", "handleContract",
                 "setContractStatus", "importContracts", "advanceQuote"):
        assert f"function {name}(" in HTML, name
    assert 'class="file-drop"' in HTML[HTML.index('id="tab-contracts"'):HTML.index('id="tab-contracts"') + 6000]
    assert "?tab=" in HTML or 'get("tab")' in HTML
    quotes = _function("loadQuotes")
    assert "proposal" in quotes and "closed" in quotes and "next_statuses" in quotes
    # Jamais de fenêtre du navigateur (lot 39) ; vouvoiement (lot 48)
    for js in (_function("advanceQuote"), _function("renewContract"), _function("setContractStatus")):
        assert "confirm(" not in js.replace("bobConfirm(", "") and "prompt(" not in js and "alert(" not in js.replace("bobAlert(", "")
