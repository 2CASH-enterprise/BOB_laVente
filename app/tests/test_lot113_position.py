"""
Lot 113 — « Envoyer la position » pour la livraison (restaurants et boutiques). Décisions du 08/11 : aucun repère
demandé en plus ; « même endroit que la dernière fois ? » ; point exact effacé 30 jours après la livraison.
Commune déduite par le code (centres des communes d'Abidjan), toujours à confirmer ; aucun service de cartes, aucun coût.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.whatsapp.client import parse_whatsapp_message
from app.models.cart import Cart
from app.models.delivery import Delivery, DeliveryStatus
from app.models.order import Order
from app.services import geo_ci, guided_flow, receipt_image
from app.services import shop_cart as sc
from app.tests.test_lot53_courtier import _conversation, _headers
from app.tests.test_lot94_parcours_boutons import _shop, guided_on  # noqa: F401 — fixture
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot106_restaurant import _resto
from app.tests.test_lot107_parcours_restaurant import HTML, _dish, _guided, _say, _tap  # noqa: F401 (fixture)

COCODY = {"lat": 5.3561, "lng": -3.9871, "name": None, "address": None}
ABOBO = {"lat": 5.4262, "lng": -4.0195, "name": None, "address": None}
PARIS = {"lat": 48.8566, "lng": 2.3522, "name": "Bureau", "address": "10 rue de Rivoli"}


async def _located(db, tenant, customer, conversation, where):
    turn = await guided_flow.handle(db, tenant, "📍 Position partagée", None, None, False, customer, conversation,
                                    entry={"location": where})
    await db.commit()
    return turn


def _titles(action):
    return [b[1] for b in action["buttons"]] if action["type"] == "buttons" else [r[1] for r in action["rows"]]


def test_the_commune_of_a_position():
    assert geo_ci.commune(COCODY["lat"], COCODY["lng"]) == "Cocody"
    assert geo_ci.commune(5.3480, -4.0760) == "Yopougon" and geo_ci.commune(ABOBO["lat"], ABOBO["lng"]) == "Abobo"
    assert geo_ci.commune(PARIS["lat"], PARIS["lng"]) is None  # loin d'Abidjan : rien n'est deviné
    zones = [{"name": "Cocody Angré", "fee": 1000}, {"name": "Port Bouet", "fee": 1500}, {"name": "Yopougon", "fee": 2000}]
    assert geo_ci.area_index(zones, "Port-Bouët") == 1 and geo_ci.area_index(zones, "Cocody") == 0
    assert geo_ci.area_index(zones, "Abobo") is None and geo_ci.area_index(zones, None) is None
    assert geo_ci.valid("5.3", "-3.9") == (5.3, -3.9) and geo_ci.valid(0, 0) is None and geo_ci.valid(95, 1) is None
    assert geo_ci.valid("abc", 1) is None
    assert geo_ci.map_link({"lat": 5.3561, "lng": -3.9871}) == "https://www.google.com/maps?q=5.3561,-3.9871"
    assert geo_ci.label(PARIS) == "Bureau — 10 rue de Rivoli" and geo_ci.label(COCODY) == "Position partagée"


def test_a_shared_position_is_read_from_the_webhook():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn"}, "messages": [{
        "from": "2250700000001", "id": "wamid.1", "type": "location",
        "location": {"latitude": 5.3561, "longitude": -3.9871, "name": "Chez moi", "address": "Angré 8e tranche"}}]}}]}]}
    parsed = parse_whatsapp_message(payload)
    assert parsed["type"] == "location" and parsed["location"] == {"lat": 5.3561, "lng": -3.9871, "name": "Chez moi",
                                                                   "address": "Angré 8e tranche"}
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": "1", "id": "w", "type": "text", "text": {"body": "x"}}
    assert parse_whatsapp_message(payload)["location"] is None


def test_the_native_button_and_its_text_fallback():
    action = guided_flow.location_request(False)
    assert action["type"] == "location" and "Envoyer la position" in action["body"]
    simpler = guided_flow.degraded(action)
    assert simpler["type"] == "text" and "trombone" in simpler["body"] and guided_flow.degraded(simpler) is None
    assert guided_flow._trace(action).endswith("[Bouton : Envoyer la position]")
    assert "Touche « Envoyer la position »" in guided_flow.location_request(True)["body"]


@pytest.mark.asyncio
async def test_restaurant_position_commune_confirmed_and_remembered(db_session):
    tenant = await _resto(db_session, pickup_enabled=False, dine_in_enabled=False, kitchen_hours=None)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:2")
    ask = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]  # un seul mode : livraison
    assert ask["type"] == "location"
    confirm = (await _located(db_session, tenant, customer, conversation, COCODY)).actions[0]
    assert confirm["body"] == "📍 Position reçue. Vous êtes à *Cocody* ? Livraison 1 000 XOF."
    assert [b[0] for b in confirm["buttons"]] == ["g:fz:0", "g:fz:l"] and _titles(confirm) == ["Oui, c'est ça", "Autre commune"]
    other = (await _tap(db_session, tenant, customer, conversation, "fz:l")).actions[0]
    assert _titles(other) == ["Cocody", "Yopougon", "Ma commune n'y est pas"]
    recap = (await _tap(db_session, tenant, customer, conversation, "fz:0")).actions[0]  # adresse déjà là : pas de question
    assert "Livraison (Cocody) : 1 000 XOF" in recap["body"] and "Position partagée" in recap["body"]
    await _tap(db_session, tenant, customer, conversation, "fw")
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert order.delivery_location == {"lat": 5.3561, "lng": -3.9871} and order.delivery_area == "Cocody"
    await db_session.refresh(customer)
    assert customer.delivery_location["area"] == "Cocody" and customer.delivery_location["lat"] == 5.3561
    assert customer.address is None  # « Position partagée » n'est pas une adresse écrite
    data = await receipt_image.gather(db_session, order)
    assert data["place"] == ("Livraison", "Position partagée · Cocody")

    # Commande suivante : même endroit que la dernière fois, sans rien redemander.
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    again = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert again["body"].startswith("Livraison au même endroit que la dernière fois ?") and "(Cocody)" in again["body"]
    assert _titles(again) == ["Oui, même endroit", "Autre endroit"]
    recap = (await _tap(db_session, tenant, customer, conversation, "fy:s")).actions[0]
    assert "Livraison (Cocody) : 1 000 XOF" in recap["body"]
    elsewhere = (await _tap(db_session, tenant, customer, conversation, "fy:n")).actions[0]
    assert elsewhere["type"] == "location"
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert cart.address is None and "pos" not in (cart.meta or {})


@pytest.mark.asyncio
async def test_a_commune_not_served_or_far_away_shows_the_list(db_session):
    tenant = await _resto(db_session, pickup_enabled=False, dine_in_enabled=False, kitchen_hours=None)
    dish = await _dish(db_session, tenant)
    customer, conversation = await _conversation(db_session, tenant)
    await _tap(db_session, tenant, customer, conversation, f"fq:{dish.id}:-:1")
    await _tap(db_session, tenant, customer, conversation, "fo")
    listing = (await _located(db_session, tenant, customer, conversation, ABOBO)).actions[0]
    assert listing["type"] == "list" and listing["body"].startswith("📍 Position reçue. Nous ne livrons pas encore à Abobo.")
    await _tap(db_session, tenant, customer, conversation, "fy:n")
    far = (await _located(db_session, tenant, customer, conversation, PARIS)).actions[0]
    assert far["body"].startswith("📍 Position reçue.\n\nDans quelle commune") and "Abobo" not in far["body"]
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert cart.address == "Bureau — 10 rue de Rivoli" and cart.meta["pos"] == {"lat": 48.8566, "lng": 2.3522}


@pytest.mark.asyncio
async def test_a_position_nobody_asked_for_goes_to_bob(db_session):
    tenant = await _resto(db_session)
    customer, conversation = await _conversation(db_session, tenant)
    turn = await _located(db_session, tenant, customer, conversation, COCODY)
    assert turn is None or all(a["type"] != "location" for a in turn.actions)  # pas de commande en cours : rien d'imposé
    assert (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one_or_none() is None \
        or not (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one().address


@pytest.mark.asyncio
async def test_shop_position_then_recap_and_same_place_next_time(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, f"pn-l113-{uuid.uuid4().hex[:6]}")
    tenant.delivery_fee, tenant.delivery_zone = 1500, "Abidjan"
    customer, conversation = await _conversation(db_session, tenant)
    await guided_flow.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:1", customer=customer, conversation=conversation)
    [ask] = (await guided_flow.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conversation)).actions
    assert ask["type"] == "location" and ask["body"].startswith("Livraison : Abidjan.")
    [recap] = (await _located(db_session, tenant, customer, conversation, PARIS)).actions
    assert "Adresse : Bureau — 10 rue de Rivoli" in recap["body"]
    await guided_flow.handle(db_session, tenant, None, "g:val", customer=customer, conversation=conversation)
    await db_session.commit()
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert order.delivery_location == {"lat": 48.8566, "lng": 2.3522}
    await db_session.refresh(customer)
    assert customer.address == "Bureau — 10 rue de Rivoli" and customer.delivery_location["label"] == "Bureau — 10 rue de Rivoli"
    await guided_flow.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:1", customer=customer, conversation=conversation)
    [same] = (await guided_flow.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conversation)).actions
    assert same["body"].startswith("Livraison au même endroit que la dernière fois ?\n\n📍 Bureau")
    assert [b[1] for b in same["buttons"]] == ["Oui, même endroit", "Autre endroit"]
    [recap] = (await guided_flow.handle(db_session, tenant, None, "g:ad:same", customer=customer, conversation=conversation)).actions
    cart = await sc.get_cart(db_session, tenant, customer)
    assert "Adresse : Bureau" in recap["body"] and cart.meta["pos"] == {"lat": 48.8566, "lng": 2.3522}
    # Écrire l'adresse reste possible et retire le point.
    await guided_flow.handle(db_session, tenant, None, "g:ad:new", customer=customer, conversation=conversation)
    await guided_flow.handle(db_session, tenant, "Cocody Angré, 8e tranche, près de la pharmacie", None,
                             customer=customer, conversation=conversation)
    assert "pos" not in ((await sc.get_cart(db_session, tenant, customer)).meta or {})


@pytest.mark.asyncio
async def test_forget_me_and_the_30_day_purge(db_session):
    from app.services import customer_profile
    from app.services.order_service import create_order

    tenant = await _resto(db_session, kitchen_hours=None)
    dish = await _dish(db_session, tenant)
    customer, _ = await _conversation(db_session, tenant)
    now = datetime.now(timezone.utc)
    orders = []
    for _ in range(3):
        orders.append(await create_order(db_session, tenant_id=tenant.id, customer_id=customer.id,
                                         items=[{"product_id": str(dish.id), "quantity": 1}], delivery_address="Position partagée",
                                         payment_method=None, created_by="IA", fulfillment="DELIVERY",
                                         delivery_location={"lat": 5.35, "lng": -3.98}))
    await db_session.flush()
    deliveries = {d.order_id: d for d in (await db_session.execute(select(Delivery))).scalars().all()}
    old_delivered, recent_delivered, never = orders
    deliveries[old_delivered.id].status, deliveries[old_delivered.id].updated_at = DeliveryStatus.DELIVERED, now - timedelta(days=31)
    deliveries[recent_delivered.id].status, deliveries[recent_delivered.id].updated_at = DeliveryStatus.DELIVERED, now - timedelta(days=5)
    never.created_at = now - timedelta(days=61)
    await db_session.commit()
    assert await geo_ci.purge_positions(db_session, now) == 2
    assert await geo_ci.purge_positions(db_session, now) == 0  # un point effacé est un vrai NULL : jamais relu
    for order in orders:
        await db_session.refresh(order)
    assert old_delivered.delivery_location is None and never.delivery_location is None
    assert recent_delivered.delivery_location == {"lat": 5.35, "lng": -3.98} and old_delivered.delivery_address == "Position partagée"
    assert customer.delivery_location is not None
    await customer_profile.forget(db_session, customer)
    assert customer.delivery_location is None
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.positions" in TASK_MODULES
    assert celery_app.conf.beat_schedule["positions-purge"]["task"] == "app.workers.positions.purge_positions_task"


@pytest.mark.asyncio
async def test_the_order_shows_the_map_and_a_message_for_the_courier(client, db_session):
    from app.services.order_service import create_order

    tenant = await _resto(db_session, kitchen_hours=None)
    dish = await _dish(db_session, tenant)
    customer, _ = await _conversation(db_session, tenant, number="2250707070707")
    order = await create_order(db_session, tenant_id=tenant.id, customer_id=customer.id,
                               items=[{"product_id": str(dish.id), "quantity": 1}], delivery_address="Position partagée",
                               payment_method=None, created_by="IA", fulfillment="DELIVERY", delivery_area="Cocody",
                               delivery_location={"lat": 5.3561, "lng": -3.9871})
    plain = await create_order(db_session, tenant_id=tenant.id, customer_id=customer.id,
                               items=[{"product_id": str(dish.id), "quantity": 1}], delivery_address="Rue 12",
                               payment_method=None, created_by="IA", fulfillment="DELIVERY")
    await db_session.commit()
    headers = await _headers(client, tenant)
    data = (await client.get(f"/api/v1/orders/{order.id}", headers=headers)).json()
    assert data["map_link"] == "https://www.google.com/maps?q=5.3561,-3.9871" and data["customer_phone"] == "+2250707070707"
    other = (await client.get(f"/api/v1/orders/{plain.id}", headers=headers)).json()
    assert other["map_link"] is None and other["customer_phone"] is None  # pas de point : pas de numéro à transmettre
    assert "courierLinks(order)" in HTML and "📍 Voir sur la carte" in HTML and "Envoyer au livreur" in HTML
    assert "https://wa.me/?text=" in HTML and "google\\.com\\/maps" in HTML


@pytest.mark.asyncio
async def test_the_webhook_takes_a_shared_position(client, db_session, guided_on):  # noqa: F811
    from app.models.conversation import Message
    from app.tests.test_lot94_parcours_boutons import _WA
    from app.tests.test_lot94_parcours_boutons import _tap as tap_payload

    tenant = await _resto(db_session, pickup_enabled=False, dine_in_enabled=False, kitchen_hours=None)
    dish = await _dish(db_session, tenant)
    guided_on({"intents": [], "objections": []}, [])
    number = "2250102030499"
    for reply in (f"g:fq:{dish.id}:-:1", "g:fo"):
        await client.post("/webhooks/whatsapp", json=tap_payload(tenant.pnid, number, reply, "x"))
    assert "Envoyer la position" in _WA.sent[-1]["body"] or "trombone" in _WA.sent[-1]["body"]
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": tenant.pnid}, "messages": [{
        "from": number, "id": f"wamid.{uuid.uuid4().hex}", "type": "location", "timestamp": "1",
        "location": {"latitude": 5.3561, "longitude": -3.9871}}]}}]}]}
    r = await client.post("/webhooks/whatsapp", json=payload)
    assert r.json()["guided"] is True
    assert _WA.sent[-1]["body"].startswith("📍 Position reçue. Vous êtes à *Cocody* ?")
    trace = (await db_session.execute(select(Message).where(Message.tenant_id == tenant.id, Message.message_type == "location"))).scalar_one()
    assert trace.content == "📍 Position partagée" and trace.message_metadata["location"] == {"lat": 5.3561, "lng": -3.9871}


@pytest.mark.asyncio
async def test_the_location_request_payload_sent_to_meta(monkeypatch):
    from app.integrations.whatsapp.client import WhatsAppClient

    posted = []

    async def fake_post(self, path, payload):
        posted.append((path, payload))
        return {"messages": [{"id": "wamid.x"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    client = WhatsAppClient(phone_number_id="pn", system_user_token="t")
    await guided_flow._send(client, "2250700000001", guided_flow.location_request(False))
    path, payload = posted[-1]
    assert path == "/messages" and payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "location_request_message"
    assert payload["interactive"]["action"] == {"name": "send_location"} and "Envoyer la position" in payload["interactive"]["body"]["text"]
