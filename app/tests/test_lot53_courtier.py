"""
Lot 53 — secteur « Courtier / agent d'assurance » et renommage « Commerce ».
Jamais de prix donné par Bob ; qualification par branche ; demande de cotation transmise par le CODE ;
appel ou rendez-vous au cabinet ; concession et commerce strictement inchangés.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agents.orchestrator import generate_ai_reply_detailed
from app.agents.prompts import build_system_prompt
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.product import Product
from app.models.quote_request import QuoteRequest
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import insurance
from app.services.business_type import (
    BUSINESS_TYPES,
    CAR_DEALERSHIP,
    INSURANCE_BROKER,
    ONLINE_STORE,
    appointment_kinds,
    tool_allowed,
    tools_for,
)
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


async def _cabinet(db, email=None, business_type=INSURANCE_BROKER, role=Role.OWNER, with_account=True):
    email = email or f"c{uuid.uuid4().hex[:8]}@l53.ci"
    tenant = Tenant(name="Cabinet Kouassi", country="CI", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type=business_type, business_type_chosen_at=datetime.now(timezone.utc))
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    tenant.pnid = f"pn-{uuid.uuid4().hex[:8]}"
    if with_account:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=tenant.pnid, system_user_token="t"))
    await db.commit()
    return tenant


async def _conversation(db, tenant, number=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number or f"2250{uuid.uuid4().int % 10**9:09d}", first_name="Awa")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.commit()
    return customer, conversation


async def _headers(client, tenant):
    token = (await client.post("/api/v1/auth/login", data={"username": tenant.email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Le secteur ---------------------------------------------------------------------------------------------

def test_sectors_and_commerce_rename():
    assert BUSINESS_TYPES[ONLINE_STORE]["label"] == "Commerce" and BUSINESS_TYPES[ONLINE_STORE]["hint"] == "En ligne et physique"
    assert BUSINESS_TYPES[INSURANCE_BROKER]["label"] == "Courtier / agent d'assurance"
    assert "Jamais de prix" in BUSINESS_TYPES[INSURANCE_BROKER]["description"]
    assert appointment_kinds(INSURANCE_BROKER) == ("CABINET", "APPEL")
    assert appointment_kinds(CAR_DEALERSHIP) == ("ESSAI", "VISITE", "ESTIMATION_REPRISE")
    assert appointment_kinds(ONLINE_STORE) == ()


def test_insurance_tools_never_show_a_price():
    tools = {t["name"]: t for t in tools_for(INSURANCE_BROKER, TOOL_DEFINITIONS)}
    for forbidden in ("create_order", "share_payment_link", "negotiate_price", "check_stock", "get_product_price",
                      "check_order_status", "send_product_images", "update_prospect_profile",
                      "suggest_complementary_products", "get_frequently_bought_together"):
        assert forbidden not in tools, forbidden
        assert not tool_allowed(INSURANCE_BROKER, forbidden)
    for kept in ("update_insurance_request", "request_appointment", "get_available_slots", "get_my_appointments",
                 "search_products", "recommend_products", "handoff_to_human", "record_customer_email"):
        assert kept in tools, kept
    appointment = tools["request_appointment"]["input_schema"]["properties"]
    assert appointment["kind"]["enum"] == ["CABINET", "APPEL"]
    assert not {"budget", "trade_in", "financing_interest"} & set(appointment)
    assert not {"min_price", "max_price"} & set(tools["search_products"]["input_schema"]["properties"])
    assert "budget" not in tools["recommend_products"]["input_schema"]["properties"]
    assert "Aucun prix" in tools["search_products"]["description"]
    # Le catalogue commun n'est jamais modifié par l'adaptation.
    original = next(t for t in TOOL_DEFINITIONS if t["name"] == "request_appointment")
    assert original["input_schema"]["properties"]["kind"]["enum"] == ["ESSAI", "VISITE", "ESTIMATION_REPRISE"]
    # L'outil du courtier n'existe ni en commerce ni en concession.
    for other in (ONLINE_STORE, CAR_DEALERSHIP):
        assert "update_insurance_request" not in {t["name"] for t in tools_for(other, TOOL_DEFINITIONS)}
        assert not tool_allowed(other, "update_insurance_request")


def test_prompt_of_the_cabinet():
    tenant = Tenant(name="Cabinet Kouassi", country="CI", currency="XOF", email="x@y.ci", business_type=INSURANCE_BROKER)
    prompt = build_system_prompt(tenant, now=datetime(2026, 10, 9, 10, tzinfo=timezone.utc))
    # Lot 54 : sans statut renseigné, « intermédiaire en assurance » (plus jamais « courtage / agence » à la fois).
    assert "assistant virtuel de Cabinet Kouassi, intermédiaire en assurance." in prompt
    assert "Ne donne JAMAIS de montant" in prompt and "update_insurance_request" in prompt and "kind APPEL ou CABINET" in prompt
    assert "Vouvoie TOUJOURS" in prompt and "CALENDRIER (heure du cabinet)" in prompt
    assert "concession" not in prompt.lower() and "Devise" not in prompt


# --- Garde-fou des montants ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "La prime est de 85 000 FCFA par an.", "Comptez environ 120 000 F CFA.", "À partir de 45 000 francs.",
    "Le tiers coûte 50 000 XOF.", "Une franchise de 10 % s'applique.", "Un plafond de 5 000 000 par sinistre.",
    "Environ 2 millions pour la flotte.", "Prix : 25 €.", "Cotisation mensuelle 15 000.", "Capital de 1 000 000 garanti.",
    "LA PRIME EST DE 85 000 FCFA.", "85 000 fcfa", "Cela fait 2 M FCFA.", "Les jeunes conducteurs paient 20 % de plus.",
    "Votre devis : 140 000.", "Ce sera 140 000 pour l'année.", "Comptez 1.500.000 par véhicule.",
])
def test_amounts_are_caught(text):
    assert insurance.contains_amount(text)


@pytest.mark.parametrize("text", [
    "Combien de véhicules souhaitez-vous assurer ? Vous en avez 10, c'est bien noté.",
    "Pour 3 personnes de 35, 40 et 8 ans, c'est noté.", "Votre véhicule date de 2019 ?",
    "Votre contrat arrive à échéance le 15 octobre 2026.", "Appelez-nous au +225 07 11 22 33 44.",
    "Nous sommes ouverts de 8 h à 17 h.", "Un conseiller vous appelle samedi 10 octobre.",
    "Le montant dépend de votre situation : votre conseiller vous fera une proposition personnalisée.",
    "Le prix dépend de 3 critères : le véhicule, l'usage et le conducteur.",
    "Votre véhicule a 80 000 km, c'est noté.", "Le numéro du cabinet : 27 22 41 10 00.",
])
def test_ordinary_numbers_are_not_amounts(text):
    assert not insurance.contains_amount(text)


@pytest.mark.asyncio
async def test_bob_rewrites_an_amount_then_falls_back(db_session):
    tenant = await _cabinet(db_session)
    _, conversation = await _conversation(db_session, tenant)

    fake = FakeLLMClient([text_response("La prime auto est de 85 000 FCFA."),
                          text_response("Le conseiller vous fera une proposition personnalisée. Appel ou rendez-vous ?")])
    text, failure = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "C'est combien ?", fake)
    assert failure is None and text.startswith("Le conseiller vous fera")
    assert "CONSIGNE (correction) : ta réponse contient un montant" in fake.received_systems[1]

    stubborn = FakeLLMClient([text_response("85 000 FCFA."), text_response("Bon, disons 80 000 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Combien ?", stubborn)
    assert text == insurance.AMOUNT_FALLBACK and stubborn.call_count == 2


@pytest.mark.asyncio
async def test_other_sectors_keep_their_prices(db_session):
    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    _, conversation = await _conversation(db_session, store)
    fake = FakeLLMClient([text_response("Le sac coûte 15 000 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, store, conversation, [], "Prix ?", fake)
    assert text == "Le sac coûte 15 000 FCFA." and fake.call_count == 1


# --- Demande de cotation ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quote_request_is_submitted_by_the_code_once_complete(db_session):
    tenant = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    now = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)

    first = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                           {"branch": "AUTO", "vehicle": "  Toyota   Corolla ", "vehicle_year": "2019"}, now)
    assert not first["submitted_now"] and first["missing"] == ["Usage"] and first["request"].status == "DRAFT"
    assert first["request"].details == {"vehicle": "Toyota Corolla", "vehicle_year": 2019}

    complete = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                              {"branch": "AUTO", "usage": "privé", "vehicle_year": "deux mille", "current_insurer": "NSIA"}, now)
    # Lot 54 (CIMA) : complète, mais rien ne part sans l'accord du client.
    assert not complete["submitted_now"] and complete["needs_consent"] and complete["request"].status == "DRAFT"
    done = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id, {"branch": "AUTO", "consent": True}, now)
    assert done["submitted_now"] and done["request"].consent_at == now and not done["needs_consent"]
    done["rejected"] = complete["rejected"]
    assert done["submitted_now"] and done["request"].status == "SUBMITTED" and done["request"].submitted_at == now
    assert done["rejected"] == ["vehicle_year"] and done["request"].details["vehicle_year"] == 2019  # jamais écrasé par une valeur douteuse

    again = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                           {"branch": "AUTO", "coverage": "tous risques"}, now)
    assert not again["submitted_now"] and again["request"].id == done["request"].id
    assert (await db_session.execute(select(func.count(QuoteRequest.id)))).scalar_one() == 1


@pytest.mark.asyncio
async def test_company_name_is_needed_for_a_company(db_session):
    tenant = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    flotte = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                            {"branch": "FLOTTE", "vehicles_count": 10, "usage": "transport"})
    assert flotte["missing"] == ["Entreprise"] and flotte["request"].client_type == "ENTREPRISE"
    sante = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                           {"branch": "SANTE", "client_type": "ENTREPRISE", "persons_count": 40, "ages": "25 à 60 ans"})
    assert sante["missing"] == ["Entreprise"]
    assert (await insurance.update_request(db_session, tenant.id, customer.id, conversation.id, {"branch": "PISCINE"}))["error"]
    assert (await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                           {"branch": "AUTO", "client_type": "ASSOCIATION"}))["error"]


def test_value_cleaning():
    assert insurance._clean_value("persons_count", True) is None
    assert insurance._clean_value("persons_count", "-1") is None and insurance._clean_value("persons_count", 100001) is None
    assert insurance._clean_value("persons_count", " 4 ") == 4 and insurance._clean_value("persons_count", 0) == 0
    assert insurance._clean_value("vehicle", 12) is None and insurance._clean_value("vehicle", "   ") is None
    # Lot 54 : l'échéance est une vraie date, la durée une valeur de la liste ; jamais devinées.
    assert insurance._clean_value("current_expiry", "x" * 100) is None
    assert insurance._clean_value("current_expiry", " 2026-10-25 ") == "2026-10-25"
    assert insurance._clean_value("current_expiry", "2026-13-01") is None and insurance._clean_value("current_expiry", "1999-01-01") is None
    assert insurance._clean_value("current_term", "MENSUEL") == "MENSUEL" and insurance._clean_value("current_term", "HEBDO") is None


@pytest.mark.asyncio
async def test_a_handled_request_starts_a_new_one(db_session):
    tenant = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    first = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                           {"branch": "VOYAGE", "destination": "France", "travel_dates": "décembre", "persons_count": 2})
    first["request"].status = "HANDLED"
    second = await insurance.update_request(db_session, tenant.id, customer.id, conversation.id,
                                            {"branch": "VOYAGE", "destination": "Maroc"})
    assert second["request"].id != first["request"].id and second["request"].status == "DRAFT"


@pytest.mark.asyncio
async def test_the_tool_guides_bob(db_session):
    tenant = await _cabinet(db_session)
    _, conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=INSURANCE_BROKER)

    partial = await executor.execute("update_insurance_request", {"branch": "HABITATION", "occupancy": "locataire"})
    assert partial["missing"] == ["Type de logement"] and "il manque : Type de logement" in partial["instruction"]
    ready = await executor.execute("update_insurance_request", {"branch": "HABITATION", "housing_type": "appartement", "ages": 5})
    assert ready["status"] == "consent_needed" and "consent=true" in ready["instruction"] and ready["not_understood"] == ["ages"]
    sent = await executor.execute("update_insurance_request", {"branch": "HABITATION", "consent": True})
    assert sent["status"] == "quote_request_sent" and "appel" in sent["instruction"]
    more = await executor.execute("update_insurance_request", {"branch": "HABITATION", "coverage": "vol"})
    assert more["status"] == "quote_request_updated"
    notes = (await db_session.execute(select(Message.content).where(Message.message_type == "quote_request"))).scalars().all()
    assert notes == ["Demande de cotation transmise au cabinet : Assurance habitation"]
    assert "error" in await executor.execute("update_insurance_request", {"branch": "INCONNUE"})


@pytest.mark.asyncio
async def test_appointment_kinds_follow_the_sector(db_session):
    tenant = await _cabinet(db_session)
    _, conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=INSURANCE_BROKER)
    refused = await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi 10 octobre"})
    assert refused["error"] == "Type de rendez-vous invalide : CABINET, APPEL"
    ok = await executor.execute("request_appointment", {"kind": "APPEL", "availability": "lundi 12 octobre matin",
                                                        "vehicle": "assurance auto"})
    assert ok["status"] == "appointment_requested"
    [appointment] = (await db_session.execute(select(AppointmentRequest))).scalars().all()
    assert (appointment.kind, appointment.vehicle_label) == ("APPEL", "assurance auto")

    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)
    _, dealer_conv = await _conversation(db_session, dealer)
    no_cabinet = await ToolExecutor(db_session, dealer.id, dealer_conv, business_type=CAR_DEALERSHIP).execute(
        "request_appointment", {"kind": "CABINET", "availability": "samedi"})
    assert no_cabinet["error"] == "Type de rendez-vous invalide : ESSAI, VISITE, ESTIMATION_REPRISE"


@pytest.mark.asyncio
async def test_products_are_shown_without_price(db_session):
    tenant = await _cabinet(db_session)
    _, conversation = await _conversation(db_session, tenant)
    db_session.add(Product(tenant_id=tenant.id, sku="A", name="Assurance automobile", description="Tiers ou tous risques",
                           price=0, currency="XOF", stock_quantity=0))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=INSURANCE_BROKER)
    [found] = (await executor.execute("search_products", {"query": "automobile"}))["results"]
    assert set(found) == {"product_id", "name", "description", "active"}
    [recommended] = (await executor.execute("recommend_products", {"customer_need": "auto"}))["results"]
    assert "price" not in recommended and "stock" not in recommended


# --- De bout en bout : WhatsApp ----------------------------------------------------------------------------

def _payload(pnid, sender, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


@pytest.mark.asyncio
async def test_whatsapp_quote_request_reaches_the_cabinet_once(client, db_session, wire):  # noqa: F811
    tenant = await _cabinet(db_session)
    state = wire({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("update_insurance_request", {"branch": "SANTE", "persons_count": 3, "ages": "35, 33 et 6 ans",
                                                       "consent": True}),
        text_response("C'est transmis ! Préférez-vous un appel ou un rendez-vous au cabinet ?"),
        text_response("Très bien, je note."),
    ])

    first = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250711111111", "Une mutuelle pour ma famille"))
    second = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "2250711111111", "Un appel plutôt"))

    assert first.json()["ai_reply"].startswith("C'est transmis") and second.status_code == 200
    mails = [m for m in state["outbox"] if m["subject"].startswith("Nouvelle demande de cotation")]
    assert len(mails) == 1 and mails[0]["to"] == tenant.email
    assert mails[0]["subject"] == "Nouvelle demande de cotation — Assurance santé — +2250711111111"
    body = mails[0]["body"]
    assert "Nombre de personnes : 3" in body and "Âges : 35, 33 et 6 ans" in body and "aucun prix" in body
    assert "/dashboard/?conversation=" in body
    assert "Accord du client pour la transmission : " in body and "Transmise au cabinet : " in body  # lot 54 (CIMA)
    # Lot 54 : les messages du courtier sont analysés avec SA liste d'objections.
    from app.models.message_signal import MessageSignal

    versions = (await db_session.execute(select(MessageSignal.taxonomy_version))).scalars().all()
    assert versions == ["v1.4assu", "v1.4assu"]


@pytest.mark.asyncio
async def test_quote_alert_only_for_the_cabinet(db_session):
    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    customer, conversation = await _conversation(db_session, store)
    # Une demande restée d'un ancien secteur (changé par le Super Admin) n'envoie rien en commerce.
    db_session.add(QuoteRequest(tenant_id=store.id, customer_id=customer.id, branch="AUTO", client_type="PARTICULIER",
                                details={}, status="SUBMITTED"))
    await db_session.commit()
    assert await insurance.quote_alert_emails(db_session, store, customer, conversation) == []
    assert await insurance.quote_alert_emails(db_session, None, customer, conversation) == []


# --- Produits par défaut, catalogue sans prix ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_choosing_the_sector_creates_the_default_products(client, db_session):
    tenant = Tenant(name="Cabinet", country="CI", currency="XOF", email=f"s{uuid.uuid4().hex[:6]}@l53.ci", plan=TenantPlan.FREE)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=tenant.email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    headers = await _headers(client, tenant)

    r = await client.put("/api/v1/tenants/me/business-type", json={"business_type": INSURANCE_BROKER}, headers=headers)

    assert r.status_code == 200 and r.json()["options"][2]["hint"] is None
    products = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    assert len(products) == 11 and {p.sku for p in products} == {f"ASSUR-{c}" for c in insurance.DEFAULT_PRODUCTS}
    assert all(float(p.price) == 0 and p.stock_quantity == 0 and p.description for p in products)
    assert "MARCHANDISES" in insurance.DEFAULT_PRODUCTS and "AUTRE" not in insurance.DEFAULT_PRODUCTS
    assert await insurance.seed_products(db_session, tenant) == 0  # jamais deux fois


@pytest.mark.asyncio
async def test_products_without_price_for_the_cabinet_only(client, db_session):
    cabinet = await _cabinet(db_session)
    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    h_cab, h_store = await _headers(client, cabinet), await _headers(client, store)

    r = await client.post("/api/v1/products", headers=h_cab, json={"sku": "AUTO", "name": "Auto", "currency": "XOF",
                                                                  "description": "Tiers", "price": 9000, "stock_quantity": 4})
    assert r.status_code == 201 and float(r.json()["price"]) == 0 and r.json()["stock_quantity"] == 0
    updated = await client.put(f"/api/v1/products/{r.json()['id']}", headers=h_cab, json={"price": 5000, "name": "Auto plus"})
    assert updated.status_code == 200 and float(updated.json()["price"]) == 0 and updated.json()["name"] == "Auto plus"

    missing = await client.post("/api/v1/products", headers=h_store, json={"sku": "S", "name": "Sac", "currency": "XOF"})
    assert missing.status_code == 422 and missing.json()["detail"] == "Le prix est obligatoire"
    priced = await client.post("/api/v1/products", headers=h_store, json={"sku": "S", "name": "Sac", "currency": "XOF", "price": 15000})
    assert priced.status_code == 201 and float(priced.json()["price"]) == 15000


@pytest.mark.asyncio
async def test_csv_import_without_prices(client, db_session):
    cabinet = await _cabinet(db_session)
    csv_text = "SKU,NAME,DESCRIPTION,CATEGORY,STOCK\nRC,Responsabilité civile pro,Dommages aux tiers,Entreprises,5\n"
    r = await client.post("/api/v1/catalog/import-csv", headers=await _headers(client, cabinet),
                          files={"file": ("a.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["imported"] == 1, r.json()
    product = (await db_session.execute(select(Product).where(Product.tenant_id == cabinet.id))).scalar_one()
    assert float(product.price) == 0 and product.currency == "XOF" and product.stock_quantity == 0

    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    r = await client.post("/api/v1/catalog/import-csv", headers=await _headers(client, store),
                          files={"file": ("a.csv", csv_text.encode(), "text/csv")})
    assert r.json()["imported"] == 0 and "Colonnes obligatoires manquantes" in r.json()["errors"][0]


# --- Demandes de cotation : API et tableau de bord -------------------------------------------------------

@pytest.mark.asyncio
async def test_quote_requests_api(client, db_session):
    cabinet = await _cabinet(db_session)
    other = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    sent = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                          {"branch": "SCOLAIRE", "children_count": 2, "client_type": "PARTICULIER", "consent": True})
    draft = await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id, {"branch": "AUTO"})
    o_customer, o_conv = await _conversation(db_session, other)
    await insurance.update_request(db_session, other.id, o_customer.id, o_conv.id, {"branch": "SCOLAIRE", "children_count": 1, "consent": True})
    await db_session.commit()
    headers = await _headers(client, cabinet)

    todo = (await client.get("/api/v1/quote-requests", headers=headers)).json()
    assert [q["id"] for q in todo] == [str(sent["request"].id)]
    assert todo[0]["branch_label"] == "Assurance scolaire" and todo[0]["lines"] == ["Nombre d'enfants : 2"]
    assert todo[0]["status_label"] == "Reçue" and todo[0]["conversation_id"] == str(conversation.id)
    drafts = (await client.get("/api/v1/quote-requests?view=draft", headers=headers)).json()
    assert [q["id"] for q in drafts] == [str(draft["request"].id)]
    assert (await client.get("/api/v1/quote-requests?view=x", headers=headers)).status_code == 422

    handled = await client.post(f"/api/v1/quote-requests/{sent['request'].id}/handle", headers=headers)
    assert handled.status_code == 200 and handled.json()["status"] == "HANDLED"
    assert (await client.post(f"/api/v1/quote-requests/{sent['request'].id}/handle", headers=headers)).status_code == 409
    assert (await client.get("/api/v1/quote-requests", headers=headers)).json() == []
    assert len((await client.get("/api/v1/quote-requests?view=handled", headers=headers)).json()) == 1

    foreign = (await db_session.execute(select(QuoteRequest).where(QuoteRequest.tenant_id == other.id))).scalar_one()
    assert (await client.post(f"/api/v1/quote-requests/{foreign.id}/handle", headers=headers)).status_code == 404

    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    assert (await client.get("/api/v1/quote-requests", headers=await _headers(client, store))).status_code == 403


@pytest.mark.asyncio
async def test_tasks_and_home_of_the_cabinet(client, db_session):
    from app.services.home_service import home_summary
    from app.services.notifications import task_counts

    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    await insurance.update_request(db_session, cabinet.id, customer.id, conversation.id,
                                   {"branch": "MOTO", "vehicle": "Yamaha", "usage": "livraison", "consent": True})
    other = await _cabinet(db_session)  # une autre boutique ne compte jamais
    o_customer, o_conv = await _conversation(db_session, other)
    await insurance.update_request(db_session, other.id, o_customer.id, o_conv.id, {"branch": "SCOLAIRE", "children_count": 1,
                                                                                  "consent": True})
    # Un rendez-vous à confirmer passe APRÈS la demande de cotation dans « À traiter ».
    db_session.add(AppointmentRequest(tenant_id=cabinet.id, customer_id=customer.id, conversation_id=conversation.id,
                                      kind="APPEL", availability="lundi", status="REQUESTED"))
    await db_session.commit()

    counts = await task_counts(db_session, cabinet)
    assert counts["quotes"] == 1 and counts["orders"] == 0 and counts["appointments"] == 1 and counts["total"] == 2
    home = await home_summary(db_session, cabinet.id)
    assert home["business_type"] == INSURANCE_BROKER and "appointments" in home["kpis"]
    assert [t["kind"] for t in home["todo"]] == ["QUOTE", "APPOINTMENT"]
    [quote] = [t for t in home["todo"] if t["kind"] == "QUOTE"]
    # Lot 54 : cotation transmise + appel prévu = prospect chaud, signalé dans la tâche.
    assert quote["detail"] == "Assurance moto · 🔥 demande de cotation transmise, appel ou rendez-vous prévu"
    assert quote["reason"] == "Prospect chaud" and quote["tone"] == "danger" and quote["action"] == "Préparer la cotation"
    # Lot 54 : la cotation est l'étape clé du courtier.
    assert [f["label"] for f in home["funnel"]] == ["Conversations", "Demandes de cotation", "Rendez-vous et appels", "Contrats souscrits"]

    store = await _cabinet(db_session, business_type=ONLINE_STORE)
    assert (await task_counts(db_session, store))["quotes"] == 0
    assert (await home_summary(db_session, store.id))["business_type"] == ONLINE_STORE


@pytest.mark.asyncio
async def test_the_cabinet_reaches_appointments_and_commercials(client, db_session):
    cabinet = await _cabinet(db_session)
    headers = await _headers(client, cabinet)
    assert (await client.get("/api/v1/appointments?view=pending", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/analytics/commercials", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/orders", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/tenants/me/negotiation-settings", headers=headers)).status_code == 403


# --- Messages au client ---------------------------------------------------------------------------------

def test_appointment_messages_speak_cabinet():
    from zoneinfo import ZoneInfo

    from app.services.appointment_outcome import email_text, whatsapp_text
    from app.services.appointment_service import confirmation_message

    zone = ZoneInfo("Africa/Abidjan")
    when = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    cabinet = AppointmentRequest(kind="CABINET", scheduled_at=when, vehicle_label="assurance santé")
    call = AppointmentRequest(kind="APPEL", scheduled_at=when, vehicle_label="Assurance automobile")
    assert confirmation_message(cabinet, "Cabinet K", zone) == \
        "Bonjour ! Votre rendez-vous au cabinet (assurance santé) est confirmé le lundi 12 octobre à 9 h. À bientôt chez Cabinet K !"
    assert confirmation_message(call, "Cabinet K", zone) == \
        "Bonjour ! C'est noté : un conseiller de Cabinet K vous appelle le lundi 12 octobre à 9 h au sujet de votre assurance automobile. À bientôt !"
    call.outcome = "NO_SHOW"
    assert whatsapp_text(call, "Cabinet K").startswith("Bonjour, nous n'avons pas réussi à vous joindre (Cabinet K).")
    cabinet.outcome = "NO_SHOW"
    assert "nous ne vous avons pas vu à votre rendez-vous" in whatsapp_text(cabinet, "Cabinet K")
    cabinet.outcome = "FOLLOW_UP"
    assert "questions sur assurance santé" in whatsapp_text(cabinet, "Cabinet K") and "essai" not in whatsapp_text(cabinet, "Cabinet K")
    subject, body = email_text(cabinet, "Cabinet K")
    assert subject == "Merci pour notre échange — Cabinet K" and "écrivez-nous sur WhatsApp" in body and "\n\nÀ bientôt,\nCabinet K" in body


@pytest.mark.asyncio
async def test_a_subscribed_contract_never_hides_the_product(db_session):
    from app.services.appointment_outcome import record_outcome

    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    product = Product(tenant_id=cabinet.id, sku="A", name="Auto", price=0, currency="XOF", stock_quantity=0)
    db_session.add(product)
    await db_session.flush()
    appointment = AppointmentRequest(tenant_id=cabinet.id, customer_id=customer.id, conversation_id=conversation.id,
                                     kind="CABINET", availability="x", status="CONFIRMED", product_id=product.id,
                                     scheduled_at=datetime.now(timezone.utc) - timedelta(days=1))
    db_session.add(appointment)
    await db_session.commit()
    result = await record_outcome(db_session, cabinet, appointment, "SOLD", "u")
    assert result["vehicle_unavailable"] is False


def test_cabinet_always_says_vous_and_has_no_order_recap():
    from app.services.address_form import uses_tu

    cabinet = Tenant(business_type=INSURANCE_BROKER, address_form="TU")
    assert not uses_tu(cabinet)
    assert uses_tu(Tenant(business_type=ONLINE_STORE, address_form="TU"))


@pytest.mark.asyncio
async def test_order_recap_never_for_the_cabinet(db_session):
    from app.services.order_emails import recap_to_send

    cabinet = await _cabinet(db_session)
    customer, conversation = await _conversation(db_session, cabinet)
    customer.email = "client@example.com"
    from app.models.order import Order, OrderStatus

    db_session.add(Order(tenant_id=cabinet.id, customer_id=customer.id, conversation_id=conversation.id,
                         status=OrderStatus.PENDING, total_amount=1000, currency="XOF"))
    await db_session.commit()
    assert await recap_to_send(db_session, cabinet, customer) is None


# --- Tableau de bord ------------------------------------------------------------------------------------------

def _function(name):
    body = HTML[HTML.index(f"function {name}("):]
    return body[:body.index("\n}\n")]


def test_dashboard_sector_switches():
    apply = _function("applyBusinessType")
    assert 'document.body.classList.toggle("rdv-sector", dealership || insurance);' in apply
    assert 'productsNav.lastChild.textContent = insurance ? "Produits d\'assurance" : "Produits";' in apply
    assert "body:not(.insurance) .insurance-only { display: none !important; }" in HTML
    assert "body.insurance .no-insurance { display: none !important; }" in HTML
    assert 'id="nav-quotes" class="insurance-only"' in HTML and 'quotes: ["quotes"],' in HTML
    assert '<th class="no-insurance">Prix</th>' in HTML and 'data-label="Prix" class="no-insurance"' in HTML
    assert 'id="new-product-description" class="input insurance-only"' in HTML
    create = _function("createProduct")
    assert 'if (currentBusinessType === "INSURANCE_BROKER") {' in create and "delete payload.price;" in create
    save = _function("saveProductEdit")
    assert '...(currentBusinessType === "INSURANCE_BROKER" ? {} : {' in save
    # Lot 54 : le courtier a ses objections, l'analyse et les stratégies lui sont montrées.
    assert 'class="card" id="signals-card"' in HTML and 'class="card" id="strategies-card"' in HTML


def test_quote_page_escapes_everything():
    body = _function("loadQuotes")
    for piece in ("${esc(q.customer)}", "${esc(q.branch_label)}", "${esc(l)}", "${esc(q.status_label)}",
                  "jumpToCustomerConversation('${esc(q.conversation_id)}')", "advanceQuote('${esc(q.id)}', '${esc(s)}')", "${esc(e.message)}"):
        assert piece in body, piece
    assert 'if (item.kind === "QUOTE") return `showTab(\'quotes\')`;' in HTML
    assert 'if (name === "quotes") loadQuotes(currentQuoteView);' in HTML
    assert '"orders", "appointments", "quotes", "contracts", "reports", "integrations"' in _function("showTab")  # la page s'affiche vraiment


def test_outcome_words_of_the_cabinet():
    labels = _function("outcomeLabels")
    assert 'SOLD: "Contrat souscrit"' in labels and "Absent / injoignable" in labels
    assert '${insurer ? "Contrats souscrits" : "Véhicules vendus"}' not in HTML  # libellé passé tel quel au kpi
    assert 'insurer ? "Contrats souscrits" : "Véhicules vendus"' in HTML


def test_migration_exists():
    text = (ROOT.parent / "alembic" / "versions" / "c53a8e2f4b61_lot_53_courtier_assurance.py").read_text(encoding="utf-8")
    assert "down_revision = 'a51c0e7b3d24'" in text and "create_table('quote_requests'" in text
