"""
Lot 26 — fiches véhicules : caractéristiques normalisées sur le produit, saisies dans le
tableau de bord ou importées en CSV, transmises à Bob telles quelles, filtrables par Bob.
"""
import uuid

import pytest
from sqlalchemy import select

from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE, tools_for
from app.services.catalog_import import import_catalog_csv
from app.services.vehicle import for_ai, matches, normalize_vehicle, summary, vehicle_from_csv_row


# --- Normalisation ------------------------------------------------------------------------------

def test_values_are_normalised_from_what_merchants_type():
    vehicle, errors = normalize_vehicle({
        "brand": " Peugeot ", "model": "3008", "year": "2021", "mileage_km": "48 000 km",
        "fuel": "Gazole", "gearbox": "BVA", "color": "Gris",
    })
    assert errors == []
    assert vehicle == {"brand": "Peugeot", "model": "3008", "year": 2021, "mileage_km": 48000,
                       "fuel": "DIESEL", "gearbox": "AUTOMATIQUE", "color": "Gris"}
    assert normalize_vehicle({"fuel": "Électrique", "gearbox": "manuelle"})[0] == {"fuel": "ELECTRIQUE", "gearbox": "MANUELLE"}


@pytest.mark.parametrize("raw, message", [
    ({"year": "1890"}, "Année invalide"),
    ({"year": "demain"}, "Année invalide"),
    ({"mileage_km": "-5"}, "Kilométrage invalide"),
    ({"mileage_km": "3000000"}, "Kilométrage invalide"),
    ({"fuel": "charbon"}, "Carburant inconnu"),
    ({"gearbox": "semi"}, "Boîte inconnue"),
    ({"price_hint": "x"}, "Caractéristique inconnue"),
])
def test_doubtful_values_are_refused_with_a_readable_reason(raw, message):
    vehicle, errors = normalize_vehicle(raw)
    assert any(message in e for e in errors)


def test_empty_sheet_is_no_sheet():
    assert normalize_vehicle(None) == (None, [])
    assert normalize_vehicle({"brand": "  ", "fuel": ""}) == (None, [])


def test_summary_and_what_bob_receives():
    vehicle = {"year": 2021, "mileage_km": 48000, "fuel": "DIESEL", "gearbox": "AUTOMATIQUE", "brand": "Peugeot"}
    assert summary(vehicle) == "2021 · 48 000 km · Diesel · Automatique"
    assert for_ai(vehicle) == {"Marque": "Peugeot", "Année": 2021, "Kilométrage": "48 000 km",
                               "Carburant": "Diesel", "Boîte": "Automatique"}


def test_search_criteria_exclude_vehicles_without_the_information():
    car = {"year": 2021, "mileage_km": 48000, "fuel": "DIESEL", "gearbox": "AUTOMATIQUE"}
    assert matches(car, fuel="diesel", gearbox="AUTOMATIQUE", min_year=2020, max_mileage_km=50000)
    assert not matches(car, max_mileage_km=40000)
    assert not matches(car, fuel="ESSENCE")
    assert not matches({}, min_year=2015)  # année inconnue : jamais présenté comme conforme
    assert matches(None)  # aucun critère : tout passe


# --- Import CSV --------------------------------------------------------------------------------

def test_csv_headers_in_french_or_english():
    assert vehicle_from_csv_row({"Année": "2021", "KILOMETRAGE": "48000", "Boîte": "auto", "NAME": "x"}) == {
        "year": "2021", "mileage_km": "48000", "gearbox": "auto"}
    assert vehicle_from_csv_row({"YEAR": "2019", "FUEL": "petrol"}) == {"year": "2019", "fuel": "petrol"}


async def _tenant(db_session, email, business_type=CAR_DEALERSHIP):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    return tenant


CSV = (
    "SKU,NAME,PRICE,CURRENCY,STOCK,MARQUE,MODELE,ANNEE,KILOMETRAGE,CARBURANT,BOITE\n"
    "P3008,Peugeot 3008 GT,15000000,XOF,1,Peugeot,3008,2021,48 000,Diesel,Automatique\n"
    "CLIO,Renault Clio,6500000,XOF,1,Renault,Clio,2017,90000,Essence,Manuelle\n"
    "BAD,Voiture douteuse,1000000,XOF,1,,,1890,,Charbon,\n"
)


@pytest.mark.asyncio
async def test_csv_import_fills_vehicle_sheets_and_refuses_doubtful_rows(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)

    result = await import_catalog_csv(db_session, tenant.id, CSV)

    assert (result.imported, result.failed) == (2, 1)
    assert "Ligne 4 (BAD)" in result.errors[0] and "Année invalide" in result.errors[0] and "Carburant inconnu" in result.errors[0]
    cars = {p.sku: p.vehicle for p in (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars()}
    assert cars["P3008"] == {"brand": "Peugeot", "model": "3008", "year": 2021, "mileage_km": 48000,
                             "fuel": "DIESEL", "gearbox": "AUTOMATIQUE"}
    assert cars["CLIO"]["fuel"] == "ESSENCE"


@pytest.mark.asyncio
async def test_csv_without_vehicle_columns_keeps_existing_sheets(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    await import_catalog_csv(db_session, tenant.id, CSV)

    result = await import_catalog_csv(db_session, tenant.id, "SKU,NAME,PRICE,CURRENCY,STOCK\nP3008,Peugeot 3008 GT,14500000,XOF,1\n")

    assert result.updated == 1
    car = (await db_session.execute(select(Product).where(Product.sku == "P3008", Product.tenant_id == tenant.id))).scalar_one()
    assert float(car.price) == 14500000 and car.vehicle["year"] == 2021


# --- API produits ------------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_product_api_saves_normalised_sheet_and_refuses_doubtful_values(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    headers = await _headers(client, unique_email)
    base = {"sku": "P3008", "name": "Peugeot 3008", "price": 15000000, "currency": "XOF", "stock_quantity": 1}

    bad = await client.post("/api/v1/products", json={**base, "vehicle": {"year": "1890"}}, headers=headers)
    created = await client.post("/api/v1/products", json={**base, "vehicle": {"year": "2021", "fuel": "gazole"}}, headers=headers)

    assert bad.status_code == 422 and "Année invalide" in bad.json()["detail"]
    assert created.status_code == 201 and created.json()["vehicle"] == {"year": 2021, "fuel": "DIESEL"}
    pid = created.json()["id"]
    updated = await client.put(f"/api/v1/products/{pid}", json={"vehicle": {"year": 2021, "mileage_km": "48 000"}}, headers=headers)
    assert updated.json()["vehicle"] == {"year": 2021, "mileage_km": 48000}
    cleared = await client.put(f"/api/v1/products/{pid}", json={"vehicle": None}, headers=headers)
    assert cleared.json()["vehicle"] is None
    untouched = await client.put(f"/api/v1/products/{pid}", json={"stock_quantity": 2}, headers=headers)
    assert untouched.json()["vehicle"] is None and untouched.json()["stock_quantity"] == 2


# --- Bob ----------------------------------------------------------------------------------------

def test_dealership_search_tool_accepts_vehicle_criteria_online_store_does_not():
    dealer = next(t for t in tools_for(CAR_DEALERSHIP, TOOL_DEFINITIONS) if t["name"] == "search_products")
    store = next(t for t in tools_for(ONLINE_STORE, TOOL_DEFINITIONS) if t["name"] == "search_products")
    assert {"fuel", "gearbox", "min_year", "max_mileage_km"} <= set(dealer["input_schema"]["properties"])
    assert not {"fuel", "gearbox", "min_year", "max_mileage_km"} & set(store["input_schema"]["properties"])
    assert dealer["input_schema"]["required"] == [] and store["input_schema"]["required"] == ["query"]
    # La définition partagée n'est jamais modifiée par la copie concession.
    original = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_products")
    assert "fuel" not in original["input_schema"]["properties"]


async def _stock(db_session, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db_session.add(conversation)
    cars = [
        Product(tenant_id=tenant.id, sku="A", name="Peugeot 3008", price=15000000, currency="XOF", stock_quantity=1,
                vehicle={"year": 2021, "mileage_km": 48000, "fuel": "DIESEL", "gearbox": "AUTOMATIQUE"}),
        Product(tenant_id=tenant.id, sku="B", name="Renault Clio", price=6500000, currency="XOF", stock_quantity=1,
                vehicle={"year": 2017, "mileage_km": 90000, "fuel": "ESSENCE", "gearbox": "MANUELLE"}),
        Product(tenant_id=tenant.id, sku="C", name="Toyota Corolla", price=9000000, currency="XOF", stock_quantity=1),
    ]
    db_session.add_all(cars)
    await db_session.commit()
    return conversation


@pytest.mark.asyncio
async def test_bob_filters_vehicles_and_receives_their_sheet(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    conversation = await _stock(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    diesel = await executor.execute("search_products", {"fuel": "DIESEL", "max_mileage_km": 60000})
    everything = await executor.execute("search_products", {"query": ""})
    too_strict = await executor.execute("search_products", {"min_year": 2024})

    assert [r["name"] for r in diesel["results"]] == ["Peugeot 3008"]
    assert diesel["results"][0]["vehicle"]["Kilométrage"] == "48 000 km"
    assert {r["name"] for r in everything["results"]} == {"Peugeot 3008", "Renault Clio", "Toyota Corolla"}
    assert "vehicle" not in next(r for r in everything["results"] if r["name"] == "Toyota Corolla")
    assert too_strict["results"] == []


@pytest.mark.asyncio
async def test_invalid_criteria_are_reported_not_crashing(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    conversation = await _stock(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    result = await executor.execute("search_products", {"min_year": "récente"})
    assert "Critères de recherche invalides" in result["error"]


@pytest.mark.asyncio
async def test_online_store_ignores_vehicle_criteria(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, business_type=ONLINE_STORE)
    conversation = await _stock(db_session, tenant)
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    result = await executor.execute("search_products", {"query": "", "fuel": "DIESEL"})
    assert len(result["results"]) == 3


# --- Lot 26b : incident du 29/09 (« un SUV ? » → « je n'en ai pas ») ---------------------------

def test_body_type_is_normalised_and_shown():
    vehicle, errors = normalize_vehicle({"body_type": "4x4", "year": 2022})
    assert errors == [] and vehicle == {"body_type": "SUV", "year": 2022}
    assert normalize_vehicle({"body_type": "Crossover"})[0] == {"body_type": "SUV"}
    assert "Carrosserie inconnue" in normalize_vehicle({"body_type": "soucoupe"})[1][0]
    assert summary({"body_type": "SUV", "year": 2022, "fuel": "DIESEL"}) == "SUV · 2022 · Diesel"
    assert vehicle_from_csv_row({"CARROSSERIE": "suv"}) == {"body_type": "suv"}


def test_customer_words_become_criteria():
    from app.services.vehicle import criteria_from_query

    assert criteria_from_query("SUV") == (None, {"body_type": "SUV"})
    assert criteria_from_query("un SUV diesel automatique") == (None, {"body_type": "SUV", "fuel": "DIESEL", "gearbox": "AUTOMATIQUE"})
    assert criteria_from_query("Peugeot 5008") == ("Peugeot 5008", {})
    assert criteria_from_query("5008 SUV") == ("5008", {"body_type": "SUV"})
    assert criteria_from_query("") == ("", {})


@pytest.mark.asyncio
async def test_incident_suv_request_finds_the_5008(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    conversation = await _stock(db_session, tenant)
    db_session.add(Product(tenant_id=tenant.id, sku="D", name="Peugeot 5008", price=19000000, currency="XOF", stock_quantity=1,
                           vehicle={"body_type": "SUV", "year": 2022, "fuel": "DIESEL"}))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)

    by_word = await executor.execute("search_products", {"query": "SUV"})
    by_field = await executor.execute("search_products", {"query": "", "body_type": "SUV"})
    no_suv_in_diesel_essence = await executor.execute("search_products", {"query": "SUV essence"})

    assert [r["name"] for r in by_word["results"]] == ["Peugeot 5008"]
    assert [r["name"] for r in by_field["results"]] == ["Peugeot 5008"]
    assert by_word["results"][0]["vehicle"]["Carrosserie"] == "SUV"
    assert no_suv_in_diesel_essence["results"] == []


@pytest.mark.asyncio
async def test_search_also_looks_in_the_description(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    conversation = await _stock(db_session, tenant)
    db_session.add(Product(tenant_id=tenant.id, sku="E", name="Dacia Jogger", description="Familiale 7 places", price=1,
                           currency="XOF", stock_quantity=1))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=CAR_DEALERSHIP)
    result = await executor.execute("search_products", {"query": "7 places"})
    assert [r["name"] for r in result["results"]] == ["Dacia Jogger"]


def test_online_store_search_words_are_never_turned_into_criteria():
    """En boutique en ligne, « diesel » peut être un nom de produit (parfum, vêtement) : inchangé."""
    import inspect

    from app.agents.tools import ToolExecutor as TE

    source = inspect.getsource(TE._tool_search_products)
    assert "if dealership:" in source and "criteria_from_query(query)" in source


def test_prompt_asks_to_look_at_the_whole_stock_before_saying_no():
    from app.agents.prompts import DEALERSHIP_RULES

    assert "relance-la sans query pour voir\n   tout le stock avant de dire au client qu'il n'y a rien" in DEALERSHIP_RULES
    assert "carrosserie (SUV, berline…)" in DEALERSHIP_RULES


@pytest.mark.asyncio
async def test_online_store_still_finds_a_product_named_like_a_criterion(db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, business_type=ONLINE_STORE)
    conversation = await _stock(db_session, tenant)
    db_session.add(Product(tenant_id=tenant.id, sku="PARF", name="Parfum Diesel Only The Brave", price=1, currency="XOF", stock_quantity=3))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    result = await executor.execute("search_products", {"query": "Diesel"})
    assert [r["name"] for r in result["results"]] == ["Parfum Diesel Only The Brave"]
