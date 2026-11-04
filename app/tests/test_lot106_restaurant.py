"""
Lot 106 — secteur Restaurant : la carte (rubriques, fiche d'un plat, 14 allergènes, « indisponible aujourd'hui »), la
commande (à emporter, livraison avec frais par commune, sur place depuis la table, horaires de la cuisine) et la
réservation de table (couverts par service). Côte d'Ivoire d'abord.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.order import Order
from app.models.product import Product
from app.services import booking, product_sheets, restaurant
from app.services.business_type import (
    RESTAURANT,
    books_appointments,
    is_appointment_sector,
    tool_allowed,
    tools_for,
)
from app.tests.test_lot53_courtier import _cabinet, _conversation, _headers

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
SERVICES = {str(d): [["12:00", "15:00"], ["19:00", "23:00"]] for d in range(7)}  # midi et soir


async def _resto(db, **fields):
    tenant = await _cabinet(db, business_type=RESTAURANT)
    tenant.name, tenant.currency = "Chez Tante Awa", "XOF"
    tenant.delivery_enabled, tenant.pickup_enabled, tenant.dine_in_enabled = True, True, True
    tenant.delivery_areas = [{"name": "Cocody", "fee": 1000}, {"name": "Yopougon", "fee": 2000}]
    for key, value in fields.items():
        setattr(tenant, key, value)
    await db.commit()
    return tenant


async def _dish(db, tenant, name="Poulet braisé", price=4500, stock=1, code="GRILLADE", sheet=None):
    dish = Product(tenant_id=tenant.id, sku=f"P{uuid.uuid4().hex[:6]}", name=name, price=price, currency="XOF",
                   stock_quantity=stock, bob_category=code, sheet=sheet, active=True)
    db.add(dish)
    await db.commit()
    return dish


# --- Le secteur et ses outils -----------------------------------------------------------------------------------------

def test_a_restaurant_sells_and_books_but_never_haggles():
    assert tool_allowed(RESTAURANT, "create_order") and tool_allowed(RESTAURANT, "share_payment_link")
    assert tool_allowed(RESTAURANT, "request_appointment") and tool_allowed(RESTAURANT, "get_available_slots")
    assert not tool_allowed(RESTAURANT, "negotiate_price") and not tool_allowed(RESTAURANT, "update_prospect_profile")
    assert not tool_allowed(RESTAURANT, "update_property_project") and not tool_allowed(RESTAURANT, "update_insurance_request")
    tools = {t["name"]: t for t in tools_for(RESTAURANT, TOOL_DEFINITIONS)}
    assert tools["create_order"]["input_schema"]["properties"]["fulfillment"]["enum"] == ["EMPORTER", "LIVRAISON", "SUR_PLACE"]
    assert tools["request_appointment"]["input_schema"]["required"] == ["kind", "party_size"]
    assert "vehicle" not in tools["request_appointment"]["input_schema"]["properties"]
    assert "party_size" in tools["get_available_slots"]["input_schema"]["properties"]
    plain = type("T", (), {"business_type": RESTAURANT})()
    assert books_appointments(plain) and not is_appointment_sector(plain)  # vend comme une boutique


# --- La carte --------------------------------------------------------------------------------------------------------

def test_the_dish_sheet_keeps_only_the_14_allergens():
    sheet = product_sheets.normalize_sheet("PLAT", {"composition": "poulet, oignons", "allergens": "lait, gluten, xyz",
                                                    "spicy": "pimenté", "brand": "x", "sides": "attiéké, alloco"})
    assert sheet == {"composition": "poulet, oignons", "allergens": "Gluten, Lait", "spicy": "Pimenté", "sides": "attiéké, alloco"}
    assert product_sheets.clean_allergens(["Aucun des 14"]) == "Aucun des 14"
    assert product_sheets.clean_allergens("Sésame; aucun des 14") == "Sésame"  # jamais « aucun » avec un allergène
    assert product_sheets.clean_allergens("rien") is None
    assert [product_sheets.guess_dish(n) for n in ("Poulet braisé", "Jus de bissap", "Garba", "Menu du jour")] == \
        ["GRILLADE", "BOISSON", "PLAT", "MENU"]
    assert product_sheets.sheet_for_ai(Product(bob_category="PLAT", sheet=sheet))["Allergènes"] == "Gluten, Lait"


@pytest.mark.asyncio
async def test_the_menu_in_the_dashboard(client, db_session):
    tenant = await _resto(db_session)
    headers = await _headers(client, tenant)
    defs = (await client.get("/api/v1/products/sheets", headers=headers)).json()
    assert defs["restaurant"] and [c["code"] for c in defs["categories"]] == ["ENTREE", "PLAT", "GRILLADE", "DESSERT", "BOISSON", "MENU"]
    plat = next(c for c in defs["categories"] if c["code"] == "PLAT")
    allergens = next(f for f in plat["key_fields"] if f["key"] == "allergens")
    assert len(allergens["multi"]) == 15 and "Arachides" in allergens["multi"]
    r = await client.post("/api/v1/products", headers=headers, json={
        "sku": "GARBA", "name": "Garba", "price": 1500, "currency": "XOF", "stock_quantity": 1,
        "sheet": {"composition": "attiéké, thon frit", "allergens": "Poissons"}})
    assert r.status_code == 201, r.text
    garba = r.json()
    assert garba["bob_category"] == "PLAT" and garba["sheet"]["allergens"] == "Poissons"
    assert (await client.post("/api/v1/products", headers=headers, json={
        "sku": "X1", "name": "X", "price": 1, "currency": "XOF", "bob_category": "FASHION"})).status_code == 422
    # Indisponible aujourd'hui : plus proposé, la fiche réenregistrée ne le remet pas en vente, il revient demain.
    r = await client.post(f"/api/v1/products/{garba['id']}/unavailable-today", headers=headers, json={"unavailable": True})
    assert r.json()["stock_quantity"] == 0 and r.json()["sheet"]["unavailable_on"]
    r = await client.put(f"/api/v1/products/{garba['id']}", headers=headers,
                         json={"bob_category": "PLAT", "sheet": {"composition": "attiéké, thon"}})
    assert r.json()["stock_quantity"] == 0 and r.json()["sheet"]["unavailable_on"]
    assert await restaurant.restore_dishes(db_session) == 0  # le jour même : il reste indisponible
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    assert await restaurant.restore_dishes(db_session, now=tomorrow) == 1
    product = await db_session.get(Product, uuid.UUID(garba["id"]))
    await db_session.refresh(product)
    assert product.stock_quantity == 1 and "unavailable_on" not in (product.sheet or {})
    shop = await _cabinet(db_session, business_type="ONLINE_STORE")
    assert (await client.post(f"/api/v1/products/{garba['id']}/unavailable-today", headers=await _headers(client, shop),
                              json={"unavailable": True})).status_code == 403


# --- La commande -----------------------------------------------------------------------------------------------------

def test_order_terms_follow_the_restaurant_rules():
    tenant = type("T", (), {"pickup_enabled": True, "delivery_enabled": True, "dine_in_enabled": False,
                            "delivery_areas": [{"name": "Cocody", "fee": 1000}], "kitchen_hours": None, "country": "CI"})()
    assert restaurant.order_terms(tenant, "livraison", "cocody", None, "Riviera 2, face pharmacie") == {
        "fulfillment": "DELIVERY", "delivery_fee": 1000, "delivery_area": "Cocody", "table_number": None}
    with pytest.raises(restaurant.RestaurantError, match="livre seulement : Cocody"):
        restaurant.order_terms(tenant, "LIVRAISON", "Abobo", None, "Abobo gare")
    with pytest.raises(restaurant.RestaurantError, match="adresse"):
        restaurant.order_terms(tenant, "LIVRAISON", "Cocody", None, "")
    with pytest.raises(restaurant.RestaurantError, match="à emporter ou en livraison"):
        restaurant.order_terms(tenant, "SUR_PLACE", None, "4", None)  # sur place non activé
    tenant.dine_in_enabled = True
    with pytest.raises(restaurant.RestaurantError, match="numéro de la table"):
        restaurant.order_terms(tenant, "SUR_PLACE", None, "", None)
    assert restaurant.order_terms(tenant, "SUR_PLACE", None, " 12 ", None)["table_number"] == "12"
    assert restaurant.order_terms(tenant, "EMPORTER", None, None, None)["fulfillment"] == "PICKUP"


def test_a_closed_kitchen_takes_no_order_and_says_when_it_opens():
    tenant = type("T", (), {"pickup_enabled": True, "delivery_enabled": False, "dine_in_enabled": False, "country": "CI",
                            "delivery_areas": None, "kitchen_hours": {str(d): [["11:00", "15:00"]] for d in range(7)}})()
    morning = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)  # Abidjan = UTC : 8 h
    assert not restaurant.kitchen_open(tenant, morning)
    assert restaurant.kitchen_open(tenant, morning.replace(hour=12))
    with pytest.raises(restaurant.RestaurantError, match="rouvre aujourd'hui à 11 h"):
        restaurant.order_terms(tenant, "EMPORTER", None, None, None, now=morning)
    with pytest.raises(restaurant.RestaurantError, match="rouvre demain à 11 h"):
        restaurant.order_terms(tenant, "EMPORTER", None, None, None, now=morning.replace(hour=20))
    tenant.kitchen_hours = None
    assert restaurant.kitchen_open(tenant, morning) and restaurant.next_opening(tenant, morning) is None


@pytest.mark.asyncio
async def test_bob_orders_with_the_commune_fee_and_the_menu_never_runs_out(db_session):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    off = await _dish(db_session, tenant, name="Kedjenou", stock=0, code="PLAT")
    customer, conversation = await _conversation(db_session, tenant)
    tools = ToolExecutor(db_session, tenant.id, conversation, customer.id, business_type=RESTAURANT)
    result = await tools.execute("create_order", {"items": [{"product_id": str(dish.id), "quantity": 2}],
                                                  "fulfillment": "LIVRAISON", "delivery_area": "Yopougon",
                                                  "delivery_address": "Yopougon Siporex", "payment_method": "à la livraison"})
    assert "error" not in result, result
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert float(order.total_amount) == 11000 and float(order.delivery_fee) == 2000 and order.delivery_area == "Yopougon"
    assert order.fulfillment == "DELIVERY" and order.delivery_address == "Yopougon Siporex"
    await db_session.refresh(dish)
    assert dish.stock_quantity == 1  # la carte ne se vide pas
    result = await tools.execute("create_order", {"items": [{"product_id": str(off.id), "quantity": 1}],
                                                  "fulfillment": "SUR_PLACE", "table_number": "5"})
    assert "pas disponible aujourd'hui" in result["error"]
    result = await tools.execute("create_order", {"items": [{"product_id": str(dish.id), "quantity": 1}], "fulfillment": "LIVRAISON",
                                                  "delivery_area": "Abobo", "delivery_address": "x"})
    assert "Cocody, Yopougon" in result["error"]
    found = await tools.execute("search_products", {"query": "Kedjenou"})
    assert found["results"] == [] and "Pas disponible aujourd'hui : Kedjenou" in found["message"]


# --- La réservation de table ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tables_are_counted_in_covers_per_service(db_session):
    tenant = await _resto(db_session)
    db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=SERVICES,
                                             slot_minutes=30, capacity=10))
    await db_session.commit()
    customer, conversation = await _conversation(db_session, tenant)
    tools = ToolExecutor(db_session, tenant.id, conversation, customer.id, business_type=RESTAURANT)
    assert "pour combien de personnes" in (await tools.execute("request_appointment", {"kind": "TABLE"}))["error"]
    slots = (await tools.execute("get_available_slots", {"party_size": 6}))["slots"]
    result = await tools.execute("request_appointment", {"kind": "TABLE", "party_size": 6, "slot": slots[0]["slot"]})
    assert result.get("status") != "error" and "error" not in result, result
    table = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert table.kind == "TABLE" and table.party_size == 6 and table.vehicle_label == "6 personnes" and table.status == "CONFIRMED"
    from app.services import appointment_service

    assert "Votre table pour 6 personnes est réservée" in appointment_service.confirmation_message(table, tenant.name, timezone.utc)
    # Même service : 6 + 5 > 10 couverts → refusé ; 6 + 4 = 10 → accepté.
    settings = await booking.load_settings(db_session, tenant.id)
    now = datetime.now(timezone.utc)
    start = table.scheduled_at.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    from app.services.local_time import tenant_zone

    zone = tenant_zone(tenant)
    local = start.astimezone(zone)
    assert not await booking.is_bookable(db_session, tenant, settings, zone, now, local, party=5)
    assert await booking.is_bookable(db_session, tenant, settings, zone, now, local, party=4)
    other_service = await booking.free_slots(db_session, tenant, settings, zone, now, day=local.date(), party=8, limit=20)
    assert other_service and all(booking._service_of(s, settings) != booking._service_of(local, settings) for s in other_service)


# --- Ce que Bob sait, les réglages ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bob_knows_the_restaurant(db_session):
    from app.agents.prompts import build_system_prompt

    tenant = await _resto(db_session, kitchen_hours={"0": [["11:00", "23:00"]]})
    prompt = build_system_prompt(tenant)
    assert "un restaurant" in prompt and "RÈGLES (restaurant)" in prompt and "ne dis\n   JAMAIS « il n'y en a pas »" in prompt
    assert "Livraison (frais par commune) : Cocody 1 000 XOF ; Yopougon 2 000 XOF" in prompt
    assert "à emporter, en livraison, sur place (depuis la table)" in prompt and "lundi : 11:00-23:00 ; mardi : fermé" in prompt


@pytest.mark.asyncio
async def test_restaurant_settings(client, db_session):
    tenant = await _resto(db_session, delivery_areas=None, dine_in_enabled=False)
    headers = await _headers(client, tenant)
    body = {"delivery_enabled": True, "pickup_enabled": True, "pickup_address": "Cocody Angré", "dine_in_enabled": True,
            "delivery_areas": [{"name": " Cocody ", "fee": "1000"}, {"name": "Marcory", "fee": 1500}],
            "kitchen_hours": {"0": [["11:00", "15:00"], ["18:00", "23:00"]]}}
    r = await client.put("/api/v1/tenants/me/fulfillment", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["delivery_areas"] == [{"name": "Cocody", "fee": 1000.0}, {"name": "Marcory", "fee": 1500.0}]
    assert r.json()["dine_in_enabled"] and r.json()["kitchen_hours"]["0"][1] == ["18:00", "23:00"]
    bad = await client.put("/api/v1/tenants/me/fulfillment", headers=headers,
                           json={**body, "delivery_areas": [{"name": "Cocody", "fee": 1}, {"name": "cocody", "fee": 2}]})
    assert bad.status_code == 422 and "double" in bad.json()["detail"]
    only_table = await client.put("/api/v1/tenants/me/fulfillment", headers=headers,
                                  json={"delivery_enabled": False, "pickup_enabled": False, "dine_in_enabled": True})
    assert only_table.status_code == 200
    # Couverts par service : jusqu'à 500 pour un restaurant (20 personnes en même temps ailleurs).
    r = await client.put("/api/v1/appointments/settings", headers=headers,
                         json={"online_booking": True, "opening_hours": SERVICES, "slot_minutes": 30, "capacity": 120})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/orders", headers=headers)).status_code == 200
    dealer = await _cabinet(db_session, business_type="CAR_DEALERSHIP")
    r = await client.put("/api/v1/appointments/settings", headers=await _headers(client, dealer),
                         json={"online_booking": True, "opening_hours": SERVICES, "slot_minutes": 30, "capacity": 120})
    assert r.status_code == 422


def test_receipt_lines():
    from app.services.receipt_service import delivery_line

    assert delivery_line(Order(fulfillment="DINE_IN", table_number="7"), "XOF") == "Sur place — table 7"
    assert delivery_line(Order(fulfillment="DELIVERY", delivery_fee=1000, delivery_area="Cocody"), "XOF") == "Livraison (Cocody) — 1 000 XOF"


def test_dashboard():
    for needle in ('body:not(.restaurant) .restaurant-only', 'id="ff-dine-in"', 'id="ff-areas"', 'id="ff-kitchen-hours"',
                   "function dishToday(", "function toggleDishToday(", "/unavailable-today", 'restaurant ? "Ma carte"',
                   'apptNav.dataset.label = restaurant ? "Réservations" : "Rendez-vous"', 'restaurant ? "Couverts par service "',
                   "function readDayRows(", 'class="sheet-multi"', '"ONLINE_STORE", "REAL_ESTATE", "RESTAURANT"'):
        assert needle in HTML, needle
