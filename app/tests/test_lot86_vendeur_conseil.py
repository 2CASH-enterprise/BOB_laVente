"""
Lot 86 — Bob vendeur conseil (demande du 20/10) :
- fiche client qui s'enrichit au fil des échanges et des commandes (rayon, tailles, couleurs, occasion, quartier,
  adresse, paiement), visible du commerçant ;
- méthode de vente : comprendre (3 questions au plus, une à la fois), présenter jusqu'à 10 articles en stock dans la
  taille, rebondir, conclure avec l'adresse connue ;
- collecte naturelle mais jamais cachée : une phrase d'information, une fois ; « oubliez-moi » efface tout.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.agents.tools import ToolExecutor
from app.main import app
from app.models.customer import Customer
from app.models.customer_product_view import CustomerProductView
from app.models.product import Product
from app.services import customer_profile
from app.services.product_sheets import fits_size
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


# --- Fiche client -------------------------------------------------------------------------------------------------

def test_profile_merges_without_forgetting_and_lists_in_order():
    prefs = customer_profile.merge({}, {"section": "femme", "clothing_size": " M ", "occasions": "mariage",
                                        "budget_max": "45000", "unknown": "x"})
    prefs = customer_profile.merge(prefs, {"shoe_size": "39", "occasions": "travail", "colors": "bordeaux, noir"})
    prefs = customer_profile.merge(prefs, {"occasions": "Mariage", "clothing_size": ""})  # doublon, vide : ignorés
    assert prefs == {"section": "femme", "clothing_size": "M", "occasions": ["mariage", "travail"], "budget_max": 45000.0,
                     "shoe_size": "39", "colors": "bordeaux, noir"}
    assert customer_profile.lines(prefs, "Cocody Riviera 3, villa 12") == [
        ("Rayon", "femme"), ("Taille de vêtement", "M"), ("Pointure", "39"), ("Couleurs préférées", "bordeaux, noir"),
        ("Occasions d'achat", "mariage, travail"), ("Budget maximum mentionné", "45 000"),
        ("Adresse de livraison", "Cocody Riviera 3, villa 12")]


@pytest.mark.asyncio
async def test_bob_remembers_what_the_client_said_and_uses_it_next_time(db_session, unique_email):
    from app.services.customer_memory_service import build_customer_memory
    from app.tests.test_product_images import _shop

    tenant, conversation = await _shop(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    await executor.execute("update_customer_profile", {"clothing_size": "L", "section": "homme", "district": "Yopougon",
                                                       "delivery_address": "Yopougon Maroc, près de la pharmacie",
                                                       "payment": "Wave"})
    await db_session.commit()
    customer = await db_session.get(Customer, conversation.customer_id)
    assert customer.address == "Yopougon Maroc, près de la pharmacie"
    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    for text in ("Ce que le client t'a déjà dit", "Taille de vêtement : L", "Rayon : homme", "Quartier : Yopougon",
                 "Moyen de paiement préféré : Wave", "Adresse de livraison : Yopougon Maroc"):
        assert text in memory, text


@pytest.mark.asyncio
async def test_last_order_address_becomes_the_usual_address(db_session, unique_email):
    from app.services.order_service import create_order
    from app.tests.test_order_service import _setup

    tenant, product, customer = await _setup(db_session, unique_email)
    await create_order(db=db_session, tenant_id=tenant.id, customer_id=customer.id,
                       items=[{"product_id": str(product.id), "quantity": 1}], delivery_address="  Cocody   Angré 8e tranche ",
                       payment_method="cash", created_by="IA")
    await db_session.commit()
    await db_session.refresh(customer)
    assert customer.address == "Cocody Angré 8e tranche"


# --- Taille en stock ----------------------------------------------------------------------------------------------

def _p(variants=None, sizes=None):
    return Product(name="x", variants=variants, sheet={"sizes": sizes} if sizes else {})


@pytest.mark.parametrize("product, size, fits", [
    (_p(variants=[{"options": {"Taille": "M"}, "stock": 2}, {"options": {"Taille": "L"}, "stock": 0}]), "m", True),
    (_p(variants=[{"options": {"Taille": "M"}, "stock": 2}, {"options": {"Taille": "L"}, "stock": 0}]), "L", False),
    (_p(variants=[{"options": {"Couleur": "Noir"}, "stock": 2}]), "M", None),  # pas de taille dans les variantes
    (_p(sizes="S à XL"), "M", True), (_p(sizes="S à XL"), "XXL", False),
    (_p(sizes="38 à 44"), "42", True), (_p(sizes="38-44"), "45", False),
    (_p(sizes="38, 39 et 41"), "41", True), (_p(sizes="38, 39 et 41"), "40", False),
    (_p(sizes="Unique"), "M", False), (_p(), "M", None), (_p(sizes="S à XL"), "", None),
])
def test_fits_size(product, size, fits):
    assert fits_size(product, size) is fits


@pytest.mark.asyncio
async def test_search_keeps_only_what_exists_in_the_client_size(db_session, unique_email):
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    ok = _product(tenant, "R1", name="Robe soirée rouge")
    ok.sheet = {"sizes": "S à L"}
    too_small = _product(tenant, "R2", name="Robe soirée noire")
    too_small.sheet = {"sizes": "XS, S"}
    unknown = _product(tenant, "R3", name="Robe soirée bleue")
    db_session.add_all([ok, too_small, unknown])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("search_products", {"query": "robe soirée", "size": "M"})
    assert sorted(r["name"] for r in result["results"]) == ["Robe soirée bleue", "Robe soirée rouge"]
    none = await executor.execute("search_products", {"query": "robe soirée noire", "size": "M"})
    assert none["results"] == [] and "taille M" in none["message"]


# --- Méthode de vente ---------------------------------------------------------------------------------------------

def test_sales_method_is_given_to_shops_and_dealerships_only():
    from app.agents.prompts import build_system_prompt
    from app.models.tenant import Tenant

    shop = build_system_prompt(Tenant(name="MS4", country="CI", currency="XOF", business_type="ONLINE_STORE"))
    assert "MÉTHODE DE VENTE (comme un bon vendeur en boutique)" in shop
    for text in ("au plus 3 questions, UNE par message", "jusqu'à 10 articles EN STOCK", "REBONDIR", "toujours à Cocody",
                 "paramètre size", "PRÉSENTER des produits (jusqu'à 10"):
        assert text in shop, text
    dealer = build_system_prompt(Tenant(name="MS4", country="CI", currency="XOF", business_type="CAR_DEALERSHIP"))
    assert "MÉTHODE DE VENTE (comme un bon vendeur en concession)" in dealer and "propose un essai" in dealer
    broker = build_system_prompt(Tenant(name="Cabinet", country="CI", currency="XOF", business_type="INSURANCE_BROKER"))
    assert "MÉTHODE DE VENTE" not in broker


def test_profile_tool_knows_the_new_fields():
    from app.agents.tool_definitions import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "update_customer_profile")
    for key in ("section", "clothing_size", "shoe_size", "colors", "materials", "occasions", "district",
                "delivery_address", "payment"):
        assert key in tool["input_schema"]["properties"], key
    assert "jamais une supposition" in tool["description"] and "sexe" in tool["description"]
    search = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_products")
    assert "size" in search["input_schema"]["properties"]


# --- Information et « oubliez-moi » ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, forget", [
    ("Oubliez-moi", True), ("oubliez moi !", True), ("Supprimez mes données", True), ("efface-moi", True),
    ("Ne m'oubliez pas", False), ("oubliez-moi la robe rouge, je prends la bleue", False), ("STOP", False),
])
def test_forget_keyword(text, forget):
    assert customer_profile.is_forget_request(text) is forget


@pytest.mark.asyncio
async def test_notice_once_when_bob_first_remembers_then_forget_clears_everything(client, db_session, unique_email):
    from app.tests.test_freemium_webhook import _payload, _setup

    tenant = await _setup(db_session, unique_email, "phone_lot86", is_paid=True)
    number = "2250700008601"
    fake = FakeLLMClient([text_response("Bonjour ! Que cherchez-vous ?"),
                          tool_use_response("update_customer_profile", {"clothing_size": "M", "district": "Cocody",
                                                                         "delivery_address": "Cocody Riviera 3"}),
                          text_response("Très bien, taille M. Pour quelle occasion ?"),
                          tool_use_response("update_customer_profile", {"occasions": "mariage"}),
                          text_response("Parfait. Une couleur préférée ?")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        first = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot86", number, "Bonjour"))).json()["ai_reply"]
        second = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot86", number, "Une robe en M, j'habite Cocody"))).json()["ai_reply"]
        third = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot86", number, "Pour un mariage"))).json()["ai_reply"]
        calls = fake.call_count
        customer = (await db_session.execute(select(Customer).where(Customer.tenant_id == tenant.id,
                                                                    Customer.whatsapp_number == number))).scalar_one()
        await db_session.refresh(customer)
        assert customer.address == "Cocody Riviera 3"
        dress = Product(tenant_id=tenant.id, sku="R1", name="Robe", price=1, currency="XOF", stock_quantity=1)
        db_session.add(dress)
        await db_session.flush()
        db_session.add(CustomerProductView(tenant_id=tenant.id, customer_id=customer.id, product_id=dress.id))
        await db_session.commit()
        forgotten = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot86", number, "Oubliez-moi"))).json()["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]
    notice = "Pour mieux vous conseiller, Boutique garde vos préférences (tailles, goûts, adresse)."
    assert notice not in first  # rien de retenu encore
    assert second.endswith("Écrivez « oubliez-moi » à tout moment pour les effacer.") and notice in second
    assert notice not in third  # une seule fois
    assert forgotten == customer_profile.FORGOTTEN_REPLY and fake.call_count == calls  # jamais l'IA
    customer = (await db_session.execute(select(Customer).where(Customer.tenant_id == tenant.id,
                                                                Customer.whatsapp_number == number))).scalar_one()
    await db_session.refresh(customer)
    assert customer.detected_preferences == {} and customer.address is None and customer.privacy_notice_at is not None
    views = (await db_session.execute(select(CustomerProductView).where(CustomerProductView.customer_id == customer.id))).all()
    assert views == []


def test_tutoiement_notice_and_forget_reply():
    assert customer_profile.privacy_line("MS4", tu=True).startswith("Pour mieux te conseiller, MS4 garde tes préférences")
    assert "tes préférences" in customer_profile.FORGOTTEN_REPLY_TU


# --- Tableau de bord ------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merchant_sees_what_bob_knows(client, db_session, unique_email):
    from app.core.security import hash_password
    from app.models.tenant import Tenant
    from app.models.user import Role, User

    tenant = Tenant(name="Boutique", country="CI", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="A", role=Role.OWNER))
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"225{uuid.uuid4().int % 10**10:010d}", address="Cocody",
                        detected_preferences={"clothing_size": "M", "occasions": ["mariage"]})
    db_session.add(customer)
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    r = await client.get(f"/api/v1/customers/{customer.id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["profile_lines"] == [["Taille de vêtement", "M"], ["Occasions d'achat", "mariage"],
                                         ["Adresse de livraison", "Cocody"]]
    assert "Ce que Bob sait du client" in DASH and "c.profile_lines" in DASH and "Préférences détectées par l'IA" not in DASH
