"""
Lot 95 — panier, livraison ou retrait, confirmation et suivi, dans le parcours à boutons des boutiques (lot 94).
Validé le 31/10 (écrans 5 à 9 de la maquette « Bob — parcours d'achat WhatsApp »).

Tout est fait par le CODE, sans appel à l'IA : quantités limitées au stock réel, prix relus dans le catalogue à chaque
écran (jamais mémorisés dans le panier), commande créée par order_service.create_order (stock réservé, prix de la taille
choisie), frais de livraison et minimum de commande pris dans les réglages de la boutique.

Boutons (préfixe « g: ») :
  g:a:<produit>                 « Ajouter au panier » (produit sans taille ; avec tailles → liste des tailles)
  g:v:<produit>:<variante>      taille choisie → quantités
  g:q:<produit>:<variante|->:<n|x>  quantité choisie (x = « Autre quantité », écrite par le client)
  g:k  panier · g:clr  vider · g:ok  confirmer · g:m:l / g:m:r  livraison / retrait
  g:ad:same / g:ad:new  adresse habituelle ou nouvelle · g:val  valider la commande · g:s  suivre ma commande
"""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select

CART_HOURS = 24
AWAIT_HOURS = 2
MAX_QTY = 99
CART_WORDS = {"panier", "mon panier", "voir panier", "voir le panier", "voir mon panier"}
TRACK_WORDS = {"ma commande", "suivre ma commande", "suivi", "suivi commande", "suivi de commande", "suivi de ma commande",
               "ou est ma commande", "etat de ma commande", "statut commande", "ma livraison"}

DELIVERY, PICKUP = "DELIVERY", "PICKUP"
from app.services.guided_flow import HUMAN_BUTTON  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _you(tu: bool, vous: str, toi: str) -> str:
    return toi if tu else vous


def _money(value, currency) -> str:
    from app.services.product_cards import money_text

    return money_text(value, currency or "XOF")


def _turn(actions):
    from app.services.guided_flow import GuidedTurn

    return GuidedTurn(actions)


def _buttons(body: str, buttons: list, **extra) -> dict:
    return {"type": "buttons", "body": body, "buttons": buttons[:3], **extra}


def _ref(order) -> str:
    return str(order.id)[:8].upper()


# --- Le panier en base ----------------------------------------------------------------------------------------------

async def get_cart(db, tenant, customer, create: bool = False):
    """Le panier du client ; vidé s'il n'a pas bougé depuis 24 h."""
    from app.models.cart import Cart

    if customer is None:
        return None
    cart = (await db.execute(select(Cart).where(Cart.tenant_id == tenant.id, Cart.customer_id == customer.id))).scalar_one_or_none()
    if cart is not None and _aware(cart.updated_at) < _now() - timedelta(hours=CART_HOURS):
        cart.items, cart.fulfillment, cart.address, cart.awaiting = [], None, None, None
    if cart is None and create:
        # Paniers oubliés de la boutique : effacés au passage (rien n'est gardé plus longtemps que nécessaire).
        await db.execute(delete(Cart).where(Cart.tenant_id == tenant.id,
                                            Cart.updated_at < _now() - timedelta(hours=CART_HOURS * 7)))
        cart = Cart(tenant_id=tenant.id, customer_id=customer.id, items=[])
        db.add(cart)
    return cart


def _touch(cart, **fields) -> None:
    for key, value in fields.items():
        setattr(cart, key, value)
    cart.updated_at = _now()


def awaiting(cart) -> str | None:
    if cart is None or not cart.awaiting:
        return None
    if _aware(cart.updated_at) < _now() - timedelta(hours=AWAIT_HOURS):
        return None
    return cart.awaiting


async def has_items(db, tenant, customer) -> bool:
    cart = await get_cart(db, tenant, customer)
    return bool(cart is not None and cart.items)


def _variant_of(product, index):
    """(libellé, prix, stock) de la variante, ou du produit si index est None ; None si la variante n'existe plus."""
    from app.services.order_service import variant_label

    if index is None:
        return None, Decimal(str(product.price or 0)), int(product.stock_quantity or 0)
    variants = product.variants or []
    if not (0 <= index < len(variants)) or not isinstance(variants[index], dict):
        return None
    v = variants[index]
    price = v.get("price")
    try:
        price = Decimal(str(price)) if price not in (None, "") else Decimal(str(product.price or 0))
    except Exception:  # noqa: BLE001
        price = Decimal(str(product.price or 0))
    return variant_label(v, index), price, int(v.get("stock") or 0)


async def lines(db, tenant, cart) -> tuple[list[dict], list[str]]:
    """Lignes du panier relues dans le catalogue (prix et stock du moment) ; les articles disparus sont retirés."""
    from app.models.product import Product
    from app.services.guided_flow import _uuid

    out, notes, keep = [], [], []
    for item in list(cart.items or []):
        product = await db.get(Product, _uuid(str(item.get("product_id"))))
        index = item.get("variant")
        found = _variant_of(product, index) if product is not None and product.tenant_id == tenant.id and product.active else None
        if found is None or (index is not None and found[0] != item.get("label")):
            notes.append(f"{item.get('name') or 'Un article'} n'est plus disponible : retiré du panier.")
            continue
        label, price, stock = found
        quantity = min(int(item.get("quantity") or 0), stock)
        if quantity <= 0:
            notes.append(f"{product.name}{f' ({label})' if label else ''} est épuisé : retiré du panier.")
            continue
        if quantity < int(item.get("quantity") or 0):
            notes.append(f"{product.name}{f' ({label})' if label else ''} : il n'en reste que {quantity}.")
        keep.append({**item, "quantity": quantity})
        out.append({"product": product, "variant": index, "label": label, "price": price, "quantity": quantity,
                    "subtotal": price * quantity, "stock": stock})
    if keep != list(cart.items or []):
        _touch(cart, items=keep)
    return out, notes


def _line_text(line, currency) -> str:
    label = f" ({line['label']})" if line["label"] else ""
    return f"• {line['product'].name}{label} × {line['quantity']} — {_money(line['subtotal'], currency)}"


def _subtotal(items) -> Decimal:
    return sum((line["subtotal"] for line in items), Decimal(0))


def _currency(tenant, items) -> str:
    return (items[0]["product"].currency if items else None) or tenant.currency or "XOF"


# --- Réglages de remise de la boutique ------------------------------------------------------------------------------

def modes(tenant) -> list[str]:
    out = [DELIVERY] if getattr(tenant, "delivery_enabled", True) else []
    if getattr(tenant, "pickup_enabled", False):
        out.append(PICKUP)
    return out or [DELIVERY]


def _dec(value) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def delivery_fee(tenant, subtotal: Decimal) -> Decimal | None:
    """Frais de la boutique, 0 au-delà du seuil de livraison offerte ; None = non précisés (la boutique les annonce)."""
    free_from = _dec(getattr(tenant, "delivery_free_from", None))
    if free_from is not None and free_from > 0 and subtotal >= free_from:
        return Decimal(0)
    return _dec(getattr(tenant, "delivery_fee", None))


def _fee_text(tenant, fee: Decimal | None, currency) -> str:
    zone = f" ({tenant.delivery_zone})" if getattr(tenant, "delivery_zone", None) else ""
    if fee is None:
        return f"Livraison{zone} : frais précisés par la boutique"
    if fee == 0:
        return f"Livraison{zone} : offerte"
    return f"Livraison{zone} : {_money(fee, currency)}"


# --- Les écrans -----------------------------------------------------------------------------------------------------

async def quantities(db, tenant, tu, customer, product, index):
    """Liste « 1 · 2 · 3 · 4 · 5 » avec le montant de chaque ligne, jamais plus que le stock (moins ce qui est au panier)."""
    from app.services.guided_flow import PREFIX, details

    found = _variant_of(product, index)
    if found is None:
        return await details(db, tenant, tu, product)
    label, price, stock = found
    cart = await get_cart(db, tenant, customer)
    already = sum(int(i.get("quantity") or 0) for i in (cart.items or [] if cart else [])
                  if i.get("product_id") == str(product.id) and i.get("variant") == index)
    left = min(stock, MAX_QTY) - already
    name = f"{product.name}{f' ({label})' if label else ''}"
    if left <= 0:
        return _turn([_buttons(f"*{name}* : " + _you(tu, "tout le stock disponible est déjà dans votre panier.",
                                                      "tout le stock disponible est déjà dans ton panier."),
                               [(f"{PREFIX}k", "Voir mon panier"), (f"{PREFIX}cat", "Continuer mes achats")])])
    ref = "-" if index is None else str(index)
    rows = [(f"{PREFIX}q:{product.id}:{ref}:{n}", str(n), _money(price * n, product.currency)) for n in range(1, min(5, left) + 1)]
    if left > 5:
        rows.append((f"{PREFIX}q:{product.id}:{ref}:x", "Autre quantité", f"jusqu'à {left}"))
    body = f"*{name}* — {_money(price, product.currency)} " + _you(tu, "l'unité. Combien en voulez-vous ?",
                                                                    "l'unité. Combien en veux-tu ?")
    return _turn([{"type": "list", "body": body, "button": "Choisir la quantité", "rows": rows}])


async def ask_quantity(db, tenant, tu, customer, product, index):
    found = _variant_of(product, index)
    if found is None:
        return await quantities(db, tenant, tu, customer, product, index)
    cart = await get_cart(db, tenant, customer, create=True)
    _touch(cart, awaiting=f"qty:{product.id}:{'-' if index is None else index}")
    return _turn([{"type": "text", "body": _you(tu, "Écrivez le nombre d'articles souhaité (par exemple 8).",
                                                "Écris le nombre d'articles souhaité (par exemple 8).")}])


async def add(db, tenant, tu, customer, product, index, quantity: int):
    from app.services.guided_flow import PREFIX

    found = _variant_of(product, index)
    if found is None or not product.active:
        return await show(db, tenant, tu, customer, notes=[f"{product.name} n'est plus disponible."])
    label, _, stock = found
    cart = await get_cart(db, tenant, customer, create=True)
    items = [dict(i) for i in (cart.items or [])]
    current = next((i for i in items if i.get("product_id") == str(product.id) and i.get("variant") == index), None)
    already = int(current["quantity"]) if current else 0
    allowed = min(stock, MAX_QTY) - already
    notes = []
    if quantity > allowed:
        if allowed <= 0:
            return _turn([_buttons(_you(tu, "Tout le stock disponible est déjà dans votre panier.",
                                        "Tout le stock disponible est déjà dans ton panier."),
                                   [(f"{PREFIX}k", "Voir mon panier"), (f"{PREFIX}cat", "Continuer mes achats")])])
        notes.append(f"Il n'en reste que {stock} : j'en ai mis {already + allowed} au panier.")
        quantity = allowed
    if current:
        current["quantity"] = already + quantity
    else:
        items.append({"product_id": str(product.id), "variant": index, "label": label, "name": product.name[:80],
                      "quantity": quantity})
    _touch(cart, items=items, awaiting=None)
    return await show(db, tenant, tu, customer, notes=notes, added=True)


async def show(db, tenant, tu, customer, notes=None, added=False):
    """Le panier : lignes, total, puis « Confirmer », « Continuer mes achats », « Vider le panier »."""
    from app.services.guided_flow import PREFIX

    cart = await get_cart(db, tenant, customer)
    items, more_notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    notes = list(notes or []) + more_notes
    if not items:
        body = "\n".join(notes + [_you(tu, "Votre panier est vide.", "Ton panier est vide.")])
        return _turn([_buttons(body, [(f"{PREFIX}cat", "Voir le catalogue"), HUMAN_BUTTON])])
    currency = _currency(tenant, items)
    head = "Ajouté au panier ✅" if added else _you(tu, "Votre panier :", "Ton panier :")
    body = "\n".join(notes + [head, ""] + [_line_text(line, currency) for line in items]
                     + ["", f"*Total : {_money(_subtotal(items), currency)}*"])
    return _turn([_buttons(body, [(f"{PREFIX}ok", "Confirmer"), (f"{PREFIX}cat", "Continuer mes achats"),
                                  (f"{PREFIX}clr", "Vider le panier")])])


async def clear(db, tenant, tu, customer):
    from app.services.guided_flow import PREFIX

    cart = await get_cart(db, tenant, customer)
    if cart is not None:
        _touch(cart, items=[], fulfillment=None, address=None, awaiting=None)
    return _turn([_buttons(_you(tu, "Votre panier est vidé.", "Ton panier est vidé."),
                           [(f"{PREFIX}cat", "Voir le catalogue"), HUMAN_BUTTON])])


async def confirm(db, tenant, tu, customer):
    """« Confirmer » : livraison ou retrait selon la boutique (question sautée s'il n'y a qu'un mode)."""
    from app.services.guided_flow import PREFIX

    cart = await get_cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await _already_or_empty(db, tenant, tu, customer, notes)
    if notes:
        return await show(db, tenant, tu, customer, notes=notes)
    available = modes(tenant)
    if available == [PICKUP]:
        return await choose(db, tenant, tu, customer, PICKUP)
    if available == [DELIVERY]:
        return await choose(db, tenant, tu, customer, DELIVERY)
    currency = _currency(tenant, items)
    parts = [_fee_text(tenant, delivery_fee(tenant, _subtotal(items)), currency)]
    if tenant.pickup_address:
        parts.append(f"Retrait en boutique : {tenant.pickup_address}")
    body = _you(tu, "Comment souhaitez-vous recevoir votre commande ?", "Comment veux-tu recevoir ta commande ?") \
        + "\n\n" + "\n".join(parts)
    return _turn([_buttons(body, [(f"{PREFIX}m:l", "Livraison"), (f"{PREFIX}m:r", "Retrait en boutique"),
                                  (f"{PREFIX}k", "Modifier le panier")])])


async def choose(db, tenant, tu, customer, mode: str):
    from app.services.guided_flow import PREFIX

    cart = await get_cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await _already_or_empty(db, tenant, tu, customer, notes)
    if mode not in modes(tenant):
        return await confirm(db, tenant, tu, customer)
    if mode == PICKUP:
        _touch(cart, fulfillment=PICKUP, address=None, awaiting=None)
        return await recap(db, tenant, tu, customer)
    currency = _currency(tenant, items)
    minimum = _dec(tenant.delivery_min_order)
    if minimum is not None and minimum > 0 and _subtotal(items) < minimum:
        missing = minimum - _subtotal(items)
        buttons = [(f"{PREFIX}cat", "Continuer mes achats")]
        if PICKUP in modes(tenant):
            buttons.append((f"{PREFIX}m:r", "Retrait en boutique"))
        buttons.append(HUMAN_BUTTON)
        body = (f"La livraison est possible à partir de {_money(minimum, currency)} d'achats. "
                + _you(tu, f"Il vous manque {_money(missing, currency)}.", f"Il te manque {_money(missing, currency)}."))
        return _turn([_buttons(body, buttons)])
    _touch(cart, fulfillment=DELIVERY)
    if customer.address:
        _touch(cart, awaiting=None)
        body = _you(tu, "Livraison à cette adresse ?", "Livraison à cette adresse ?") + f"\n\n{customer.address}"
        return _turn([_buttons(body, [(f"{PREFIX}ad:same", "Oui, cette adresse"), (f"{PREFIX}ad:new", "Autre adresse")])])
    return _ask_address(cart, tenant, tu)


def _ask_address(cart, tenant, tu):
    _touch(cart, awaiting="address")
    zone = f" (livraison : {tenant.delivery_zone})" if tenant.delivery_zone else ""
    return _turn([{"type": "text", "body": _you(
        tu, f"Écrivez l'adresse de livraison{zone} : quartier, rue, un repère, et un numéro joignable si ce n'est pas celui-ci.",
        f"Écris l'adresse de livraison{zone} : quartier, rue, un repère, et un numéro joignable si ce n'est pas celui-ci.")}])


async def address_choice(db, tenant, tu, customer, which: str):
    cart = await get_cart(db, tenant, customer)
    if cart is None or not cart.items:
        return await _already_or_empty(db, tenant, tu, customer)
    if which == "same" and customer.address:
        _touch(cart, fulfillment=DELIVERY, address=customer.address[:500], awaiting=None)
        return await recap(db, tenant, tu, customer)
    _touch(cart, fulfillment=DELIVERY)
    return _ask_address(cart, tenant, tu)


async def recap(db, tenant, tu, customer):
    """Récapitulatif : panier, livraison ou retrait, total ; « Valider ma commande » ou « Modifier »."""
    from app.services.guided_flow import PREFIX

    cart = await get_cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await _already_or_empty(db, tenant, tu, customer, notes)
    if notes:
        return await show(db, tenant, tu, customer, notes=notes)
    if cart.fulfillment == DELIVERY and not cart.address:
        return _ask_address(cart, tenant, tu)
    if cart.fulfillment not in modes(tenant):
        return await confirm(db, tenant, tu, customer)
    currency = _currency(tenant, items)
    subtotal = _subtotal(items)
    body = ["*Récapitulatif*", ""] + [_line_text(line, currency) for line in items] + [""]
    total = subtotal
    if cart.fulfillment == DELIVERY:
        fee = delivery_fee(tenant, subtotal)
        body.append(_fee_text(tenant, fee, currency))
        total += fee or 0
        body.append(f"Adresse : {cart.address}")
    else:
        body.append("Retrait en boutique" + (f" : {tenant.pickup_address}" if tenant.pickup_address else ""))
    body += ["", f"*Total : {_money(total, currency)}*"]
    if cart.fulfillment == DELIVERY and delivery_fee(tenant, subtotal) is None:
        body.append("(hors frais de livraison)")
    return _turn([_buttons("\n".join(body), [(f"{PREFIX}val", "Valider ma commande"), (f"{PREFIX}k", "Modifier")])])


async def validate(db, tenant, tu, customer, conversation):
    """« Valider ma commande » : commande créée (stock réservé), panier vidé, confirmation avec le lien de paiement."""
    from app.services.guided_flow import PREFIX
    from app.services.order_service import OrderCreationError, create_order
    from app.services.receipt_service import generate_order_confirmation_text, get_order_item_lines

    cart = await get_cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await _already_or_empty(db, tenant, tu, customer, notes)
    if notes or cart.fulfillment not in modes(tenant) or (cart.fulfillment == DELIVERY and not cart.address):
        return await recap(db, tenant, tu, customer) if not notes else await show(db, tenant, tu, customer, notes=notes)
    fee = delivery_fee(tenant, _subtotal(items)) if cart.fulfillment == DELIVERY else None
    try:
        order = await create_order(
            db, tenant_id=tenant.id, customer_id=customer.id,
            items=[{"product_id": str(line["product"].id), "quantity": line["quantity"], "variant": line["variant"],
                    "label": line["label"]} for line in items],
            delivery_address=cart.address if cart.fulfillment == DELIVERY else None,
            payment_method=None, created_by="IA", conversation_id=conversation.id if conversation else None,
            delivery_fee=float(fee) if fee is not None else None, fulfillment=cart.fulfillment)
    except OrderCreationError as exc:
        return _turn([_buttons(f"{exc.message}.", [(f"{PREFIX}k", "Voir mon panier"),
                                                  HUMAN_BUTTON])])
    _touch(cart, items=[], fulfillment=None, address=None, awaiting=None)
    text = generate_order_confirmation_text(order, await get_order_item_lines(db, order.id, order.currency), tenant.name,
                                            tenant.payment_link, tu=tu)
    if order.fulfillment == PICKUP and tenant.pickup_address:
        text += f"\n\nRetrait : {tenant.pickup_address}"
    buttons = [(f"{PREFIX}s", "Suivre ma commande"), HUMAN_BUTTON]
    if len(text) > 1000:  # limite d'un message à boutons : la confirmation part en texte, les boutons ensuite
        return _turn([{"type": "text", "body": text, "message_type": "order_confirmation"},
                      _buttons(_you(tu, "Et ensuite ?", "Et ensuite ?"), buttons)])
    return _turn([_buttons(text, buttons, message_type="order_confirmation")])


async def _already_or_empty(db, tenant, tu, customer, notes=None):
    """Panier vide : soit la commande vient d'être validée (double appui), soit rien n'a encore été choisi."""
    from app.models.order import Order
    from app.services.guided_flow import PREFIX

    if notes:  # le dernier article vient d'être retiré (épuisé, retiré du catalogue)
        return await show(db, tenant, tu, customer, notes=notes)
    recent = (await db.execute(select(Order).where(
        Order.tenant_id == tenant.id, Order.customer_id == customer.id,
        Order.created_at >= _now() - timedelta(minutes=30)).order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()
    if recent is not None:
        return _turn([_buttons(_you(tu, f"Votre commande n° {_ref(recent)} est bien enregistrée.",
                                    f"Ta commande n° {_ref(recent)} est bien enregistrée."),
                               [(f"{PREFIX}s", "Suivre ma commande"), (f"{PREFIX}cat", "Voir le catalogue")])])
    return await show(db, tenant, tu, customer)


TRACK_TEXT = {
    "CANCELLED": "annulée",
    "PENDING": "enregistrée : la boutique attend la preuve de paiement pour confirmer la vente",
    "PAID": "confirmée : la boutique prépare votre commande",
    "CONFIRMED": "confirmée : la boutique prépare votre commande",
    "PICKED_UP": "en livraison",
    "IN_TRANSIT": "en livraison",
    "DELIVERED": "livrée",
    "DELIVERED_PICKUP": "retirée en boutique",
    "READY_PICKUP": "prête : vous pouvez venir la retirer",
    "FAILED": "pas encore livrée : la boutique vous recontacte",
}


async def track(db, tenant, tu, customer):
    """« Suivre ma commande » : l'état réel de la dernière commande (vente, puis livraison)."""
    from app.models.delivery import Delivery
    from app.models.order import Order
    from app.services.guided_flow import PREFIX

    order = (await db.execute(select(Order).where(Order.tenant_id == tenant.id, Order.customer_id == customer.id)
                              .order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()
    if order is None:
        return _turn([_buttons(_you(tu, "Vous n'avez pas encore de commande chez nous.",
                                    "Tu n'as pas encore de commande chez nous."),
                               [(f"{PREFIX}cat", "Voir le catalogue"), HUMAN_BUTTON])])
    delivery = (await db.execute(select(Delivery).where(Delivery.tenant_id == tenant.id, Delivery.order_id == order.id)
                                 .limit(1))).scalar_one_or_none()
    state = order.status.value
    step = delivery.status.value if delivery is not None else None
    if state != "CANCELLED" and step in ("PICKED_UP", "IN_TRANSIT", "DELIVERED", "FAILED"):
        state = step
        if order.fulfillment == PICKUP and step == "DELIVERED":
            state = "DELIVERED_PICKUP"
        elif order.fulfillment == PICKUP and step in ("PICKED_UP", "IN_TRANSIT"):
            state = "READY_PICKUP"
    text = TRACK_TEXT.get(state, "enregistrée")
    if tu:
        text = text.replace("votre commande", "ta commande").replace("vous pouvez", "tu peux").replace("vous recontacte", "te recontacte")
    from app.services.order_emails import _money as money

    body = f"Commande n° {_ref(order)} ({money(order.total_amount, order.currency)}) : {text}."
    if delivery is not None and delivery.tracking_number and state in ("PICKED_UP", "IN_TRANSIT"):
        body += f"\nN° de suivi : {delivery.tracking_number}"
    return _turn([_buttons(body, [(f"{PREFIX}cat", "Voir le catalogue"), HUMAN_BUTTON])])


# --- Messages écrits attendus (quantité, adresse) et mots-clés -------------------------------------------------------

def _plain(text: str | None) -> str:
    from app.services.guided_flow import _norm

    return _norm(text)


def keyword(text: str | None) -> str | None:
    plain = _plain(text)
    if plain in CART_WORDS:
        return "cart"
    if plain in TRACK_WORDS:
        return "track"
    return None


def _looks_like_address(text: str) -> bool:
    words = (text or "").strip()
    return 3 <= len(words) <= 500 and "?" not in words and not re.fullmatch(r"\d+", words)


async def expects_text(db, tenant, customer, text: str | None) -> bool:
    """Ce message écrit est-il pour le panier (mot-clé, quantité ou adresse attendue) ? Sert à éviter l'analyse IA."""
    if keyword(text):
        return True
    wanted = awaiting(await get_cart(db, tenant, customer))
    if wanted == "address":
        return _looks_like_address(text or "")
    if wanted and wanted.startswith("qty:"):
        return bool(re.fullmatch(r"\s*\d{1,3}\s*", text or ""))
    return False


async def handle_text(db, tenant, tu, customer, text: str | None):
    """Réponse du panier à un message écrit, ou None (l'IA répond)."""
    from app.models.product import Product
    from app.services.guided_flow import _uuid

    word = keyword(text)
    if word == "cart":
        return await show(db, tenant, tu, customer)
    if word == "track":
        return await track(db, tenant, tu, customer)
    cart = await get_cart(db, tenant, customer)
    wanted = awaiting(cart)
    if wanted == "address" and _looks_like_address(text or ""):
        _touch(cart, fulfillment=DELIVERY, address=" ".join((text or "").split())[:500], awaiting=None)
        return await recap(db, tenant, tu, customer)
    if wanted and wanted.startswith("qty:") and re.fullmatch(r"\s*\d{1,3}\s*", text or ""):
        _, raw_product, raw_index = wanted.split(":")
        product = await db.get(Product, _uuid(raw_product))
        if product is None or product.tenant_id != tenant.id:
            _touch(cart, awaiting=None)
            return None
        quantity = int(text.strip())
        index = None if raw_index == "-" else int(raw_index)
        if quantity <= 0:
            return await quantities(db, tenant, tu, customer, product, index)
        return await add(db, tenant, tu, customer, product, index, quantity)
    if wanted:  # le client parle d'autre chose : Bob ne l'attend plus
        _touch(cart, awaiting=None)
    return None


# --- Aiguillage des boutons -----------------------------------------------------------------------------------------

async def handle_tap(db, tenant, tu, customer, conversation, action: str, parts: list[str]):
    """Boutons du panier ; None si l'identifiant n'est pas un bouton du panier."""
    from app.models.product import Product
    from app.services.guided_flow import _uuid, sizes

    if customer is None:
        return None

    async def product_of(raw):
        product = await db.get(Product, _uuid(raw)) if _uuid(raw) else None
        return product if product is not None and product.tenant_id == tenant.id and product.active else None

    if action == "a" and len(parts) == 2:
        product = await product_of(parts[1])
        if product is None:
            return None
        from app.services.guided_flow import _variants

        if _variants(product):
            return await sizes(db, tenant, tu, product)
        return await quantities(db, tenant, tu, customer, product, None)
    if action == "v" and len(parts) == 3 and parts[2].isdigit():
        product = await product_of(parts[1])
        return None if product is None else await quantities(db, tenant, tu, customer, product, int(parts[2]))
    if action == "q" and len(parts) == 4:
        product = await product_of(parts[1])
        if product is None or not (parts[2] == "-" or parts[2].isdigit()):
            return None
        index = None if parts[2] == "-" else int(parts[2])
        if parts[3] == "x":
            return await ask_quantity(db, tenant, tu, customer, product, index)
        if parts[3].isdigit() and int(parts[3]) > 0:
            return await add(db, tenant, tu, customer, product, index, int(parts[3]))
        return None
    if action == "k":
        return await show(db, tenant, tu, customer)
    if action == "clr":
        return await clear(db, tenant, tu, customer)
    if action == "ok":
        return await confirm(db, tenant, tu, customer)
    if action == "m" and len(parts) == 2 and parts[1] in ("l", "r"):
        return await choose(db, tenant, tu, customer, DELIVERY if parts[1] == "l" else PICKUP)
    if action == "ad" and len(parts) == 2:
        return await address_choice(db, tenant, tu, customer, parts[1])
    if action == "val":
        return await validate(db, tenant, tu, customer, conversation)
    if action == "s":
        return await track(db, tenant, tu, customer)
    return None
