"""
Lot 107 — parcours à boutons du restaurant : « Commander » (accompagnement obligatoire, mode, commune, table, paiement,
commande pour plus tard) et « Réserver une table » (personnes, jour, service, heure), sans que le client écrive.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.cart import Cart
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services import guided_flow, receipt_service, resto_flow
from app.tests.test_lot53_courtier import _conversation
from app.tests.test_lot106_restaurant import SERVICES, _resto

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


async def _dish(db, tenant, name="Poulet braisé", price=4500, code="GRILLADE", sides=None, stock=1, **sheet):
    if sides:
        sheet["sides"] = sides
    dish = Product(tenant_id=tenant.id, sku=f"P{uuid.uuid4().hex[:6]}", name=name, price=price, currency="XOF",
                   stock_quantity=stock, bob_category=code, sheet=sheet or None, active=True)
    db.add(dish)
    await db.commit()
    return dish


async def _tap(db, tenant, customer, conversation, reply_id):
    turn = await guided_flow.handle(db, tenant, None, f"g:{reply_id}", None, False, customer, conversation)
    await db.commit()
    return turn


async def _say(db, tenant, customer, conversation, text):
    turn = await guided_flow.handle(db, tenant, text, None, None, False, customer, conversation)
    await db.commit()
    return turn


def _ids(action) -> list[str]:
    if action["type"] == "list":
        return [row[0] for row in action["rows"]]
    if action["type"] == "buttons":
        return [b[0] for b in action["buttons"]]
    return [b[0] for card in action["cards"] for b in card["buttons"]]


def _titles(action) -> list[str]:
    if action["type"] == "list":
        return [row[1] for row in action["rows"]]
    return [b[1] for b in action["buttons"]]


def _closed_hours():
    """Cuisine fermée aujourd'hui, ouverte demain de 11 h à 15 h."""
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).weekday()
    return {str(tomorrow): [["11:00", "15:00"]]}


def test_sides_are_read_from_the_dish_sheet():
    assert resto_flow.sides_of(Product(sheet={"sides": "attiéké, alloco ou riz"})) == ["attiéké", "alloco", "riz"]
    assert resto_flow.sides_of(Product(sheet={"sides": "Riz; riz / frites"})) == ["Riz", "frites"]
    assert resto_flow.sides_of(Product(sheet={"sides": "riz"})) == []  # un seul : pas de choix à faire
    assert resto_flow.sides_of(Product(sheet=None)) == []


@pytest.mark.asyncio
async def test_the_welcome_offers_order_table_menu_and_a_person(db_session):
    tenant = await _resto(db_session)
    assert guided_flow.applies(tenant)
    customer, conversation = await _conversation(db_session, tenant)
    turn = await _say(db_session, tenant, customer, conversation, "Bonjour")
    welcome = turn.actions[0]
    assert welcome["type"] == "list" and "Chez Tante Awa" in welcome["body"]
    assert _titles(welcome) == ["Commander", "Réserver une table", "Voir la carte", "Parler au restaurant"]
    human = await _tap(db_session, tenant, customer, conversation, "human")
    assert human.handoff and "restaurant" in human.handoff_text
    assert await _say(db_session, tenant, customer, conversation, "Vous avez du poisson braisé ?") is None  # l'IA répond


@pytest.mark.asyncio
async def test_a_delivery_order_with_a_mandatory_side(db_session):
    tenant = await _resto(db_session, pickup_enabled=False, dine_in_enabled=False)
    tenant.pickup_enabled = True
    await db_session.commit()
    dish = await _dish(db_session, tenant, sides="attiéké, alloco ou riz", composition="poulet, oignons",
                       allergens="Aucun des 14", spicy="Pimenté")
    await _dish(db_session, tenant, name="Bissap", price=500, code="BOISSON", volume="50 cl")
    await _dish(db_session, tenant, name="Kedjenou", code="PLAT", stock=0)  # indisponible aujourd'hui
    customer, conversation = await _conversation(db_session, tenant)

    menu = (await _tap(db_session, tenant, customer, conversation, "fv")).actions[0]
    assert _titles(menu) == ["Grillades", "Boissons"]  # pas « Plats » : le seul plat est indisponible
    listing = (await _tap(db_session, tenant, customer, conversation, "fr:GRILLADE:0")).actions
    assert f"g:fd:{dish.id}" in _ids(listing[0])
    detail = (await _tap(db_session, tenant, customer, conversation, f"fd:{dish.id}")).actions[0]
    assert "Composition : poulet, oignons" in detail["body"] and "Piment : Pimenté" in detail["body"]
    assert "aucun des 14 allergènes majeurs" in detail["body"] and f"g:fa:{dish.id}" in _ids(detail)

    sides = (await _tap(db_session, tenant, customer, conversation, f"fa:{dish.id}")).actions[0]
    assert _titles(sides) == ["attiéké", "alloco", "riz"]
    # Sans accompagnement, la quantité ne suffit pas : le choix revient.
    skipped = (await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:2")).actions[0]
    assert _titles(skipped) == ["attiéké", "alloco", "riz"]
    quantity = (await _tap(db_session, tenant, customer, conversation, f"fs:{dish.id}:1")).actions[0]
    assert "avec alloco" in quantity["body"] and _titles(quantity)[:2] == ["1", "2"]
    cart = (await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:1:2")).actions[0]
    assert "Poulet braisé (Avec alloco) × 2 — 9 000 XOF" in cart["body"]
    assert _ids(cart) == ["g:fo", "g:fv", "g:fx"]

    modes = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert _titles(modes) == ["À emporter", "Livraison"]
    areas = (await _tap(db_session, tenant, customer, conversation, "fm:l")).actions[0]
    assert _titles(areas) == ["Cocody", "Yopougon", "Ma commune n'y est pas"]
    assert areas["rows"][1][2] == "2 000 XOF"
    ask = (await _tap(db_session, tenant, customer, conversation, "fz:1")).actions[0]
    assert ask["type"] == "text" and "adresse de livraison" in ask["body"]
    assert await guided_flow.expects_text(db_session, tenant, customer, "Siporex, près de la pharmacie")
    recap = (await _say(db_session, tenant, customer, conversation, "Siporex, près de la pharmacie")).actions[0]
    # Pas de lien de paiement : « à la livraison » seulement, sans question.
    assert "Livraison (Yopougon) : 2 000 XOF" in recap["body"] and "Adresse : Siporex, près de la pharmacie" in recap["body"]
    assert "Paiement : à la livraison" in recap["body"] and "Total : 11 000 XOF" in recap["body"]
    assert "Dès que possible" in recap["body"]

    done = await _tap(db_session, tenant, customer, conversation, "fw")
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert float(order.total_amount) == 11000 and float(order.delivery_fee) == 2000 and order.delivery_area == "Yopougon"
    assert order.fulfillment == "DELIVERY" and order.payment_method == "À la livraison" and order.scheduled_for is None
    item = (await db_session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
    assert item.variant_label == "Avec alloco" and item.quantity == 2
    assert "Paiement : à la livraison." in done.actions[0]["body"] and "Contactez-nous" not in done.actions[0]["body"]
    assert _ids(done.actions[0]) == ["g:s", "g:fv"]
    await db_session.refresh(dish)
    assert dish.stock_quantity == 1  # la carte ne se vide pas
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert cart.items == [] and cart.meta is None
    again = (await _tap(db_session, tenant, customer, conversation, "fw")).actions[0]  # double appui
    assert "est bien enregistrée" in again["body"]


@pytest.mark.asyncio
async def test_dine_in_asks_the_table_and_a_payment_link_is_offered(db_session):
    tenant = await _resto(db_session, pickup_enabled=False, delivery_enabled=False, payment_link="https://pay.example/awa")
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    ask = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]  # un seul mode : pas de question
    assert ask["type"] == "text" and "numéro de ta table" not in ask["body"] and "numéro de votre table" in ask["body"]
    pay = (await _say(db_session, tenant, customer, conversation, "Table 7")).actions[0]
    assert _titles(pay) == ["Payer sur place", "Payer par lien"]
    recap = (await _tap(db_session, tenant, customer, conversation, "fp:r")).actions[0]
    assert "Sur place, table 7" in recap["body"] and "Paiement : au restaurant" in recap["body"]
    bad = (await _tap(db_session, tenant, customer, conversation, "fp:d")).actions[0]  # « à la livraison » : pas sur place
    assert _titles(bad) == ["Payer sur place", "Payer par lien"]
    await _tap(db_session, tenant, customer, conversation, "fp:k")
    done = (await _tap(db_session, tenant, customer, conversation, "fw")).actions[0]
    assert "https://pay.example/awa" in done["body"]
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert order.fulfillment == "DINE_IN" and order.table_number == "7" and order.payment_method == "Par lien de paiement"


@pytest.mark.asyncio
async def test_a_closed_kitchen_takes_orders_for_later(db_session):
    tenant = await _resto(db_session, kitchen_hours=_closed_hours(), dine_in_enabled=False, delivery_enabled=False)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    closed = (await _tap(db_session, tenant, customer, conversation, "fc")).actions[0]
    assert "La cuisine est fermée" in closed["body"] and "demain à 11 h" in closed["body"]
    assert _ids(closed) == ["g:fl", "g:fb", "g:fv"]
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    assert (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]["body"] == closed["body"]
    times = (await _tap(db_session, tenant, customer, conversation, "fl")).actions[0]
    assert times["rows"][0][1] == "Demain à 11 h" and times["rows"][1][1] == "Demain à 11 h 30"
    assert times["rows"][-1][1] == "Demain à 14 h 30"  # 30 min avant la fermeture au plus tard
    refused = (await _tap(db_session, tenant, customer, conversation, "ft:203001010300")).actions[0]  # hors horaires
    assert refused["rows"] == times["rows"]
    stamp = times["rows"][1][0].split(":", 2)[2]
    recap = (await _tap(db_session, tenant, customer, conversation, f"ft:{stamp}")).actions[0]
    assert "Pour : demain à 11 h 30" in recap["body"] and "À emporter" in recap["body"]
    done = (await _tap(db_session, tenant, customer, conversation, "fw")).actions[0]
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    scheduled = order.scheduled_for.replace(tzinfo=order.scheduled_for.tzinfo or timezone.utc)
    assert (scheduled.hour, scheduled.minute) == (11, 30)  # Abidjan : UTC
    assert "Pour le " in done["body"] and " à 11 h 30" in done["body"]
    assert receipt_service.scheduled_line(datetime(2026, 11, 6, 19, 30, tzinfo=timezone.utc), timezone.utc) == \
        "Pour le vendredi 6 novembre à 19 h 30"


@pytest.mark.asyncio
async def test_a_dish_that_runs_out_leaves_the_cart(db_session):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    dish.stock_quantity = 0  # « Indisponible aujourd'hui »
    await db_session.commit()
    cart = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert "n'est plus disponible aujourd'hui" in cart["body"] and "panier est vide" in cart["body"]
    assert await db_session.scalar(select(Order.id).where(Order.tenant_id == tenant.id)) is None


@pytest.mark.asyncio
async def test_a_table_is_booked_with_buttons(db_session):
    tenant = await _resto(db_session)
    db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=SERVICES,
                                             slot_minutes=30, capacity=10))
    await db_session.commit()
    customer, conversation = await _conversation(db_session, tenant)
    party = (await _tap(db_session, tenant, customer, conversation, "fb")).actions[0]
    assert len(party["rows"]) == 10 and party["rows"][-1][0] == "g:fb:big"
    big = await _tap(db_session, tenant, customer, conversation, "fb:big")
    assert big.handoff and "groupe" in big.handoff_reason
    days = (await _tap(db_session, tenant, customer, conversation, "fb:4")).actions[0]
    assert days["rows"] and all(r[0].startswith("g:fj:4:") for r in days["rows"])
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    services = (await _tap(db_session, tenant, customer, conversation, f"fj:4:{tomorrow}")).actions[0]
    assert _titles(services) == ["Midi", "Soir"] and services["rows"][1][2] == "De 19 h à 23 h"
    times = (await _tap(db_session, tenant, customer, conversation, f"fe:4:{tomorrow}:1")).actions[0]
    assert times["rows"][0][1] == "19 h" and times["rows"][1][1] == "19 h 30"
    booked = await _tap(db_session, tenant, customer, conversation, times["rows"][0][0][2:])
    table = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert table.kind == "TABLE" and table.party_size == 4 and table.status == "CONFIRMED" and booked.booked == [table]
    confirmation = booked.actions[0]
    assert "Votre table pour 4 personnes est réservée" in confirmation["body"]
    assert _titles(confirmation) == ["Déplacer", "Annuler", "Voir la carte"]
    # 4 + 7 > 10 couverts au même service : le soir de demain n'est plus proposé à 7 personnes.
    full = await resto_flow.services(db_session, tenant, False, customer, 7, tomorrow)
    assert _titles(full.actions[0])[:2] == ["12 h", "12 h 30"]  # midi seulement : la liste des heures, directement
    assert all(not t.startswith(("19", "20", "21", "22")) for t in _titles(full.actions[0]))


@pytest.mark.asyncio
async def test_without_online_booking_the_request_goes_to_the_restaurant(db_session):
    tenant = await _resto(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    ask = (await _tap(db_session, tenant, customer, conversation, "fb:2")).actions[0]
    assert ask["type"] == "text" and "Pour 2 personnes" in ask["body"]
    assert await guided_flow.expects_text(db_session, tenant, customer, "samedi à 20 h")
    sent = await _say(db_session, tenant, customer, conversation, "samedi à 20 h")
    assert sent.handoff and sent.handoff_done and "samedi à 20 h" in sent.handoff_text
    request = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert request.kind == "TABLE" and request.party_size == 2 and request.status != "CONFIRMED"


@pytest.mark.asyncio
async def test_a_dish_qr_code_opens_its_sheet(db_session):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    turn = await guided_flow.handle(db_session, tenant, "Bonjour", None, None, False, customer, conversation,
                                    entry={"product": dish})
    assert f"g:fa:{dish.id}" in _ids(turn.actions[0])
    shop_buttons = await _tap(db_session, tenant, customer, conversation, "cat")  # bouton commun « Voir la carte »
    assert shop_buttons.actions[0]["type"] in ("list", "buttons")


def test_dashboard_shows_the_scheduled_time():
    assert "function scheduledNotice(order)" in HTML and "${scheduledNotice(o)}" in HTML
    assert '"Accompagnements" : "Tailles"' in HTML


@pytest.mark.asyncio
async def test_written_quantities_and_party_sizes_stay_in_bounds(db_session):
    tenant = await _resto(db_session)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    ask = (await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:x")).actions[0]
    assert ask["type"] == "text" and "1 à 20" in ask["body"]
    again = (await _say(db_session, tenant, customer, conversation, "50")).actions[0]
    assert again["type"] == "text" and "1 à 20" in again["body"]
    cart = (await _say(db_session, tenant, customer, conversation, "12")).actions[0]
    assert "Poulet braisé × 12" in cart["body"]
    party = (await _tap(db_session, tenant, customer, conversation, "fb:12")).actions[0]  # au-delà : la liste revient
    assert party["rows"][-1][0] == "g:fb:big"


SUPERADMIN = (Path(__file__).resolve().parents[1] / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restaurant_in_superadmin_and_buttons_switch(client, db_session):
    from app.tests.test_lot53_courtier import _headers

    assert 'RESTAURANT: "Restaurant"' in SUPERADMIN  # liste des secteurs, filtre et changement de secteur
    tenant = await _resto(db_session)
    headers = await _headers(client, tenant)
    assert (await client.get("/api/v1/tenants/me/guided-flow", headers=headers)).status_code == 200
    r = await client.put("/api/v1/tenants/me/guided-flow", headers=headers, json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"enabled": False}
    await db_session.refresh(tenant)
    assert not guided_flow.applies(tenant)
    assert 'class="card guided-card" style' in HTML and "loadFulfillment(); loadGuidedFlow(); }" in HTML
