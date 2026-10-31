"""
Lot 79 — secteur « Agence immobilière » (décisions du 17/10 : 1B 2A 3C 4A 5A).

4 publics (acheteur, locataire, vendeur, propriétaire bailleur), fiche bien par type, cartes, fiche projet et score
par règles fixes, visites et estimations ; verrous légaux dans le CODE : aucune pièce interdite au dossier locataire,
aucune question discriminatoire, jamais un prix d'estimation ni un chiffre de crédit. Les autres secteurs restent
inchangés.
"""
import io
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.prompts import build_system_prompt
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.product import Product
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import product_sheets, real_estate
from app.services.business_type import (
    BUSINESS_TYPES,
    CAR_DEALERSHIP,
    INSURANCE_BROKER,
    ONLINE_STORE,
    REAL_ESTATE,
    appointment_kinds,
    is_appointment_sector,
    tool_allowed,
    tools_for,
)
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
DEMO_HTML = (ROOT / "static" / "instant-demo" / "index.html").read_text(encoding="utf-8")


async def _agency(db, business_type=REAL_ESTATE, profile=None, with_account=True):
    email = f"a{uuid.uuid4().hex[:8]}@l79.fr"
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", email=email, plan=TenantPlan.PRO,
                    business_type=business_type, business_type_chosen_at=datetime.now(timezone.utc),
                    real_estate_profile=profile)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    tenant.pnid = f"pn-{uuid.uuid4().hex[:8]}"
    if with_account:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=tenant.pnid, system_user_token="t"))
    await db.commit()
    return tenant


async def _conversation(db, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"336{uuid.uuid4().int % 10**8:08d}", first_name="Claire")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.commit()
    return customer, conversation


def _bien(tenant, sku, name, price, code, sheet, stock=1, image="https://cdn.example.com/b.jpg", description=None):
    return Product(tenant_id=tenant.id, sku=sku, name=name, price=price, currency="EUR", stock_quantity=stock,
                   image_url=image, active=True, bob_category=code, description=description,
                   sheet=product_sheets.normalize_sheet(code, sheet))


async def _headers(client, tenant):
    token = (await client.post("/api/v1/auth/login", data={"username": tenant.email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _payload(pnid, sender, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


# --- Le secteur et ses outils ------------------------------------------------------------------------------

def test_sector_kinds_and_tools():
    assert BUSINESS_TYPES[REAL_ESTATE]["label"] == "Agence immobilière"
    assert BUSINESS_TYPES[REAL_ESTATE]["hint"] == "Vente, location, gestion"
    assert appointment_kinds(REAL_ESTATE) == ("VISITE", "ESTIMATION", "AGENCE", "APPEL")
    assert is_appointment_sector(Tenant(business_type=REAL_ESTATE))
    for tool in ("create_order", "share_payment_link", "negotiate_price", "update_prospect_profile", "update_insurance_request"):
        assert not tool_allowed(REAL_ESTATE, tool), tool
    for tool in ("update_property_project", "request_appointment", "send_product_images", "search_products"):
        assert tool_allowed(REAL_ESTATE, tool), tool
    # La fiche projet n'existe que pour l'agence.
    for sector in (ONLINE_STORE, CAR_DEALERSHIP, INSURANCE_BROKER):
        assert not tool_allowed(sector, "update_property_project"), sector
    # Les autres secteurs gardent leurs outils.
    assert tool_allowed(CAR_DEALERSHIP, "update_prospect_profile") and tool_allowed(ONLINE_STORE, "create_order")
    assert appointment_kinds(CAR_DEALERSHIP) == ("ESSAI", "VISITE", "ESTIMATION_REPRISE")


def test_tools_are_adapted_to_properties():
    tools = {t["name"]: t for t in tools_for(REAL_ESTATE, TOOL_DEFINITIONS)}
    search = tools["search_products"]["input_schema"]
    assert {"transaction", "property_type", "city", "min_rooms", "min_surface", "max_price"} <= set(search["properties"])
    assert "min_price" not in search["properties"] and search["required"] == []
    appointment = tools["request_appointment"]["input_schema"]["properties"]
    assert appointment["kind"]["enum"] == ["VISITE", "ESTIMATION", "AGENCE", "APPEL"]
    assert "trade_in" not in appointment and "financing_interest" not in appointment
    assert "biens" in tools["send_product_images"]["description"]
    project = tools["update_property_project"]["input_schema"]
    assert project["required"] == ["project"] and project["properties"]["project"]["enum"] == ["ACHAT", "LOCATION", "VENTE", "GESTION"]
    # Le schéma partagé n'est pas modifié (copie).
    shared = next(t for t in TOOL_DEFINITIONS if t["name"] == "request_appointment")
    assert shared["input_schema"]["properties"]["kind"]["enum"] == ["ESSAI", "VISITE", "ESTIMATION_REPRISE"]


# --- Fiche du bien -----------------------------------------------------------------------------------------

def test_property_sheet_values_are_checked():
    sheet = product_sheets.normalize_sheet("APPARTEMENT", {
        "transaction": "location", "surface": "68,5 m²", "rooms": "3 pièces", "dpe": "c", "city": "Lyon 3e",
        "fees": "12 €/m²", "brand": "Nike", "elevator": "peut-être", "furnished": "meublé"})
    assert sheet == {"transaction": "Location", "surface": 68.5, "rooms": 3, "dpe": "C", "city": "Lyon 3e",
                     "fees": "12 €/m²", "furnished": "Meublé"}
    # Commerce strictement inchangé : pas de listes ni de nombres, pas de champ de bien.
    assert product_sheets.normalize_sheet("FASHION", {"sizes": "S", "dpe": "C", "rooms": "3"}) == {"sizes": "S"}
    assert all("options" not in f for c in product_sheets.definitions() for f in c["key_fields"])
    defs = {c["code"]: c for c in product_sheets.definitions(True)}
    assert list(defs) == ["APPARTEMENT", "MAISON", "TERRAIN", "LOCAL", "PARKING", "AUTRE"]
    keys = {f["key"]: f for f in defs["APPARTEMENT"]["key_fields"]}
    assert list(keys) == ["transaction", "city", "fees", "surface", "rooms", "dpe"]
    assert keys["transaction"]["options"] == ["Vente", "Location"] and keys["rooms"]["numeric"] is True
    assert [f["key"] for f in product_sheets.common_definitions(True)] == ["highlights", "reference", "visit_link"]


@pytest.mark.parametrize("text, code", [
    ("Bel appartement T3 lumineux", "APPARTEMENT"), ("Studio meublé", "APPARTEMENT"), ("Villa avec piscine", "MAISON"),
    ("Terrain constructible 600 m²", "TERRAIN"), ("Local commercial en angle", "LOCAL"), ("Box fermé", "PARKING"),
    ("Immeuble de rapport", "AUTRE"),
])
def test_property_type_is_guessed_from_words(text, code):
    assert product_sheets.guess_property_type(text) == code


def test_completeness_counts_the_key_fields_of_the_type():
    product = Product(name="T3", bob_category="APPARTEMENT", image_url="https://x/y.jpg", description="Beau",
                      sheet={"transaction": "Vente", "city": "Lyon", "fees": "5 %"})
    assert product_sheets.completeness(product) == 62  # photo, description, 3 des 6 champs clés : 5 sur 8


def test_card_says_type_surface_city_never_the_price():
    from app.services.product_cards import card_body, card_noun, carousel_intro

    product = Product(name="T3 lumineux", price=1150, bob_category="APPARTEMENT",
                      sheet={"transaction": "Location", "rooms": 3, "surface": 68, "city": "Lyon 3e", "dpe": "C"})
    assert card_body(product) == "T3 lumineux\nÀ louer · 3 pièces · 68 m² · Lyon 3e · DPE C"
    assert "1150" not in card_body(product)
    studio = Product(name="Studio", bob_category="APPARTEMENT", sheet={"transaction": "Vente", "rooms": 1})
    assert card_body(studio) == "Studio\nÀ vendre · 1 pièce"
    land = Product(name="Terrain", bob_category="TERRAIN", sheet={"land_surface": 600})
    assert card_body(land) == "Terrain\n600 m²"
    assert card_noun(Tenant(business_type=REAL_ESTATE)) == "bien"
    assert card_noun(Tenant(business_type=CAR_DEALERSHIP)) == "véhicule"
    assert carousel_intro(False, "bien") == "Touchez « Ça m'intéresse » sous le bien qui vous plaît."


# --- Recherche de biens ------------------------------------------------------------------------------------

@pytest.mark.parametrize("query, words, criteria", [
    ("T3 à louer à Lyon", "lyon", {"transaction": "LOCATION", "property_type": "APPARTEMENT", "min_rooms": 3}),
    ("maison 4 pièces à vendre", None, {"transaction": "VENTE", "property_type": "MAISON", "min_rooms": 4}),
    ("studio", None, {"property_type": "APPARTEMENT", "min_rooms": 1}),
    ("appartement 3 chambres 80 m2 Villeurbanne", "villeurbanne",
     {"property_type": "APPARTEMENT", "min_rooms": 4, "min_surface": 80}),
    ("", "", {}),
])
def test_query_words_become_criteria(query, words, criteria):
    assert real_estate.criteria_from_query(query) == (words, criteria)


@pytest.mark.asyncio
async def test_search_finds_properties_by_criteria(db_session):
    tenant = await _agency(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    t3 = _bien(tenant, "B1", "T3 lumineux avec balcon", 1150, "APPARTEMENT",
               {"transaction": "Location", "city": "Lyon 3e", "rooms": 3, "surface": 68})
    studio = _bien(tenant, "B2", "Studio meublé", 620, "APPARTEMENT",
                   {"transaction": "Location", "city": "Lyon 7e", "rooms": 1, "surface": 24})
    maison = _bien(tenant, "B3", "Maison familiale", 435000, "MAISON",
                   {"transaction": "Vente", "city": "Villeurbanne", "rooms": 5, "surface": 120})
    rented = _bien(tenant, "B4", "T4 déjà loué", 1400, "APPARTEMENT",
                   {"transaction": "Location", "city": "Lyon 3e", "rooms": 4}, stock=0)
    db_session.add_all([t3, studio, maison, rented])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=REAL_ESTATE)

    names = lambda result: [r["name"] for r in result.get("results", [])]  # noqa: E731
    assert names(await executor.execute("search_products", {"query": "T3 à louer à Lyon"})) == ["T3 lumineux avec balcon"]
    assert names(await executor.execute("search_products", {"transaction": "VENTE"})) == ["Maison familiale"]
    assert names(await executor.execute("search_products", {"property_type": "APPARTEMENT", "max_price": 700})) == ["Studio meublé"]
    assert names(await executor.execute("search_products", {"min_surface": 100})) == ["Maison familiale"]
    # « lumineux et calme » n'est écrit dans aucune fiche : les critères suffisent, rien n'est caché.
    assert names(await executor.execute("search_products", {"query": "appartement calme", "transaction": "LOCATION"})) == \
        ["T3 lumineux avec balcon", "Studio meublé"]
    result = await executor.execute("search_products", {"city": "Marseille"})
    assert result["results"] == [] and "Aucun bien" in result["message"]
    found = (await executor.execute("search_products", {"query": "T3"}))["results"][0]
    assert found["fiche"]["catégorie"] == "Appartement" and found["fiche"]["Pièces"] == 3
    assert (await executor.execute("search_products", {"min_rooms": "trois"}))["error"].startswith("Critères")


# --- Fiche projet et score ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_tool_saves_checks_and_guides(db_session):
    tenant = await _agency(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=REAL_ESTATE)

    result = await executor.execute("update_property_project", {
        "project": "ACHAT", "property_type": "MAISON", "zone": "Villeurbanne", "budget": "400 000 €",
        "financing": "ACCORD_BANCAIRE", "must_sell": True, "occupancy": "LOUE", "rooms_min": "quatre"})
    assert result["status"] == "saved" and result["not_saved"] == ["nombre de pièces", "bien libre ou loué"]
    assert "ESTIMATION" in result["instruction"]  # il doit vendre son bien : estimation proposée
    assert result["useful_next"] == ["délai", "nombre de pièces"]

    sale = await executor.execute("update_property_project", {"project": "VENTE", "address": "rue Racine, Villeurbanne",
                                                              "property_type": "APPARTEMENT", "surface": 72})
    assert "kind ESTIMATION" in sale["instruction"] and "Ne donne JAMAIS de prix" in sale["instruction"]
    profile = (await db_session.execute(select(ProspectProfile))).scalar_one()
    assert set(profile.real_estate["projects"]) == {"ACHAT", "VENTE"}
    assert profile.real_estate["projects"]["ACHAT"]["budget"] == "400 000 €" and profile.budget == "400 000 €"
    assert (await executor.execute("update_property_project", {"project": "CROISIERE"}))["error"].startswith("Indique")


@pytest.mark.asyncio
async def test_project_not_offered_by_the_agency_is_not_saved(db_session):
    tenant = await _agency(db_session, profile={"status": "AGENCE", "activities": ["VENTE"]})
    customer, conversation = await _conversation(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=REAL_ESTATE)
    result = await executor.execute("update_property_project", {"project": "GESTION", "address": "Lyon"})
    assert result["status"] == "not_offered" and "gestion locative" in result["instruction"]
    assert (await executor.execute("update_property_project", {"project": "LOCATION"}))["status"] == "not_offered"
    assert (await executor.execute("update_property_project", {"project": "ACHAT"}))["status"] == "saved"
    assert (await db_session.execute(select(ProspectProfile))).scalar_one().real_estate["projects"].keys() == {"ACHAT"}


@pytest.mark.parametrize("projects, appointment, outcome, expected", [
    ({}, False, None, "FROID"),
    ({"ACHAT": {"zone": "Lyon"}}, False, None, "FROID"),
    ({"ACHAT": {"zone": "Lyon", "budget": "300 k"}}, False, None, "TIEDE"),
    ({"VENTE": {"address": "Lyon"}}, False, None, "TIEDE"),            # un propriétaire = un mandat possible
    ({"VENTE": {"address": "Lyon"}}, True, None, "CHAUD"),             # estimation prévue
    ({"GESTION": {"timeline": "MOINS_1_MOIS"}}, False, None, "CHAUD"),
    ({"ACHAT": {"timeline": "UN_A_3_MOIS"}}, False, None, "TIEDE"),
    ({"ACHAT": {"timeline": "UN_A_3_MOIS", "financing": "ACCORD_BANCAIRE"}}, False, None, "CHAUD"),
    ({"ACHAT": {"timeline": "MOINS_1_MOIS", "financing": "EN_COURS"}}, True, None, "CHAUD"),
    ({"ACHAT": {"timeline": "PLUS_3_MOIS", "financing": "COMPTANT"}}, False, None, "FROID"),
    ({"LOCATION": {"timeline": "MOINS_1_MOIS"}}, True, None, "CHAUD"),
    ({"LOCATION": {"timeline": "PLUS_3_MOIS"}}, True, None, "TIEDE"),
    ({}, False, "FOLLOW_UP", "TIEDE"),
    ({"VENTE": {}}, True, "SOLD", "VENDU"),
])
def test_score_rules(projects, appointment, outcome, expected):
    assert real_estate.score(projects, appointment, outcome)[0] == expected


@pytest.mark.asyncio
async def test_fiche_and_hot_alert_for_an_owner(db_session):
    from app.services.prospect import build_fiche, hot_alert_emails

    tenant = await _agency(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    await real_estate.update_project(db_session, tenant.id, customer.id, {
        "project": "VENTE", "property_type": "MAISON", "address": "Villeurbanne", "surface": 120, "timeline": "UN_A_3_MOIS"})
    await real_estate.update_project(db_session, tenant.id, customer.id, {
        "project": "ACHAT", "zone": "Lyon 6e", "budget": "500 000 €", "must_sell": True})
    await db_session.commit()
    fiche = await build_fiche(db_session, tenant, customer)
    assert fiche["score"] == "CHAUD" and fiche["score_label"] == "Chaud"
    assert "Projet : Achat : Lyon 6e, budget 500 000 €, doit vendre avant d'acheter" in fiche["lines"]
    assert "Projet : Vente de son bien : maison, Villeurbanne, 120 m², dans 1 à 3 mois" in fiche["lines"]
    assert fiche["lines"][-1].startswith("Score commercial : Chaud (propriétaire : vente de son bien")
    mails = await hot_alert_emails(db_session, tenant, customer, conversation)
    assert len(mails) == 1 and "une visite ou une estimation" in mails[0]["body"]
    assert await hot_alert_emails(db_session, tenant, customer, conversation) == []  # une seule fois


# --- Verrous légaux ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "Pouvez-vous m'envoyer vos 3 derniers relevés de compte ?", "Il faudra une attestation de bonne tenue de compte.",
    "Merci de joindre une photo d'identité.", "Envoyez aussi votre carte Vitale.", "Un extrait de casier judiciaire est demandé.",
    "Un chèque de réservation bloque le logement.", "Quelle est votre nationalité ?", "Avez-vous des enfants ?",
    "Êtes-vous mariée ?", "Pouvez-vous préciser votre religion", "Êtes-vous enceinte ?",
])
def test_forbidden_requests_are_removed(line):
    cleaned, reasons = real_estate.legal_guard(f"Merci pour votre intérêt !\n{line}\nQuelle date d'entrée souhaitez-vous ?")
    assert cleaned == "Merci pour votre intérêt !\nQuelle date d'entrée souhaitez-vous ?" and len(reasons) == 1


def test_allowed_lines_stay():
    text = ("Il faudra une pièce d'identité, vos 3 derniers bulletins de salaire et votre dernier avis d'imposition.\n"
            "Combien de personnes vivront dans le logement ?")
    assert real_estate.legal_guard(text) == (text, [])
    assert real_estate.legal_guard("Quelle est votre nationalité ?")[0] == real_estate.SAFE_FALLBACK


@pytest.mark.parametrize("text, flagged", [
    ("Votre maison vaut environ 300 000 €.", True),
    ("On peut estimer votre appartement autour de 180 000 €.", True),
    ("Vous pourriez le louer 950 € par mois.", True),
    ("Avec un apport de 20 000 €, c'est possible.", True),
    ("Mensualités de 900 € sur 25 ans", True),
    ("Un taux de 3,5 % est courant.", True),
    ("Ce T3 est à louer 850 € par mois charges comprises.", False),
    ("Prix : 250 000 € honoraires inclus (5 % TTC à la charge de l'acquéreur).", False),
    ("Dépenses d'énergie estimées entre 900 € et 1 300 € par an.", False),
    ("Un conseiller viendra estimer votre bien.", False),
])
def test_valuation_and_loan_figures_are_detected(text, flagged):
    assert real_estate.contains_valuation_or_loan_figure(text) is flagged


@pytest.mark.asyncio
async def test_whatsapp_reply_loses_a_forbidden_request(client, db_session, wire):  # noqa: F811
    tenant = await _agency(db_session)
    wire({"intents": ["AUTRE"], "objections": []}, [text_response(
        "Pour louer ce T3, il faut une pièce d'identité et vos bulletins de salaire.\n"
        "Envoyez-moi aussi vos 3 derniers relevés de compte.\nQuand souhaitez-vous visiter ?")])
    response = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "33611111111", "Il faut quoi pour louer ?"))
    reply = response.json()["ai_reply"]
    assert "relevés de compte" not in reply and reply.startswith("Pour louer ce T3") and "visiter" in reply
    traces = (await db_session.execute(select(Message.content).where(Message.message_type == "legal_removed"))).scalars().all()
    assert traces == ["Ligne retirée de la réponse de Bob : pièce interdite au dossier locataire."]
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal.taxonomy_version))).scalars().all() == ["v1.5immo"]


@pytest.mark.asyncio
async def test_whatsapp_valuation_is_replaced_and_transferred(client, db_session, wire):  # noqa: F811
    tenant = await _agency(db_session)
    wire({"intents": ["DEMANDE_PRIX"], "objections": []}, [text_response("Votre maison vaut environ 320 000 €.")])
    response = await client.post("/webhooks/whatsapp", json=_payload(tenant.pnid, "33622222222", "Ma maison vaut combien ?"))
    assert response.json()["ai_reply"] == real_estate.VALUATION_MESSAGE
    conversation = (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant.id)
                                             .execution_options(populate_existing=True))).scalar_one()
    assert conversation.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_other_sectors_keep_their_replies(client, db_session, wire):  # noqa: F811
    store = await _agency(db_session, business_type=ONLINE_STORE)
    db_session.add(Product(tenant_id=store.id, sku="M93", name="Maison", price=300000, currency="EUR", stock_quantity=1))  # lot 93 : un prix annoncé par Bob existe dans le catalogue
    await db_session.commit()
    text = "Envoyez-moi vos relevés de compte, et votre maison vaut 300 000 €."
    wire({"intents": ["AUTRE"], "objections": []}, [text_response(text)])
    response = await client.post("/webhooks/whatsapp", json=_payload(store.pnid, "33633333333", "Bonjour"))
    assert response.json()["ai_reply"] == text


# --- Consignes de Bob --------------------------------------------------------------------------------------

def test_prompt_for_an_agency():
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", business_type=REAL_ESTATE,
                    real_estate_profile={"status": "MANDATAIRE", "network": "iad France", "activities": ["VENTE"],
                                         "card": "RSAC Lyon 123", "fees_url": "https://parc.fr/honoraires"},
                    privacy_policy_url="https://parc.fr/rgpd")
    prompt = build_system_prompt(tenant, [], "")
    assert "l'assistant virtuel de Agence du Parc, un conseiller immobilier indépendant du réseau iad France" in prompt
    assert "Activités : vente" in prompt and "Barème des honoraires : https://parc.fr/honoraires" in prompt
    assert "Politique de confidentialité : https://parc.fr/rgpd" in prompt
    for marker in ("update_property_project", "kind ESTIMATION", "dis TOUJOURS les honoraires", "3 derniers bulletins",
                   "Ne demande JAMAIS d'autre pièce", "Ne pose JAMAIS de question sur l'origine",
                   "Crédit immobilier : ne donne JAMAIS de chiffre", "CALENDRIER (heure de l'agence)"):
        assert marker in prompt, marker
    agency = Tenant(name="Agence B", country="FR", currency="EUR", business_type=REAL_ESTATE)
    assert "une agence immobilière." in build_system_prompt(agency, [], "")
    assert "Activités : vente, location, gestion locative" in build_system_prompt(agency, [], "")
    store = build_system_prompt(Tenant(name="B", country="SN", currency="XOF", business_type=ONLINE_STORE), [], "")
    assert "update_property_project" not in store and "INFORMATIONS DE L'AGENCE" not in store


def test_no_commerce_strategy_for_an_agency():
    from app.agents.classifier import build_classifier_prompt, normalize_classification
    from app.agents.strategies import no_knowledge_instruction, priority_for, select_strategy
    from app.agents.taxonomy import objection_label, objections_for, taxonomy_version

    chosen, _ = select_strategy(["PRIX_TROP_ELEVE", "HESITATION"], set(), business_type=REAL_ESTATE)
    assert chosen.activity == REAL_ESTATE and chosen.code.startswith("IMMO_PRIX_")  # lot 79 B : jamais celles du commerce
    assert priority_for(REAL_ESTATE)[0] == "CONFIANCE" and "HONORAIRES" in priority_for(REAL_ESTATE)
    assert "l'agence" in no_knowledge_instruction(REAL_ESTATE)
    assert taxonomy_version(REAL_ESTATE) == "v1.5immo" and len(taxonomy_version(REAL_ESTATE)) <= 8
    assert set(objections_for(REAL_ESTATE)) == {"PRIX_TROP_ELEVE", "HONORAIRES", "FINANCEMENT", "DOSSIER", "CONFIANCE",
                                                "DELAI", "RUPTURE_STOCK", "HESITATION"}
    assert objection_label("HONORAIRES") == "Honoraires d'agence"
    assert "agence immobilière" in build_classifier_prompt(REAL_ESTATE)
    assert normalize_classification({"intents": ["AUTRE"], "objections": ["FRAIS_LIVRAISON", "DOSSIER"]},
                                    REAL_ESTATE)["objections"] == ["DOSSIER"]


@pytest.mark.asyncio
async def test_handoff_rules_for_an_agency(db_session):
    from app.services.handoff_rules import decide_turn

    tenant = await _agency(db_session)
    discount = await decide_turn(db_session, tenant, {"intents": ["DEMANDE_REMISE"], "objections": [], "offered_amount": 400000})
    assert discount.rule == "DEALER_PRICE"  # une offre se discute avec un conseiller, jamais « prix fixes »
    conditions = await decide_turn(db_session, tenant, {"intents": ["CONDITIONS_VENTE"], "objections": []})
    assert conditions.rule != "MISSING_CONDITIONS"
    store = await _agency(db_session, business_type=ONLINE_STORE)
    assert (await decide_turn(db_session, store, {"intents": ["CONDITIONS_VENTE"], "objections": []})).rule == "MISSING_CONDITIONS"


# --- Rendez-vous ------------------------------------------------------------------------------------------

def test_appointment_messages_for_an_agency():
    from zoneinfo import ZoneInfo

    from app.services.appointment_outcome import email_text, whatsapp_text
    from app.services.appointment_service import confirmation_message

    zone = ZoneInfo("Europe/Paris")
    when = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
    estimation = AppointmentRequest(kind="ESTIMATION", vehicle_label="maison à Villeurbanne", scheduled_at=when)
    assert confirmation_message(estimation, "Agence du Parc", zone).startswith(
        "Bonjour ! Votre rendez-vous d'estimation (maison à Villeurbanne) est confirmé le samedi 24 octobre")
    agence = AppointmentRequest(kind="AGENCE", scheduled_at=when)
    assert "Votre rendez-vous à l'agence est confirmé" in confirmation_message(agence, "Agence du Parc", zone)
    visit = AppointmentRequest(kind="VISITE", vehicle_label="T3 Lyon 3e", outcome="FOLLOW_UP")
    text = whatsapp_text(visit, "Agence du Parc", REAL_ESTATE)
    assert "questions sur T3 Lyon 3e" in text and "essai" not in text and "véhicule" not in text
    call = AppointmentRequest(kind="APPEL", outcome="NO_SHOW")
    assert whatsapp_text(call, "Agence du Parc", REAL_ESTATE).startswith("Bonjour, nous n'avons pas réussi à vous joindre")
    assert "assurance" not in whatsapp_text(AppointmentRequest(kind="APPEL", outcome="FOLLOW_UP"), "A", REAL_ESTATE)
    subject, body = email_text(visit, "Agence du Parc", business_type=REAL_ESTATE)
    assert subject == "Merci pour notre rendez-vous — Agence du Parc" and "nouvelle visite" in body
    # Concession et courtier inchangés.
    assert "essai" in whatsapp_text(AppointmentRequest(kind="VISITE", outcome="FOLLOW_UP"), "Garage", CAR_DEALERSHIP)


@pytest.mark.asyncio
async def test_visit_request_for_a_property(db_session):
    tenant = await _agency(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    t3 = _bien(tenant, "B1", "T3 lumineux", 1150, "APPARTEMENT", {"transaction": "Location"})
    db_session.add(t3)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=REAL_ESTATE)
    assert "invalide" in (await executor.execute("request_appointment", {"kind": "ESSAI", "availability": "samedi"}))["error"]
    result = await executor.execute("request_appointment", {"kind": "VISITE", "product_id": str(t3.id), "availability": "demain matin"})
    assert "error" not in result
    appointment = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert appointment.kind == "VISITE" and appointment.vehicle_label == "T3 lumineux"


# --- Profil de l'agence, produits, import -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_agency_profile_api(client, db_session):
    tenant = await _agency(db_session)
    headers = await _headers(client, tenant)
    empty = (await client.get("/api/v1/tenants/me/real-estate-profile", headers=headers)).json()
    assert empty["activities"] == ["VENTE", "LOCATION", "GESTION"] and "une agence immobilière" in empty["preview"]
    saved = await client.put("/api/v1/tenants/me/real-estate-profile", headers=headers, json={
        "status": "AGENCE", "activities": ["LOCATION", "VENTE"], "card": "  Carte T n° CPI 6901  ", "rsac": "RSAC 1",
        "guarantee": "Galian, 120 000 €", "fees_url": "parc.fr/honoraires", "privacy_policy_url": "https://parc.fr/rgpd"})
    body = saved.json()
    assert saved.status_code == 200 and body["activities"] == ["VENTE", "LOCATION"] and body["card"] == "Carte T n° CPI 6901"
    assert "rsac" not in body and body["fees_url"] == "https://parc.fr/honoraires"
    await db_session.refresh(tenant)
    assert tenant.privacy_policy_url == "https://parc.fr/rgpd" and tenant.real_estate_profile["status"] == "AGENCE"
    bad = await client.put("/api/v1/tenants/me/real-estate-profile", headers=headers, json={"status": "NOTAIRE"})
    assert bad.status_code == 422
    bad = await client.put("/api/v1/tenants/me/real-estate-profile", headers=headers, json={"activities": ["SYNDIC"]})
    assert bad.status_code == 422
    store = await _agency(db_session, business_type=ONLINE_STORE)
    assert (await client.get("/api/v1/tenants/me/real-estate-profile", headers=await _headers(client, store))).status_code == 403
    onboarding = (await client.get("/api/v1/tenants/me/onboarding", headers=headers)).json()
    labels = {s["key"]: s["label"] for s in onboarding["steps"]}
    assert labels["products"] == "Ajouter vos biens" and onboarding["steps"][4]["key"] == "agency"
    assert onboarding["steps"][4]["done"] is True


@pytest.mark.asyncio
async def test_property_sheets_api(client, db_session):
    tenant = await _agency(db_session)
    headers = await _headers(client, tenant)
    sheets = (await client.get("/api/v1/products/sheets", headers=headers)).json()
    assert sheets["real_estate"] is True and sheets["categories"][0]["code"] == "APPARTEMENT"
    created = await client.post("/api/v1/products", headers=headers, json={
        "sku": "B1", "name": "Villa avec jardin", "price": 435000, "currency": "EUR", "stock_quantity": 1,
        "sheet": {"transaction": "vente", "rooms": "5", "city": "Villeurbanne"}})
    assert created.status_code == 201
    assert created.json()["bob_category"] == "MAISON"
    assert created.json()["sheet"] == {"transaction": "Vente", "rooms": 5, "city": "Villeurbanne"}
    wrong = await client.post("/api/v1/products", headers=headers, json={
        "sku": "B2", "name": "T2", "price": 900, "currency": "EUR", "bob_category": "FASHION"})
    assert wrong.status_code == 422
    store = await _agency(db_session, business_type=ONLINE_STORE)
    store_sheets = (await client.get("/api/v1/products/sheets", headers=await _headers(client, store))).json()
    assert store_sheets["real_estate"] is False and store_sheets["categories"][0]["code"] == "FASHION"


@pytest.mark.asyncio
async def test_demo_imports_properties_with_their_sheet(client, db_session):
    sample = DEMO_HTML.split("REAL_ESTATE: {", 1)[1].split("sample: `", 1)[1].split("`", 1)[0]
    files = {"file": ("biens.csv", io.BytesIO(sample.encode()), "text/csv")}
    response = await client.post("/api/v1/demo/create", data={"company_name": "Agence Exemple", "currency": "EUR",
                                                               "country": "FR", "business_type": REAL_ESTATE}, files=files)
    assert response.status_code == 200 and response.json()["imported"] == 6
    products = {p.name: p for p in (await db_session.execute(select(Product))).scalars()}
    t3 = products["T3 lumineux avec balcon"]
    assert t3.bob_category == "APPARTEMENT" and t3.category_id is None
    assert t3.sheet == {"transaction": "Location", "city": "Lyon 3e", "surface": 68, "rooms": 3, "dpe": "C",
                        "fees": "Honoraires locataire : 12 €/m²"}
    assert products["Terrain constructible"].bob_category == "TERRAIN"
    assert products["Local commercial en angle"].bob_category == "LOCAL"
    tenant = (await db_session.execute(select(Tenant).where(Tenant.business_type == REAL_ESTATE))).scalar_one()
    from app.models.appointment_settings import TenantAppointmentSettings

    assert (await db_session.execute(select(TenantAppointmentSettings).where(
        TenantAppointmentSettings.tenant_id == tenant.id))).scalar_one().online_booking is True


@pytest.mark.asyncio
async def test_csv_without_property_columns_guesses_type_and_keeps_a_sheet(db_session):
    from app.services.catalog_import import import_catalog_csv

    tenant = await _agency(db_session)
    csv_text = "SKU,NAME,DESCRIPTION,PRICE,CURRENCY,STOCK\nB1,Appartement T2 à louer,Proche gare,780,EUR,1\n"
    result = await import_catalog_csv(db_session, tenant.id, csv_text, with_properties=True)
    assert result.imported == 1
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.bob_category == "APPARTEMENT" and product.sheet == {"transaction": "Location"}
    product.sheet = {"transaction": "Location", "rooms": 2}
    await db_session.commit()
    await import_catalog_csv(db_session, tenant.id, csv_text.replace("780", "800"), with_properties=True)
    await db_session.refresh(product)
    assert product.sheet == {"transaction": "Location", "rooms": 2} and float(product.price) == 800
    # Un bien sans type (créé avant le choix du secteur) reçoit un type, sa fiche reste.
    product.bob_category = None
    await db_session.commit()
    await import_catalog_csv(db_session, tenant.id, csv_text, with_properties=True)
    await db_session.refresh(product)
    assert product.bob_category == "APPARTEMENT" and product.sheet == {"transaction": "Location", "rooms": 2}


# --- Autour du secteur : inscription, prospection, accueil -------------------------------------------------

def test_sector_everywhere():
    from app.services import launch_kit, preregistration, prospect_mailer, prospection
    from app.api.prospects.routes import PREVIEW_TEXTS
    from app.services.wa_templates import CATALOG

    assert preregistration.content(REAL_ESTATE)["products_label"] == "Liste de vos biens"
    home = preregistration.example_home(REAL_ESTATE, "Claire", "EUR")
    assert home["funnel"][-1]["label"] == "Affaires conclues" and home["business_type"] == REAL_ESTATE
    assert prospection.SECTORS[REAL_ESTATE] == "Agence immobilière" and prospection.sector_from_slug("immobilier") == REAL_ESTATE
    assert (ROOT / "static" / "visuels" / prospection.VISUALS[REAL_ESTATE]).exists()
    assert len(prospect_mailer.DEFAULTS[REAL_ESTATE]) == 3 and REAL_ESTATE in PREVIEW_TEXTS
    assert launch_kit.GREETINGS[REAL_ESTATE] == "Bonjour, j'ai un projet immobilier"
    assert REAL_ESTATE in CATALOG["rdv_rappel"]["sectors"] and REAL_ESTATE in CATALOG["rdv_absence"]["sectors"]
    alert = preregistration.alert_email(Tenant(name="Agence", business_type=REAL_ESTATE, country="FR", email="a@b.fr"), None)
    assert "(Agence immobilière, FR)" in alert[1]


def test_dashboard_and_demo_know_the_sector():
    for marker in ('["CAR_DEALERSHIP", "INSURANCE_BROKER", "REAL_ESTATE"].includes(currentBusinessType)',
                   'id="estate-profile-card"', "loadRealEstateProfile()", '"/api/v1/tenants/me/real-estate-profile"',
                   'productsLabel = insurance ? "Produits d\'assurance" : estate ? "Biens" : "Produits"',
                   "Affaire conclue (vente, location, mandat)", "f.options", 'class="input sheets-only"',
                   'code === "REAL_ESTATE" ? "Ex. Agence du Parc"', "Affaires conclues"):
        assert marker in HTML, marker
    assert "chooseSector('REAL_ESTATE')" in DEMO_HTML and 'immobilier: "REAL_ESTATE"' in DEMO_HTML
    superadmin = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
    assert 'REAL_ESTATE: "Agence immobilière"' in superadmin


@pytest.mark.asyncio
async def test_home_and_report_words_for_an_agency(db_session):
    from app.services.home_service import home_summary

    tenant = await _agency(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id,
                                      kind="VISITE", availability="samedi", status="CONFIRMED",
                                      scheduled_at=datetime.now(timezone.utc) - timedelta(days=1)))
    await db_session.commit()
    home = await home_summary(db_session, tenant.id)
    assert [f["label"] for f in home["funnel"]][-2:] == ["Rendez-vous honorés", "Affaires conclues"]
    outcome = next(t for t in home["todo"] if t["kind"] == "OUTCOME")
    assert outcome["detail"].startswith("Conclu, à relancer")
