"""Lot 43 — fiche prospect concession : qualification, score à règles fixes, fiche au commercial."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.agents.prompts import build_system_prompt
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import prospect
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE, tools_for
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

NOW = datetime.now(timezone.utc)


async def _dealer(db, business_type=CAR_DEALERSHIP, pn=None, commercial_email=None):
    email = f"d{uuid.uuid4().hex[:6]}@example.com"
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if pn:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pn, system_user_token="t"))
    referred = None
    if commercial_email:
        referred = ContactPoint(tenant_id=tenant.id, name="Moussa", code=f"c{uuid.uuid4().hex[:6]}", greeting="Bonjour",
                                owner_email=commercial_email, owner_name="Moussa")
        db.add(referred)
        await db.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}", first_name="Mamadou",
                        last_name="Diop", city="Dakar", referred_contact_point_id=referred.id if referred else None)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.commit()
    return tenant, customer, conversation, email


def _fiche(**kw):
    base = {"has_appointment": False, "timeline": None, "budget": None, "need": None}
    return {**base, **kw}


# --- Score : règles fixes ----------------------------------------------------------------------------

@pytest.mark.parametrize("fiche, expected", [
    (_fiche(timeline="MOINS_1_MOIS", has_appointment=True), "CHAUD"),
    (_fiche(timeline="MOINS_1_MOIS", budget="15 M"), "CHAUD"),
    (_fiche(timeline="MOINS_1_MOIS"), "TIEDE"),                          # pressé mais rien d'autre
    (_fiche(has_appointment=True), "TIEDE"),
    (_fiche(timeline="UN_A_3_MOIS", budget="15 M"), "CHAUD"),           # 02/10 : 3 mois = chaud en Afrique
    (_fiche(timeline="UN_A_3_MOIS", has_appointment=True), "CHAUD"),
    (_fiche(timeline="UN_A_3_MOIS"), "TIEDE"),
    (_fiche(need="SUV", budget="15 M"), "TIEDE"),
    (_fiche(need="SUV"), "FROID"),
    (_fiche(timeline="PLUS_3_MOIS", budget="15 M"), "FROID"),
    (_fiche(), "FROID"),
    (_fiche(outcome="SOLD", timeline="MOINS_1_MOIS", budget="15 M"), "VENDU"),   # déjà client
    (_fiche(outcome="FOLLOW_UP"), "TIEDE"),                                      # venu, à relancer
    (_fiche(outcome="NOT_INTERESTED"), "FROID"),
    (_fiche(outcome="NO_SHOW"), "FROID"),
])
def test_score_rules(fiche, expected):
    level, reasons = prospect.score(fiche)
    assert level == expected and reasons


def test_score_explains_itself():
    level, reasons = prospect.score(_fiche(timeline="MOINS_1_MOIS", has_appointment=True, budget="15 M"))
    assert reasons == ["rendez-vous pris", "achat sous 1 mois", "budget connu"]
    assert prospect.score(_fiche())[1] == ["simple renseignement pour l'instant"]


# --- Outil de Bob ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bob_fills_the_prospect_profile(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    tools = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    r = await tools.execute("update_prospect_profile", {"need": "SUV familial", "condition": "OCCASION", "budget": "15 à 20 millions",
                                                        "payment": "FINANCEMENT", "timeline": "MOINS_1_MOIS",
                                                        "trade_in": "Peugeot 3008 2021"})
    assert r["status"] == "saved"
    p = await prospect.load(db_session, tenant.id, customer.id)
    assert (p.need, p.condition, p.budget, p.payment, p.timeline, p.trade_in) == (
        "SUV familial", "OCCASION", "15 à 20 millions", "FINANCEMENT", "MOINS_1_MOIS", "Peugeot 3008 2021")
    r = await tools.execute("update_prospect_profile", {"timeline": "DEMAIN", "budget": "   "})
    assert r["status"] == "partially_saved" and set(r["rejected"]) == {"timeline", "budget"}
    await db_session.refresh(p)
    assert p.timeline == "MOINS_1_MOIS" and p.budget == "15 à 20 millions"  # rien d'écrasé par une valeur invalide
    assert (await tools.execute("update_prospect_profile", {}))["error"]


@pytest.mark.asyncio
async def test_store_never_gets_the_tool(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session, ONLINE_STORE)
    tools = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    r = await tools.execute("update_prospect_profile", {"budget": "10"})
    assert "error" in r and await prospect.load(db_session, tenant.id, customer.id) is None
    assert "update_prospect_profile" not in {t["name"] for t in tools_for(ONLINE_STORE, TOOL_DEFINITIONS)}
    assert "update_prospect_profile" in {t["name"] for t in tools_for(CAR_DEALERSHIP, TOOL_DEFINITIONS)}


def test_dealership_prompt_asks_the_new_questions():
    prompt = build_system_prompt(Tenant(name="Auto Plus", country="SN", currency="XOF", business_type=CAR_DEALERSHIP))
    for words in ("neuf ou occasion", "comptant ou financement", "quand il compte acheter", "update_prospect_profile",
                  "une ou deux questions à la fois"):
        assert words in prompt


@pytest.mark.asyncio
async def test_appointment_details_also_go_into_the_profile(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    tools = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    r = await tools.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi matin", "need": "berline",
                                                    "budget": "10 M", "trade_in": "Clio 2016", "financing_interest": True})
    assert "error" not in r
    p = await prospect.load(db_session, tenant.id, customer.id)
    assert (p.need, p.budget, p.trade_in, p.payment) == ("berline", "10 M", "Clio 2016", "FINANCEMENT")


# --- Fiche --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fiche_like_the_example(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {
        "need": "SUV", "budget": "15–20 M FCFA", "payment": "FINANCEMENT", "trade_in": "Peugeot 3008 2021",
        "timeline": "MOINS_1_MOIS"})
    fiche = await prospect.build_fiche(db_session, tenant, customer)
    assert fiche["lines"] == [
        "Prospect : Mamadou Diop", f"Téléphone : +{customer.whatsapp_number}", "Ville : Dakar",
        "Véhicule recherché : SUV", "Budget : 15–20 M FCFA", "Paiement : Financement", "Reprise : Peugeot 3008 2021",
        "Projet : achat sous 1 mois", "Score commercial : Chaud (achat sous 1 mois, budget connu)",
    ]


@pytest.mark.asyncio
async def test_fiche_uses_the_appointment_when_needed(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    assert await prospect.build_fiche(db_session, tenant, customer) is None  # rien appris : pas de fiche
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="x", status="REQUESTED", need="break",
                                      budget="8 M", financing_interest=False, vehicle_label="Peugeot 308"))
    await db_session.commit()
    fiche = await prospect.build_fiche(db_session, tenant, customer)
    assert fiche["need"] == "break" and fiche["payment"] == "COMPTANT" and fiche["vehicle"] == "Peugeot 308"
    assert fiche["has_appointment"] is True and fiche["score"] == "TIEDE"


@pytest.mark.asyncio
async def test_past_or_cancelled_appointment_is_not_a_current_appointment(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="x", status="CANCELLED"))
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="x", status="CONFIRMED", scheduled_at=NOW - timedelta(days=2)))
    await db_session.commit()
    fiche = await prospect.build_fiche(db_session, tenant, customer)
    assert fiche["has_appointment"] is False


@pytest.mark.asyncio
async def test_no_fiche_for_a_store(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session, ONLINE_STORE)
    db_session.add(ProspectProfile(tenant_id=tenant.id, customer_id=customer.id, budget="10"))
    await db_session.commit()
    assert await prospect.build_fiche(db_session, tenant, customer) is None


# --- Emails ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_booking_and_reminder_emails_carry_the_fiche(db_session):
    from zoneinfo import ZoneInfo

    from app.services.appointment_service import booking_alert_email, staff_reminder_email
    from app.services.email_layout import render_html
    from app.services.handoff_service import build_handoff_alert

    tenant, customer, conversation, _ = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M", "timeline": "MOINS_1_MOIS"})
    appt = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, kind="ESSAI",
                              availability="x", status="CONFIRMED", scheduled_at=NOW + timedelta(days=1), budget="15 M")
    db_session.add(appt)
    await db_session.commit()
    fiche = await prospect.build_fiche(db_session, tenant, customer)
    for subject, body in (booking_alert_email(appt, "Mamadou", ZoneInfo("Africa/Dakar"), "https://x/c", fiche),
                          staff_reminder_email(appt, "Mamadou", ZoneInfo("Africa/Dakar"), "https://x/c", fiche),
                          build_handoff_alert(customer, conversation, "Rendez-vous à confirmer : essai", fiche)):
        assert "Fiche prospect\n\nProspect : Mamadou Diop" in body
        assert "Score commercial : Chaud (rendez-vous pris, achat sous 1 mois, budget connu)" in body
        assert "Ce que Bob a appris" not in body
        html = render_html(subject, body, "Bob", False)
        assert ">Score commercial</td>" in html  # tableau dans la version HTML
    _, body = build_handoff_alert(customer, conversation, "x")  # sans fiche : inchangé
    assert "Fiche prospect" not in body


@pytest.mark.asyncio
async def test_hot_prospect_without_appointment_is_sent_once(db_session):
    tenant, customer, conversation, email = await _dealer(db_session, commercial_email="moussa@example.com")
    await prospect.update_profile(db_session, tenant.id, customer.id, {"timeline": "MOINS_1_MOIS"})
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation) == []  # tiède : rien
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M"})
    mails = await prospect.hot_alert_emails(db_session, tenant, customer, conversation)
    assert {m["to"] for m in mails} == {email, "moussa@example.com"}
    assert mails[0]["subject"] == "Prospect chaud à rappeler : Mamadou Diop"
    assert "Score commercial : Chaud" in mails[0]["body"] and f"+{customer.whatsapp_number}" in mails[0]["body"]
    await db_session.commit()
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation) == []  # une seule fois


@pytest.mark.asyncio
async def test_hot_alert_is_reserved_even_with_a_stale_read(db_session):
    from sqlalchemy.orm.attributes import set_committed_value

    tenant, customer, conversation, _ = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M", "timeline": "MOINS_1_MOIS"})
    await db_session.commit()
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation)
    await db_session.commit()
    profile = await prospect.load(db_session, tenant.id, customer.id)
    set_committed_value(profile, "hot_alert_sent_at", None)  # un autre traitement l'a lu avant l'envoi
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation) == []


@pytest.mark.asyncio
async def test_no_hot_alert_with_an_appointment(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M", "timeline": "MOINS_1_MOIS"})
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="x", status="REQUESTED"))
    await db_session.commit()
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation) == []  # la fiche part avec le rendez-vous


@pytest.mark.asyncio
async def test_whatsapp_conversation_triggers_the_hot_alert(client, db_session, monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: sent.append(kw) or True)
    pn = f"PN43{uuid.uuid4().hex[:6]}"
    tenant, customer, conversation, email = await _dealer(db_session, pn=pn)
    llm = FakeLLMClient([tool_use_response("update_prospect_profile", {"budget": "15 M", "timeline": "MOINS_1_MOIS"}),
                         text_response("Très bien ! Vous préférez un SUV ou une berline ?")])
    app.dependency_overrides[get_llm_client] = lambda: llm
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pn}, "messages": [
        {"from": customer.whatsapp_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
         "text": {"body": "Je veux acheter ce mois-ci, budget 15 millions"}, "timestamp": "1"}]}}]}]}
    try:
        await client.post("/webhooks/whatsapp", json=payload)
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    hot = [m for m in sent if m["subject"].startswith("Prospect chaud")]
    assert len(hot) == 1 and hot[0]["to"] == email


# --- Tableau de bord ---------------------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_fiche_in_appointments_and_customer_page(client, db_session):
    tenant, customer, conversation, email = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M", "timeline": "MOINS_1_MOIS"})
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="ESSAI", availability="samedi", status="REQUESTED"))
    await db_session.commit()
    h = await _headers(client, email)
    [item] = (await client.get("/api/v1/appointments?view=pending", headers=h)).json()["items"]
    assert item["prospect"]["score"] == "CHAUD" and "Projet : achat sous 1 mois" in item["prospect"]["lines"]
    detail = (await client.get(f"/api/v1/customers/{customer.id}", headers=h)).json()
    assert detail["prospect"]["score_label"] == "Chaud"


@pytest.mark.asyncio
async def test_customer_page_has_no_fiche_for_a_store_or_another_shop(client, db_session):
    store, customer, _, email = await _dealer(db_session, ONLINE_STORE)
    db_session.add(ProspectProfile(tenant_id=store.id, customer_id=customer.id, budget="10"))
    await db_session.commit()
    h = await _headers(client, email)
    assert (await client.get(f"/api/v1/customers/{customer.id}", headers=h)).json()["prospect"] is None
    _, other_customer, _, _ = await _dealer(db_session)
    assert (await client.get(f"/api/v1/customers/{other_customer.id}", headers=h)).status_code == 404


def test_dashboard_shows_score_and_fiche():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "function scorePill(p)" in html and "Fiche prospect ${scorePill(c.prospect)}" in html
    assert "${scoreBadge}" in html and "esc(label)" in html and "esc(rest.join" in html


@pytest.mark.asyncio
async def test_transfer_alert_from_whatsapp_carries_the_fiche(client, db_session, monkeypatch):
    from app.api.webhooks import whatsapp as webhook
    from app.services.handoff_rules import TRANSFER_NOW, TurnDecision

    sent = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: sent.append(kw) or True)
    pn = f"PN43{uuid.uuid4().hex[:6]}"
    tenant, customer, conversation, email = await _dealer(db_session, pn=pn)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"need": "SUV", "budget": "15 M"})
    await db_session.commit()

    async def now(db, tenant, signal):
        return TurnDecision(mode=TRANSFER_NOW, rule="HUMAN_REQUEST")

    monkeypatch.setattr(webhook, "decide_turn", now)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([])
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pn}, "messages": [
        {"from": customer.whatsapp_number, "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
         "text": {"body": "Je veux parler à un conseiller"}, "timestamp": "1"}]}}]}]}
    try:
        await client.post("/webhooks/whatsapp", json=payload)
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    [alert] = [m for m in sent if m["subject"].startswith("Un client attend")]
    assert "Fiche prospect" in alert["body"] and "Score commercial : Tiède" in alert["body"]


def test_outcome_reasons_are_shown():
    assert prospect.score(_fiche(outcome="FOLLOW_UP"))[1] == ["venu, à relancer"]
    assert prospect.score(_fiche(outcome="NO_SHOW"))[1] == ["absent au rendez-vous"]
    assert prospect.score(_fiche(outcome="SOLD")) == ("VENDU", ["venu, vendu"])


@pytest.mark.asyncio
async def test_visited_prospect_uses_the_appointment_outcome_and_gets_no_hot_alert(db_session):
    tenant, customer, conversation, _ = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "15 M", "timeline": "MOINS_1_MOIS"})
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, kind="ESSAI",
                                      availability="x", status="CONFIRMED", scheduled_at=NOW - timedelta(days=2), outcome="FOLLOW_UP"))
    await db_session.commit()
    fiche = await prospect.build_fiche(db_session, tenant, customer)
    assert fiche["score"] == "CHAUD" and "venu, à relancer" in fiche["reasons"]
    assert await prospect.hot_alert_emails(db_session, tenant, customer, conversation) == []  # il est déjà venu


@pytest.mark.asyncio
async def test_hot_alert_for_a_purchase_within_three_months(db_session):
    tenant, customer, conversation, email = await _dealer(db_session)
    await prospect.update_profile(db_session, tenant.id, customer.id, {"budget": "12 M", "timeline": "UN_A_3_MOIS"})
    [mail] = await prospect.hot_alert_emails(db_session, tenant, customer, conversation)
    assert "Score commercial : Chaud (achat dans 1 à 3 mois, budget connu)" in mail["body"]
