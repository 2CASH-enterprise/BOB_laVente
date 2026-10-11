"""
Lot 64 — fiche client du courtier : contrats (avec leurs actions), quatre chiffres, historique court (cotations,
rendez-vous et appels, réclamations et sinistres) menant au bon endroit. Prime : administrateurs seulement.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus
from app.models.insurance_complaint import InsuranceComplaint
from app.models.quote_request import QuoteRequest
from app.models.user import Role
from app.services import insurance_customer as icu
from app.services.business_type import ONLINE_STORE
from app.tests.test_lot53_courtier import _cabinet, _headers
from app.tests.test_lot55_contrats import NOW, TODAY, _contract, _customer, _login, _user

API = "/api/v1/contracts/customer"


async def _history(db, tenant, customer):
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    db.add_all([
        QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, branch="SANTE", client_type="PARTICULIER", status="HANDLED",
                     submitted_at=NOW - timedelta(days=3), details={}),
        QuoteRequest(tenant_id=tenant.id, customer_id=customer.id, branch="AUTO", client_type="PARTICULIER", status="WON",
                     submitted_at=NOW - timedelta(days=200), details={}),
        AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, kind="APPEL",
                           status="REQUESTED", availability="lundi"),
        AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, kind="CABINET",
                           status="CONFIRMED", availability="mardi", scheduled_at=NOW + timedelta(days=2)),
        AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, kind="APPEL",
                           status="CONFIRMED", availability="jeudi", scheduled_at=NOW - timedelta(days=20), outcome="SOLD"),
        AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, kind="CABINET",
                           status="CANCELLED", availability="vendredi", scheduled_at=NOW + timedelta(days=5)),
        InsuranceComplaint(tenant_id=tenant.id, customer_id=customer.id, reference="R-2026-0003", kind="SINISTRE",
                           channel="PHONE", subject="Accident", status="IN_PROGRESS", received_at=NOW - timedelta(days=30),
                           due_on=TODAY - timedelta(days=2), created_by="BOB"),
        InsuranceComplaint(tenant_id=tenant.id, customer_id=customer.id, reference="R-2026-0001", kind="RECLAMATION",
                           channel="WHATSAPP", subject="Retard", status="RESOLVED", received_at=NOW - timedelta(days=90),
                           created_by="BOB"),
    ])
    await db.commit()
    return conversation


@pytest.mark.asyncio
async def test_overview_contracts_summary_and_history(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa")
    other = await _customer(db_session, tenant, first="Koffi")
    far = await _contract(db_session, tenant, awa, 200, branch="HABITATION", premium=Decimal("50000"))
    soon = await _contract(db_session, tenant, awa, 20, policy_number="POL-1", premium=Decimal("85000"))
    old = await _contract(db_session, tenant, awa, -400, status="RENEWED")
    older = await _contract(db_session, tenant, awa, -800, status="RENEWED")
    expired = await _contract(db_session, tenant, awa, -60)  # en cours mais échu depuis longtemps : pas « prochaine »
    await _contract(db_session, tenant, other, 5)  # un autre client : jamais montré
    conversation = await _history(db_session, tenant, awa)

    data = await icu.overview(db_session, tenant, awa, can_see_premium=False, now=NOW)
    assert [c["id"] for c in data["contracts"]] == [str(expired.id), str(soon.id), str(far.id), str(old.id), str(older.id)]
    assert all("premium" not in c for c in data["contracts"])
    assert data["contracts"][1]["conversation_id"] == str(conversation.id)
    s = data["summary"]
    assert s["active"] == 3 and s["quotes"] == 2 and s["quotes_open"] == 1
    assert s["complaints_open"] == 1 and s["complaints_late"] == 1
    assert s["next_expiry"] == {"date": soon.expires_on.isoformat(), "when_label": "dans 20 jours",
                                "branch_label": "Assurance automobile", "alert": True}
    assert [(q["label"], q["status_label"], q["view"]) for q in data["quotes"]] == [
        ("Assurance santé", "En cotation", "handled"), ("Assurance automobile", "Souscrit", "closed")]
    labels = sorted((a["label"], a["status_label"], a["view"]) for a in data["appointments"])
    assert labels == [("Appel", "Passé · contrat souscrit", "closed"), ("Appel", "À confirmer", "pending"),
                      ("Rendez-vous au cabinet", "Annulé", "closed"), ("Rendez-vous au cabinet", "Prévu", "confirmed")]
    assert [(c["reference"], c["label"], c["overdue"], c["view"]) for c in data["complaints"]] == [
        ("R-2026-0003", "Sinistre", True, "progress"), ("R-2026-0001", "Réclamation", False, "resolved")]
    assert data["more"] == {"quotes": 0, "appointments": 0, "complaints": 0}
    admin = await icu.overview(db_session, tenant, awa, can_see_premium=True, now=NOW)
    assert admin["contracts"][1]["premium"] == 85000.0


@pytest.mark.asyncio
async def test_overview_empty_and_limits(db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant)
    data = await icu.overview(db_session, tenant, awa, can_see_premium=True, now=NOW)
    assert data["contracts"] == [] and data["summary"]["next_expiry"] is None and data["summary"]["active"] == 0
    for i in range(icu.HISTORY_LIMIT + 3):
        db_session.add(QuoteRequest(tenant_id=tenant.id, customer_id=awa.id, branch="AUTO", client_type="PARTICULIER",
                                    status="DRAFT", details={}))
    await db_session.commit()
    data = await icu.overview(db_session, tenant, awa, can_see_premium=True, now=NOW)
    assert len(data["quotes"]) == icu.HISTORY_LIMIT and data["more"]["quotes"] == 3 and data["summary"]["quotes_open"] == 0
    assert data["quotes"][0]["view"] == "draft"


@pytest.mark.asyncio
async def test_api_roles_and_isolation(client, db_session):
    tenant = await _cabinet(db_session)
    awa = await _customer(db_session, tenant, first="Awa")
    await _contract(db_session, tenant, awa, 20, premium=Decimal("85000"))
    owner = await _headers(client, tenant)
    r = await client.get(f"{API}/{awa.id}", headers=owner)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["premium_visible"] is True and data["contracts"][0]["premium"] == 85000.0
    assert data["branches"] and data["terms"] and data["statuses"] and data["currency"] == "XOF"
    agent = await _login(client, await _user(db_session, tenant, Role.AGENT))
    data = (await client.get(f"{API}/{awa.id}", headers=agent)).json()
    assert data["premium_visible"] is False and "premium" not in data["contracts"][0] and "85000" not in str(data)
    # Un client d'un autre cabinet, un identifiant inconnu, une boutique d'un autre secteur
    other = await _cabinet(db_session)
    stranger = await _customer(db_session, other)
    assert (await client.get(f"{API}/{stranger.id}", headers=owner)).status_code == 404
    assert (await client.get(f"{API}/00000000-0000-0000-0000-000000000000", headers=owner)).status_code == 404
    shop = await _cabinet(db_session, business_type=ONLINE_STORE)
    assert (await client.get(f"{API}/{awa.id}", headers=await _headers(client, shop))).status_code == 403
    # Ajouter un contrat depuis la fiche : le client est déjà choisi
    r = await client.post("/api/v1/contracts", headers=owner, json={"customer_id": str(awa.id), "branch": "SANTE",
                                                                     "expires_on": "2027-03-01", "term": "ANNUEL"})
    assert r.status_code == 201, r.text
    assert len((await client.get(f"{API}/{awa.id}", headers=owner)).json()["contracts"]) == 2


def test_dashboard_customer_card():
    html = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
    assert "function contractCardHtml(c, data, onCustomer)" in html and "contractCardHtml(ct, data, true)" in html
    assert "contractCardHtml(c, data, false)" in html  # la page « Contrats » garde la même carte
    assert 'id="customer-insurance-tiles"' in html and "Réclamations ouvertes" in html and "Prochaine échéance" in html
    assert "function goToItem(tab, view, id)" in html and html.count("focusPendingItem();") >= 3
    assert 'data-item-id="${esc(q.id)}"' in html and 'data-item-id="${esc(a.id)}"' in html and 'data-item-id="${esc(c.id)}"' in html
    assert "function refreshContractViews()" in html and html.count("refreshContractViews();") >= 4
    assert "openContractDialog(null, { customer_id: currentCustomerId, customer: currentCustomerName })" in html
    # Démarrage (lot 56) : les variables lues par showTab / openCustomer sont des var
    for name in ("currentCustomerId", "currentCustomerName", "pendingFocusId"):
        assert f"var {name} =" in html
