"""
Lot 129 — le restaurant depuis le numéro AgenC'AI (décisions de Thérèse du 23/11) : menu de 10 lignes sans « Ventes à
confirmer » ni « Vendu » ; plat épuisé / de retour en un toucher ; fermer / rouvrir la cuisine pour la journée ;
réservations du jour ; liens vers la carte, les horaires, les affiches et le tableau de bord complet ; alertes de
commande et de table écrites pour un restaurant (mode, commune ou table, heure, nombre de personnes).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.services import merchant_assistant as ma
from app.services import restaurant, resto_flow
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import NOW, _linked, _msg, _replies, _say, assistant_env  # noqa: F401

HOURS = {str(d): [["11:00", "15:00"], ["18:00", "23:00"]] for d in range(7)}


@pytest.fixture
def cards(monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_cards", "on")


async def _resto(client, db, dishes=("Poulet braisé", "Poisson braisé", "Garba")):
    tenant, _, headers, connection = await _linked(client, db)
    tenant = await db.get(Tenant, tenant.id)
    tenant.business_type, tenant.kitchen_hours, tenant.delivery_areas = "RESTAURANT", HOURS, [{"name": "Cocody", "fee": 1000}]
    for name in dishes:
        db.add(Product(tenant_id=tenant.id, sku=f"P{uuid.uuid4().hex[:6]}", name=name, price=3500, currency="XOF",
                       stock_quantity=5, active=True))
    await db.commit()
    return tenant, headers, connection


async def _fresh(db, model, key):
    return (await db.execute(select(model).where(model.id == key).execution_options(populate_existing=True))).scalar_one()


async def _last(db, tenant):
    return (await _replies(db, tenant))[-1].payload


def test_the_restaurant_menu_respects_the_contract_and_drops_sale_confirmation():
    lines = ma.RESTO_MENU_LIST["lignes"]
    ids = [line["id"] for line in lines]
    assert len(lines) == 10 and len(set(ids)) == 10
    assert all(len(line["titre"]) <= 24 and len(line.get("description") or "") <= 72 for line in lines)
    assert all(i.split(":")[2] in ma.MENU_COMMANDS for i in ids)
    assert "bob:menu:a-confirmer" not in ids and "bob:menu:vendu" not in ids
    for button_id, title in ma.RESTO_MENU_1[1] + ma.RESTO_MENU_2[1]:
        assert button_id.split(":")[2] in ma.MENU_COMMANDS and len(title) <= 20


def test_restaurant_words_never_hijack_a_shop():
    assert ma.match_command("carte cadeau 5000") == (None, "carte cadeau 5000")  # une boutique ajoute son produit
    assert ma.match_command("carte cadeau 5000", restaurant=True)[0] == "page_menu"
    assert ma.match_command("Épuisé poulet", restaurant=True) == ("sold_out", "poulet")
    assert ma.match_command("bob fermer la cuisine", restaurant=True) == ("close", "")
    assert ma.match_command("réservations du jour", restaurant=True) == ("bookings", "")


@pytest.mark.asyncio
async def test_menu_is_the_restaurant_list(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg("menu"))
    payload = await _last(db_session, tenant)
    assert payload["texte"] == ma.RESTO_MENU_INTRO and len(payload["liste"]["lignes"]) == 10
    assert "Ventes à confirmer" not in str(payload) and "Vendu" not in str(payload)


@pytest.mark.asyncio
async def test_a_shop_keeps_its_menu(client, db_session, assistant_env, cards):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _msg("carte"))  # pas une commande pour une boutique : le menu habituel
    payload = await _last(db_session, tenant)
    assert payload["texte"] == ma.MENU_INTRO and payload["liste"] == {**payload["liste"], "bouton": "Menu de Bob"}
    await _say(client, db_session, headers, _msg(button="bob:menu:cuisine", kind="bouton"))
    assert (await _last(db_session, tenant))["texte"] == ma.MENU_INTRO


@pytest.mark.asyncio
async def test_a_dish_sold_out_in_one_touch_and_back(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg(button="bob:menu:plats", kind="bouton"))
    rows = (await _last(db_session, tenant))["liste"]["lignes"]
    poulet = next(r for r in rows if r["titre"] == "Poulet braisé")
    assert poulet["id"].startswith("bob:plat:") and poulet["description"].startswith("Disponible")
    await _say(client, db_session, headers, _msg(button=poulet["id"], kind="bouton"))
    product = await _fresh(db_session, Product, uuid.UUID(poulet["id"].split(":")[2]))
    assert product.sheet["unavailable_on"] == "2026-10-27" and product.stock_quantity == 0
    reply = await _last(db_session, tenant)
    assert "épuisé pour aujourd'hui" in reply["texte"] and reply["boutons"][0]["id"] == f"bob:plat-on:{product.id}"
    await _say(client, db_session, headers, _msg(button="bob:menu:plats", kind="bouton"))
    first = (await _last(db_session, tenant))["liste"]["lignes"][0]
    assert first["titre"] == "Poulet braisé" and first["description"].startswith("Épuisé")  # les épuisés en tête
    await _say(client, db_session, headers, _msg(button=f"bob:plat-on:{product.id}", kind="bouton"))
    product = await _fresh(db_session, Product, product.id)
    assert not (product.sheet or {}).get("unavailable_on") and product.stock_quantity >= 1


@pytest.mark.asyncio
async def test_a_dish_by_writing(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg("épuisé poisson"))
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id, Product.name == "Poisson braisé")
                                        .execution_options(populate_existing=True))).scalar_one()
    assert product.sheet["unavailable_on"] == "2026-10-27"
    await _say(client, db_session, headers, _msg("épuisé braisé"))  # deux plats : Bob demande lequel
    reply = await _last(db_session, tenant)
    assert reply["texte"] == "Lequel ?" and len(reply["boutons"]) == 2
    await _say(client, db_session, headers, _msg("de retour poisson"))
    product = await _fresh(db_session, Product, product.id)
    assert not (product.sheet or {}).get("unavailable_on")
    await _say(client, db_session, headers, _msg("épuisé pizza"))
    assert "Aucun plat ne correspond" in (await _last(db_session, tenant))["texte"]


@pytest.mark.asyncio
async def test_close_the_kitchen_for_tonight_then_reopen(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg(button="bob:menu:cuisine", kind="bouton"))
    ask = await _last(db_session, tenant)
    assert "La fermer pour le reste de la journée" in ask["texte"]  # jamais sans confirmation
    assert [b["id"] for b in ask["boutons"]] == ["bob:cuisine-fermer", "bob:retour"]
    tenant = await _fresh(db_session, Tenant, tenant.id)
    assert tenant.kitchen_closed_on is None
    await _say(client, db_session, headers, _msg(button="bob:cuisine-fermer", kind="bouton"))
    tenant = await _fresh(db_session, Tenant, tenant.id)
    assert str(tenant.kitchen_closed_on) == "2026-10-27"
    noon = NOW + timedelta(hours=2)  # 12 h à Abidjan, dans les horaires
    assert not restaurant.kitchen_open(tenant, noon)
    assert restaurant.next_opening(tenant, noon).date().isoformat() == "2026-10-28"
    assert all(t.date().isoformat() != "2026-10-27" for t in resto_flow.later_times(tenant, noon))
    assert "fermée pour aujourd'hui" in (await _last(db_session, tenant))["texte"]
    assert restaurant.kitchen_open(tenant, noon + timedelta(days=1))  # rouverte d'elle-même le lendemain
    await _say(client, db_session, headers, _msg("rouvrir"))
    tenant = await _fresh(db_session, Tenant, tenant.id)
    assert tenant.kitchen_closed_on is None and restaurant.kitchen_open(tenant, noon)


@pytest.mark.asyncio
async def test_bookings_of_the_day(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg("réservations"))
    assert (await _last(db_session, tenant))["texte"] == "Aucune table réservée aujourd'hui pour l'instant."
    customer = Customer(tenant_id=tenant.id, whatsapp_number="2250700002222", first_name="Awa")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.flush()
    for hour, people, day in ((20, 4, 0), (12, 2, 0), (20, 6, 1)):
        db_session.add(AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                          kind="TABLE", party_size=people, availability="x", status="CONFIRMED",
                                          scheduled_at=datetime(2026, 10, 27 + day, hour, 0, tzinfo=timezone.utc)))
    await db_session.commit()
    await _say(client, db_session, headers, _msg(button="bob:menu:reservations", kind="bouton"))
    text = (await _last(db_session, tenant))["texte"]
    assert text.startswith("Réservations du jour (2 tables, 6 personnes)")
    assert text.index("12 h · 2 pers. · Awa") < text.index("20 h · 4 pers. · Awa")


@pytest.mark.asyncio
@pytest.mark.parametrize("word,expected", [("carte", "/m/?page=carte&connexion="), ("horaires", "/m/?page=horaires&connexion="),
                                           ("affiches", "/m/?page=affiches&connexion="), ("tableau de bord", "/dashboard/")])
async def test_links_to_the_right_page(client, db_session, assistant_env, cards, word, expected):
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg(word))
    assert expected in (await _last(db_session, tenant))["texte"]


@pytest.mark.asyncio
async def test_alerts_are_written_for_a_restaurant(client, db_session, assistant_env, cards):
    tenant, headers, connection = await _resto(client, db_session)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="2250700003333", first_name="Koffi")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
                                last_message_at=NOW)
    db_session.add(conversation)
    await db_session.flush()
    dish = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id, Product.name == "Garba"))).scalar_one()
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, status=OrderStatus.PENDING,
                  total_amount=8000, currency="XOF", fulfillment="DELIVERY", delivery_area="Cocody", delivery_fee=1000,
                  scheduled_for=NOW + timedelta(hours=10, minutes=30), created_at=NOW - timedelta(minutes=5))
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=dish.id, quantity=2, unit_price=3500, subtotal=7000,
                             variant_label="alloco"))
    db_session.add(AppointmentRequest(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                      kind="TABLE", party_size=4, availability="ce soir", status="CONFIRMED",
                                      confirmed_by="BOB", confirmed_at=NOW - timedelta(minutes=3),
                                      scheduled_at=datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)))
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="image", content="", created_at=NOW - timedelta(minutes=2)))
    await db_session.commit()
    await ma.scan_alerts(db_session, NOW)
    texts = [r.payload for r in await _replies(db_session, tenant)]
    order_alert = next(p for p in texts if p["texte"].startswith("🍽️ Nouvelle commande — Koffi"))
    assert "2 × Garba (alloco)" in order_alert["texte"] and "Livraison · Cocody · pour 20 h 30" in order_alert["texte"]
    assert "8 000 XOF" in order_alert["texte"] and [b["titre"] for b in order_alert["boutons"]] == ["Annuler"]
    table = next(p for p in texts if p["texte"].startswith("📅 Table réservée par Bob"))
    assert "4 personnes" in table["texte"] and "Koffi" in table["texte"]
    assert not any("preuve" in p["texte"] or "Vendu" in str(p.get("boutons")) for p in texts)  # pas de « vente à confirmer »


@pytest.mark.asyncio
async def test_orders_of_the_day_show_the_mode_not_sale_confirmation(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="2250700004444", first_name="Ama")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=3500,
                         currency="XOF", fulfillment="DINE_IN", table_number="7", created_at=NOW - timedelta(minutes=5)))
    await db_session.commit()
    await _say(client, db_session, headers, _msg("commandes"))
    reply = await _last(db_session, tenant)
    assert "Table 7" in reply["texte"] and "À confirmer" not in reply["texte"] and "boutons" not in reply
