"""
Lot 95 — panier sur WhatsApp : « Ajouter au panier », quantités dans la limite du stock, panier 24 h, livraison ou
retrait selon la boutique (frais, minimum, livraison offerte), récapitulatif, commande créée au prix de la taille
choisie, confirmation avec « Suivre ma commande », suivi réel. Tout par le code, sans IA.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.cart import Cart
from app.models.conversation import Conversation, Message
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryStatus
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.tenant import Tenant
from app.services import guided_flow as gf
from app.services import shop_cart as sc
from app.services.order_service import OrderCreationError, cancel_order, create_order
from app.services.receipt_service import get_order_item_lines
from app.tests.test_handoff_rules import _setup, wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _WA, _shop, _tap, _text, guided_on  # noqa: F401 — fixture

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)  # coupé par défaut dans les autres tests


async def _client_of(db, tenant, number="2250700009501", address=None):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number, address=address)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db.add(conversation)
    await db.flush()
    return customer, conversation


async def _setting(db, tenant, **fields):
    for key, value in fields.items():
        setattr(tenant, key, value)
    await db.flush()


def _ids(action):
    return [b[0] for b in action["buttons"]]


def _titles(action):
    return [b[1] for b in action["buttons"]]


# --- Ajouter au panier et quantités ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_to_cart_offers_quantities_within_the_stock(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-1")
    customer, conv = await _client_of(db_session, tenant)
    kente = items[1]  # 18 000 XOF, 2 en stock, sans taille
    [qty] = (await gf.handle(db_session, tenant, None, f"g:a:{kente.id}", customer=customer, conversation=conv)).actions
    assert qty["type"] == "list" and qty["button"] == "Choisir la quantité"
    assert [r[1:] for r in qty["rows"]] == [("1", "18 000 XOF"), ("2", "36 000 XOF")]  # jamais plus que le stock
    assert qty["body"].startswith("*Robe Kente* — 18 000 XOF l'unité.")


@pytest.mark.asyncio
async def test_a_product_with_sizes_asks_the_size_then_uses_its_price(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-2")
    customer, conv = await _client_of(db_session, tenant)
    [sizes] = (await gf.handle(db_session, tenant, None, f"g:a:{items[0].id}", customer=customer, conversation=conv)).actions
    assert [r[1] for r in sizes["rows"]] == ["Taille 38", "Taille 42"]
    [qty] = (await gf.handle(db_session, tenant, None, f"g:v:{items[0].id}:2", customer=customer, conversation=conv)).actions
    assert [r[1:] for r in qty["rows"]] == [("1", "16 500 XOF"), ("2", "33 000 XOF")]
    assert qty["rows"][1][0] == f"g:q:{items[0].id}:2:2"


@pytest.mark.asyncio
async def test_the_cart_shows_lines_total_and_three_buttons(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-3")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:1", customer=customer, conversation=conv)
    [cart] = (await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:2", customer=customer, conversation=conv)).actions
    assert cart["body"].splitlines() == ["Ajouté au panier ✅", "", "• Robe wax Awa (Taille 42) × 1 — 16 500 XOF",
                                         "• Robe Kente × 2 — 36 000 XOF", "", "*Total : 52 500 XOF*"]
    assert _titles(cart) == ["Confirmer", "Continuer mes achats", "Vider le panier"]
    # Le même article ajouté deux fois : une seule ligne, quantités additionnées, plafonnées au stock.
    [again] = (await gf.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:5", customer=customer, conversation=conv)).actions
    assert "Il n'en reste que 2 : j'en ai mis 2 au panier." in again["body"]
    assert "• Robe wax Awa (Taille 42) × 2 — 33 000 XOF" in again["body"]
    [full] = (await gf.handle(db_session, tenant, None, f"g:a:{items[1].id}", customer=customer, conversation=conv)).actions
    assert "tout le stock disponible est déjà dans votre panier" in full["body"]


@pytest.mark.asyncio
async def test_other_quantity_is_written_by_the_customer(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-4")
    items[1].stock_quantity = 20
    customer, conv = await _client_of(db_session, tenant)
    [qty] = (await gf.handle(db_session, tenant, None, f"g:a:{items[1].id}", customer=customer, conversation=conv)).actions
    assert qty["rows"][-1] == (f"g:q:{items[1].id}:-:x", "Autre quantité", "jusqu'à 20")
    [ask] = (await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:x", customer=customer, conversation=conv)).actions
    assert ask["type"] == "text" and "nombre" in ask["body"]
    assert await sc.expects_text(db_session, tenant, customer, "8") is True
    [cart] = (await gf.handle(db_session, tenant, "8", None, customer=customer, conversation=conv)).actions
    assert "• Robe Kente × 8 — 144 000 XOF" in cart["body"]
    # Une vraie question pendant l'attente va à l'IA, et Bob n'attend plus de nombre.
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:x", customer=customer, conversation=conv)
    assert await gf.handle(db_session, tenant, "Vous livrez à Yamoussoukro ?", None, customer=customer) is None
    assert await sc.expects_text(db_session, tenant, customer, "3") is False


@pytest.mark.asyncio
async def test_a_written_quantity_is_awaited_two_hours_only(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-22")
    items[1].stock_quantity = 20
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:x", customer=customer, conversation=conv)
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    cart.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    assert await sc.expects_text(db_session, tenant, customer, "8") is False
    assert await gf.handle(db_session, tenant, "8", None, customer=customer, conversation=conv) is None


@pytest.mark.asyncio
async def test_a_removed_or_foreign_product_is_never_added(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-23")
    other, _, foreign = await _shop(db_session, "x" + unique_email, "pn-l95-24")
    customer, conv = await _client_of(db_session, tenant)
    items[1].active = False
    await db_session.flush()
    for reply_id in (f"g:a:{items[1].id}", f"g:q:{items[1].id}:-:1", f"g:a:{foreign[1].id}", f"g:q:{foreign[1].id}:-:1"):
        turn = await gf.handle(db_session, tenant, None, reply_id, customer=customer, conversation=conv)
        assert turn.actions[0]["type"] == "buttons" and "Choisir la quantité" not in str(turn.actions)
    assert not await sc.has_items(db_session, tenant, customer)


@pytest.mark.asyncio
async def test_panier_typed_shows_the_cart_and_clear_empties_it(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-5")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    [cart] = (await gf.handle(db_session, tenant, "Mon panier", None, customer=customer, conversation=conv)).actions
    assert cart["body"].startswith("Votre panier :")
    [empty] = (await gf.handle(db_session, tenant, None, "g:clr", customer=customer, conversation=conv)).actions
    assert empty["body"] == "Votre panier est vidé."
    [again] = (await gf.handle(db_session, tenant, "panier", None, customer=customer, conversation=conv)).actions
    assert again["body"] == "Votre panier est vide." and _titles(again) == ["Voir le catalogue", "Parler à la boutique"]


@pytest.mark.asyncio
async def test_cart_button_appears_on_menus_once_something_is_in_it(db_session, unique_email):
    tenant, robes, items = await _shop(db_session, unique_email, "pn-l95-6")
    customer, conv = await _client_of(db_session, tenant)
    [menu] = (await gf.handle(db_session, tenant, "Bonjour", None, customer=customer)).actions
    assert "Voir mon panier" not in _titles(menu)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    [menu] = (await gf.handle(db_session, tenant, "Bonjour", None, customer=customer)).actions
    assert _titles(menu) == ["Voir le catalogue", "Voir mon panier", "Parler à la boutique"]
    nav = (await gf.handle(db_session, tenant, None, f"g:r:{robes.id}:0", customer=customer)).actions[-1]
    assert "g:k" in _ids(nav)
    [detail] = (await gf.handle(db_session, tenant, None, f"g:d:{items[1].id}", customer=customer)).actions
    assert _titles(detail) == ["Ajouter au panier", "Retour au catalogue", "Voir mon panier"]


@pytest.mark.asyncio
async def test_the_cart_is_kept_24_hours(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-7")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    cart.updated_at = datetime.now(timezone.utc) - timedelta(hours=23)
    assert await sc.has_items(db_session, tenant, customer)
    cart.updated_at = datetime.now(timezone.utc) - timedelta(hours=25)
    assert not await sc.has_items(db_session, tenant, customer)


@pytest.mark.asyncio
async def test_catalogue_changes_are_applied_to_the_cart(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-8")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:2", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:2", customer=customer, conversation=conv)
    # Les tailles ont été réorganisées et la Kente n'a plus qu'un exemplaire : rien n'est vendu à tort.
    items[0].variants = [{"options": {"Taille": "44"}, "stock": 1}, {"options": {"Taille": "38"}, "stock": 3},
                         {"options": {"Taille": "40"}, "stock": 5}]
    items[1].stock_quantity = 1
    await db_session.flush()
    [cart] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert "Robe wax Awa n'est plus disponible : retiré du panier." in cart["body"]
    assert "Robe Kente : il n'en reste que 1." in cart["body"]
    assert "• Robe Kente × 1 — 18 000 XOF" in cart["body"] and _titles(cart)[0] == "Confirmer"


# --- Livraison ou retrait -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_only_asks_the_address_then_shows_the_recap(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-9")
    await _setting(db_session, tenant, delivery_fee=1500, delivery_zone="Abidjan")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:1", customer=customer, conversation=conv)
    [ask] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert ask["type"] == "text" and ask["body"].startswith("Écrivez l'adresse de livraison (livraison : Abidjan)")
    assert await sc.expects_text(db_session, tenant, customer, "Cocody Angré, 8e tranche, près de la pharmacie")
    [recap] = (await gf.handle(db_session, tenant, "Cocody Angré, 8e tranche, près de la pharmacie", None,
                               customer=customer, conversation=conv)).actions
    assert recap["body"].splitlines() == [
        "*Récapitulatif*", "", "• Robe wax Awa (Taille 42) × 1 — 16 500 XOF", "",
        "Livraison (Abidjan) : 1 500 XOF", "Adresse : Cocody Angré, 8e tranche, près de la pharmacie", "",
        "*Total : 18 000 XOF*"]
    assert _titles(recap) == ["Valider ma commande", "Modifier"]


@pytest.mark.asyncio
async def test_both_modes_offer_the_choice_and_pickup_has_no_fee(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-10")
    await _setting(db_session, tenant, delivery_fee=2000, pickup_enabled=True, pickup_address="Cocody, rue des Jardins")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    [choice] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert _titles(choice) == ["Livraison", "Retrait en boutique", "Modifier le panier"]
    assert "Livraison : 2 000 XOF" in choice["body"] and "Retrait en boutique : Cocody, rue des Jardins" in choice["body"]
    [recap] = (await gf.handle(db_session, tenant, None, "g:m:r", customer=customer, conversation=conv)).actions
    assert "Retrait en boutique : Cocody, rue des Jardins" in recap["body"] and "*Total : 18 000 XOF*" in recap["body"]
    [done] = (await gf.handle(db_session, tenant, None, "g:val", customer=customer, conversation=conv)).actions
    order = (await db_session.execute(select(Order).where(Order.customer_id == customer.id))).scalar_one()
    assert order.fulfillment == "PICKUP" and order.delivery_fee is None and float(order.total_amount) == 18000
    assert done["body"].endswith("Retrait : Cocody, rue des Jardins") and "Retrait en boutique" in done["body"]


@pytest.mark.asyncio
async def test_pickup_only_skips_the_question(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-11")
    await _setting(db_session, tenant, delivery_enabled=False, pickup_enabled=True, pickup_address="Plateau")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    [recap] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert recap["body"].startswith("*Récapitulatif*") and "Retrait en boutique : Plateau" in recap["body"]
    # Un ancien bouton « Livraison » ne contourne pas le réglage.
    [again] = (await gf.handle(db_session, tenant, None, "g:m:l", customer=customer, conversation=conv)).actions
    assert "Retrait en boutique : Plateau" in again["body"]


@pytest.mark.asyncio
async def test_minimum_order_and_free_delivery(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-12")
    await _setting(db_session, tenant, delivery_fee=1500, delivery_min_order=20000, delivery_free_from=30000)
    customer, conv = await _client_of(db_session, tenant, address="Marcory, zone 4")
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    [block] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert block["body"] == "La livraison est possible à partir de 20 000 XOF d'achats. Il vous manque 2 000 XOF."
    assert _titles(block) == ["Continuer mes achats", "Parler à la boutique"]
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)  # 36 000
    [same] = (await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)).actions
    assert same["body"].endswith("Marcory, zone 4") and _titles(same) == ["Oui, cette adresse", "Autre adresse"]
    [recap] = (await gf.handle(db_session, tenant, None, "g:ad:same", customer=customer, conversation=conv)).actions
    assert "Livraison : offerte" in recap["body"] and "*Total : 36 000 XOF*" in recap["body"]


@pytest.mark.asyncio
async def test_unstated_fee_is_never_invented(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-13")
    customer, conv = await _client_of(db_session, tenant, address="Yopougon")
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)
    [recap] = (await gf.handle(db_session, tenant, None, "g:ad:same", customer=customer, conversation=conv)).actions
    assert "Livraison : frais précisés par la boutique" in recap["body"]
    assert recap["body"].endswith("*Total : 18 000 XOF*\n(hors frais de livraison)")


# --- Validation, confirmation, suivi --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validation_creates_the_order_at_the_size_price(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-14")
    await _setting(db_session, tenant, delivery_fee=1000, payment_link="https://pay.example.com/boutique")
    customer, conv = await _client_of(db_session, tenant, address="Cocody")
    await gf.handle(db_session, tenant, None, f"g:q:{items[0].id}:2:2", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, "g:ad:same", customer=customer, conversation=conv)
    [done] = (await gf.handle(db_session, tenant, None, "g:val", customer=customer, conversation=conv)).actions
    order = (await db_session.execute(select(Order).where(Order.customer_id == customer.id))).scalar_one()
    [line] = (await db_session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    assert (float(line.unit_price), line.variant_index, line.variant_label) == (16500, 2, "Taille 42")
    assert float(order.total_amount) == 34000 and float(order.delivery_fee) == 1000 and order.fulfillment == "DELIVERY"
    assert order.delivery_address == "Cocody" and order.created_by == "IA" and order.conversation_id == conv.id
    product = await db_session.get(Product, items[0].id)
    assert product.variants[2]["stock"] == 0 and product.variants[0]["stock"] == 3  # la taille vendue, et elle seule
    assert done["message_type"] == "order_confirmation" and _titles(done) == ["Suivre ma commande", "Parler à la boutique"]
    assert "Robe wax Awa (Taille 42) x2 — 33 000 XOF" in done["body"] and "Livraison — 1 000 XOF" in done["body"]
    assert "Total : 34 000 XOF" in done["body"] and "https://pay.example.com/boutique" in done["body"]
    assert (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one().items == []
    # Double appui sur « Valider » : jamais une deuxième commande.
    [twice] = (await gf.handle(db_session, tenant, None, "g:val", customer=customer, conversation=conv)).actions
    assert twice["body"] == f"Votre commande n° {str(order.id)[:8].upper()} est bien enregistrée."
    assert len((await db_session.execute(select(Order).where(Order.customer_id == customer.id))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_stock_sold_meanwhile_refuses_cleanly(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-15")
    customer, conv = await _client_of(db_session, tenant, address="Cocody")
    await gf.handle(db_session, tenant, None, f"g:q:{items[2].id}:-:1", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, "g:ok", customer=customer, conversation=conv)
    await gf.handle(db_session, tenant, None, "g:ad:same", customer=customer, conversation=conv)
    items[2].stock_quantity = 0  # vendu en boutique entre-temps
    await db_session.flush()
    [msg] = (await gf.handle(db_session, tenant, None, "g:val", customer=customer, conversation=conv)).actions
    assert "Sac cabas est épuisé : retiré du panier." in msg["body"]
    assert (await db_session.execute(select(Order).where(Order.customer_id == customer.id))).first() is None


@pytest.mark.asyncio
async def test_tracking_gives_the_real_state(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-16")
    customer, conv = await _client_of(db_session, tenant)
    [none] = (await gf.handle(db_session, tenant, "ma commande", None, customer=customer, conversation=conv)).actions
    assert none["body"] == "Vous n'avez pas encore de commande chez nous."
    order = await create_order(db_session, tenant.id, customer.id, [{"product_id": str(items[1].id), "quantity": 1}],
                               "Cocody", None, "IA", fulfillment="DELIVERY", delivery_fee=0)
    ref = str(order.id)[:8].upper()
    [state] = (await gf.handle(db_session, tenant, None, "g:s", customer=customer, conversation=conv)).actions
    assert state["body"] == (f"Commande n° {ref} (18 000 XOF) : enregistrée : la boutique attend la preuve de paiement "
                             "pour confirmer la vente.")
    delivery = (await db_session.execute(select(Delivery).where(Delivery.order_id == order.id))).scalar_one()
    delivery.status, delivery.tracking_number = DeliveryStatus.IN_TRANSIT, "ABJ-77"
    await db_session.flush()
    [state] = (await gf.handle(db_session, tenant, "Suivre ma commande", None, customer=customer, conversation=conv)).actions
    assert state["body"] == f"Commande n° {ref} (18 000 XOF) : en livraison.\nN° de suivi : ABJ-77"
    order.fulfillment, delivery.status = "PICKUP", DeliveryStatus.DELIVERED
    await db_session.flush()
    [state] = (await gf.handle(db_session, tenant, None, "g:s", customer=customer, conversation=conv)).actions
    assert state["body"].endswith(": retirée en boutique.")


# --- Commande : tailles, frais, annulation, lignes partagées --------------------------------------------------------

@pytest.mark.asyncio
async def test_create_order_checks_the_size_label_and_cancel_gives_the_stock_back(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-17")
    customer, _ = await _client_of(db_session, tenant)
    with pytest.raises(OrderCreationError, match="n'est plus disponible"):
        await create_order(db_session, tenant.id, customer.id,
                           [{"product_id": str(items[0].id), "quantity": 1, "variant": 2, "label": "Taille 40"}], None, None, "IA")
    with pytest.raises(OrderCreationError, match=r"Stock insuffisant pour Robe wax Awa \(Taille 42\)"):
        await create_order(db_session, tenant.id, customer.id,
                           [{"product_id": str(items[0].id), "quantity": 3, "variant": 2, "label": "Taille 42"}], None, None, "IA")
    with pytest.raises(OrderCreationError, match="Frais de livraison invalides"):
        await create_order(db_session, tenant.id, customer.id, [{"product_id": str(items[1].id), "quantity": 1}],
                           None, None, "IA", delivery_fee=-5)
    stock_before = items[0].stock_quantity
    order = await create_order(db_session, tenant.id, customer.id,
                               [{"product_id": str(items[0].id), "quantity": 2, "variant": 0, "label": "Taille 38"}],
                               "Cocody", None, "IA", delivery_fee=500, fulfillment="DELIVERY")
    assert float(order.total_amount) == 30500
    assert await get_order_item_lines(db_session, order.id, "XOF") == ["Robe wax Awa (Taille 38) x2 — 30 000 XOF",
                                                                       "Livraison — 500 XOF"]
    product = await db_session.get(Product, items[0].id)
    assert product.variants[0]["stock"] == 1 and product.stock_quantity == max(0, stock_before - 2)
    await cancel_order(db_session, tenant.id, order.id)
    product = await db_session.get(Product, items[0].id)
    assert product.variants[0]["stock"] == 3


@pytest.mark.asyncio
async def test_orders_taken_by_the_ai_are_unchanged(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-18")
    customer, _ = await _client_of(db_session, tenant)
    order = await create_order(db_session, tenant.id, customer.id, [{"product_id": str(items[1].id), "quantity": 1}],
                               None, None, "IA")
    assert (order.delivery_fee, order.fulfillment, float(order.total_amount)) == (None, None, 18000)
    assert await get_order_item_lines(db_session, order.id, "XOF") == ["Robe Kente x1 — 18 000 XOF"]


@pytest.mark.asyncio
async def test_forget_me_deletes_the_cart(db_session, unique_email):
    from app.services import customer_profile

    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-19")
    customer, conv = await _client_of(db_session, tenant)
    await gf.handle(db_session, tenant, None, f"g:q:{items[1].id}:-:1", customer=customer, conversation=conv)
    await customer_profile.forget(db_session, customer)
    assert (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).first() is None


# --- De bout en bout, par le webhook ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whole_purchase_through_whatsapp_without_ai(client, db_session, unique_email, guided_on):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l95-20")
    tenant = await db_session.get(Tenant, tenant.id)
    tenant.delivery_fee = 1000
    await db_session.commit()
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2250700009520"
    steps = [_tap("pn-l95-20", number, f"g:a:{items[1].id}", "Ajouter au panier"),
             _tap("pn-l95-20", number, f"g:q:{items[1].id}:-:2", "2", "list_reply"),
             _text("pn-l95-20", number, "panier"),
             _tap("pn-l95-20", number, "g:ok", "Confirmer"),
             _text("pn-l95-20", number, "Riviera 3, villa 12"),
             _tap("pn-l95-20", number, "g:val", "Valider ma commande"),
             _text("pn-l95-20", number, "ma commande")]
    for payload in steps:
        r = await client.post("/webhooks/whatsapp", json=payload)
        assert r.json().get("guided") is True, r.json()
    assert [s["kind"] for s in _WA.sent] == ["list", "buttons", "buttons", "text", "buttons", "buttons", "buttons"]
    assert state["llm"].calls == [] if hasattr(state["llm"], "calls") else True
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None  # aucune analyse IA
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert float(order.total_amount) == 37000 and order.delivery_address == "Riviera 3, villa 12"
    confirmation = (await db_session.execute(select(Message).where(Message.message_type == "order_confirmation"))).scalar_one()
    assert "Livraison — 1 000 XOF" in confirmation.content
    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == number))).scalar_one()
    assert customer.address == "Riviera 3, villa 12"  # adresse habituelle pour la prochaine fois (lot 86)


# --- Réglages du commerçant -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merchant_fulfillment_settings(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-l95-21")
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    got = (await client.get("/api/v1/tenants/me/fulfillment", headers=headers)).json()
    assert got == {"delivery_enabled": True, "delivery_fee": None, "delivery_zone": None, "delivery_min_order": None,
                   "delivery_free_from": None, "pickup_enabled": False, "pickup_address": None, "currency": "XOF"}
    body = {"delivery_enabled": True, "delivery_fee": 1500, "delivery_zone": "  Abidjan ", "delivery_min_order": 10000,
            "delivery_free_from": 50000, "pickup_enabled": True, "pickup_address": "Cocody"}
    saved = (await client.put("/api/v1/tenants/me/fulfillment", json=body, headers=headers)).json()
    assert saved["delivery_fee"] == 1500 and saved["delivery_zone"] == "Abidjan" and saved["pickup_address"] == "Cocody"
    off = {**body, "delivery_enabled": False, "pickup_enabled": False}
    assert (await client.put("/api/v1/tenants/me/fulfillment", json=off, headers=headers)).status_code == 422
    no_address = {**body, "pickup_address": " "}
    assert (await client.put("/api/v1/tenants/me/fulfillment", json=no_address, headers=headers)).status_code == 422
    assert (await client.put("/api/v1/tenants/me/fulfillment", json={**body, "delivery_fee": -1},
                             headers=headers)).status_code == 422


def test_dashboard_card_and_order_summary():
    assert 'id="fulfillment-card"' in DASH and "function saveFulfillment(" in DASH and "loadFulfillment();" in DASH
    for field in ("ff-delivery", "ff-fee", "ff-zone", "ff-min", "ff-free", "ff-pickup", "ff-address"):
        assert f'id="{field}"' in DASH
    assert "Retrait en boutique" in DASH and "variant_label" in DASH


def test_button_ids_and_titles_fit_whatsapp():
    pid = str(uuid.uuid4())
    for reply_id in (f"g:q:{pid}:12:x", f"g:v:{pid}:12", f"g:a:{pid}", "g:ad:same", "g:val", "g:clr"):
        assert len(reply_id) <= 200
    for title in ("Ajouter au panier", "Continuer mes achats", "Vider le panier", "Valider ma commande",
                  "Suivre ma commande", "Retrait en boutique", "Modifier le panier", "Oui, cette adresse",
                  "Voir mon panier", "Choisir la quantité"):
        assert len(title) <= 20, title
    assert Decimal("0") == sc.delivery_fee(type("T", (), {"delivery_free_from": 100, "delivery_fee": 5})(), Decimal(100))
