"""
Création de commande (section 18).

Avant création : 1. vérifier le stock, 2. calculer le total, 3. le client doit avoir
confirmé explicitement (imposé par le prompt système, section 20, règle 8 — cette
fonction elle-même ne peut pas vérifier une conversation, mais elle ne fait jamais
confiance à un prix ou un stock fourni en paramètre : tout est relu depuis la base).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import Delivery
from app.models.order import Order, OrderItem, OrderStatus
from app.models.order_commission import OrderCommission
from app.models.product import Product
from app.models.tenant import Tenant


class OrderCreationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class OrderItemRequest:
    def __init__(self, product_id: str, quantity: int):
        self.product_id = product_id
        self.quantity = quantity


async def create_order(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    items: list[dict],
    delivery_address: str | None,
    payment_method: str | None,
    created_by: str,
    conversation_id: uuid.UUID | None = None,
    delivery_fee: float | None = None,
    fulfillment: str | None = None,
) -> Order:
    """
    Lot 95 : une ligne peut porter « variant » (position dans les variantes du produit) et « label » (« Taille 42 ») :
    le prix et le stock sont alors ceux de la variante, relus ici ; un libellé qui ne correspond plus (catalogue
    modifié entre-temps) refuse la ligne plutôt que de vendre une autre taille. Les frais de livraison s'ajoutent au
    total sur une ligne à part (Order.delivery_fee).
    """
    if not items:
        raise OrderCreationError("Aucun article dans la commande")

    # Lot 73 : le client est un client de CETTE boutique (jamais une commande rattachée au client d'une autre).
    from app.models.customer import Customer

    customer = await db.get(Customer, customer_id) if customer_id else None
    if customer is None or customer.tenant_id != tenant_id:
        raise OrderCreationError("Client introuvable")


    order_items: list[OrderItem] = []
    total = 0.0
    currency = None

    for item in items:
        try:
            product_id = uuid.UUID(str(item["product_id"]))
            quantity = int(item["quantity"])
        except (KeyError, ValueError, TypeError) as exc:
            raise OrderCreationError("Article de commande invalide") from exc

        if quantity <= 0:
            raise OrderCreationError("La quantité doit être positive")

        # Lot 73 : ligne verrouillée jusqu'à la fin de la commande (PostgreSQL) : deux commandes simultanées ne
        # peuvent pas vendre le même dernier article.
        from sqlalchemy import select

        product: Product | None = (await db.execute(select(Product).where(
            Product.id == product_id, Product.tenant_id == tenant_id).with_for_update())).scalar_one_or_none()
        if product is None or not product.active:
            raise OrderCreationError(f"Produit introuvable ou indisponible : {product_id}")

        variant_index, chosen_label, variant = _variant(product, item)
        # Vérification stock RÉELLE au moment de la commande (section 18.1, 33) — jamais celle
        # que l'IA a pu voir plus tôt dans la conversation, qui peut être obsolète.
        available = int(variant.get("stock") or 0) if variant is not None else product.stock_quantity
        if available < quantity:
            name = f"{product.name} ({chosen_label})" if chosen_label else product.name
            raise OrderCreationError(f"Stock insuffisant pour {name} : demandé {quantity}, disponible {available}")

        if currency is None:
            currency = product.currency
        elif currency != product.currency:
            raise OrderCreationError("Impossible de mélanger plusieurs devises dans une même commande")

        unit_price = _variant_price(product, variant)
        subtotal = unit_price * quantity
        total += subtotal

        order_items.append(OrderItem(product_id=product.id, quantity=quantity, unit_price=unit_price, subtotal=subtotal,
                                     variant_index=variant_index, variant_label=chosen_label))
        # Décrément immédiat du stock (réservation) — cohérent avec section 18 : la commande
        # engage réellement le stock dès sa création, pas seulement à la livraison.
        if variant is not None:
            _move_variant_stock(product, variant_index, -quantity)
            product.stock_quantity = max(0, (product.stock_quantity or 0) - quantity)
        else:
            product.stock_quantity -= quantity

    fee = round(float(delivery_fee), 2) if delivery_fee is not None else None
    if fee is not None and fee < 0:
        raise OrderCreationError("Frais de livraison invalides")
    if fee:
        total += fee

    order = Order(
        delivery_fee=fee,
        fulfillment=fulfillment,
        tenant_id=tenant_id,
        customer_id=customer_id,
        conversation_id=conversation_id,
        status=OrderStatus.PENDING,
        total_amount=total,
        currency=currency,
        delivery_address=delivery_address,
        payment_method=payment_method,
        created_by=created_by,
    )
    db.add(order)
    await db.flush()

    for oi in order_items:
        oi.order_id = order.id
        db.add(oi)

    # Section 40 — chaque commande a un suivi de livraison, même sans adresse renseignée
    # (le commerce pourra la compléter depuis le dashboard).
    db.add(Delivery(tenant_id=tenant_id, order_id=order.id, address=delivery_address))
    if delivery_address and str(delivery_address).strip():
        # Lot 86 : l'adresse de la dernière commande devient l'adresse de livraison habituelle du client.
        customer.address = " ".join(str(delivery_address).split())[:500]

    # Section 13 — la commission n'est PLUS calculée ici : elle ne doit être due que sur un
    # paiement réellement confirmé par le commerçant (voir mark_order_as_paid ci-dessous),
    # jamais sur une commande qui pourrait ne jamais être payée.

    await db.flush()
    return order


def variant_label(variant: dict, index: int) -> str:
    """« Taille 42 », « Couleur Rouge · Taille M » : le même libellé que le parcours à boutons (lot 94)."""
    options = variant.get("options") or {}
    return " · ".join(f"{k} {val}" for k, val in options.items() if val not in (None, "")) or (variant.get("sku") or f"Modèle {index + 1}")


def _variant(product: Product, item: dict) -> tuple[int | None, str | None, dict | None]:
    raw = item.get("variant")
    if raw in (None, ""):
        return None, None, None
    try:
        index = int(raw)
    except (TypeError, ValueError) as exc:
        raise OrderCreationError("Taille ou modèle invalide") from exc
    variants = product.variants or []
    variant = variants[index] if 0 <= index < len(variants) and isinstance(variants[index], dict) else None
    label = variant_label(variant, index) if variant is not None else None
    if variant is None or (item.get("label") and str(item["label"]) != label):
        raise OrderCreationError(f"La taille ou le modèle choisi pour {product.name} n'est plus disponible")
    return index, label, variant


def _variant_price(product: Product, variant: dict | None) -> float:
    if variant is not None and variant.get("price") not in (None, ""):
        try:
            return float(variant["price"])
        except (TypeError, ValueError):
            pass
    return float(product.price)


def _move_variant_stock(product: Product, index: int, delta: int, label: str | None = None) -> None:
    """Le JSON des variantes est recopié (sinon SQLAlchemy ne voit pas le changement)."""
    variants = [dict(v) if isinstance(v, dict) else v for v in (product.variants or [])]
    if not (0 <= index < len(variants)) or not isinstance(variants[index], dict):
        return
    if label is not None and variant_label(variants[index], index) != label:
        return  # catalogue réorganisé depuis la vente : on ne touche pas une autre taille
    variants[index]["stock"] = max(0, int(variants[index].get("stock") or 0) + delta)
    product.variants = variants


async def mark_order_as_paid(db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    """
    Section 13/39 — LE seul déclencheur du reçu client et de la commission. Appelé
    uniquement par un humain côté commerce, après vérification d'une preuve de paiement
    (capture d'écran mobile money) — jamais automatique tant qu'aucun vrai prestataire
    de paiement n'est intégré.
    """
    order = await db.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise OrderCreationError("Commande introuvable")
    if order.status != OrderStatus.PENDING:
        raise OrderCreationError(f"Cette commande est déjà au statut {order.status.value}, impossible de confirmer la vente")

    order.status = OrderStatus.PAID
    order.paid_at = datetime.now(timezone.utc)

    tenant = await db.get(Tenant, tenant_id)
    if tenant is not None and tenant.commission_rate is not None and float(tenant.commission_rate) > 0:
        commission_amount = round(float(order.total_amount) * float(tenant.commission_rate) / 100, 2)
        db.add(
            OrderCommission(
                tenant_id=tenant_id,
                order_id=order.id,
                order_total_amount=order.total_amount,
                commission_rate_applied=tenant.commission_rate,
                commission_amount=commission_amount,
                currency=order.currency,
            )
        )

    await db.flush()
    return order


# Livraison déjà partie : l'annulation n'a plus de sens côté commerce (la marchandise a quitté le stock).
_DELIVERY_ALREADY_LEFT = frozenset({"PICKED_UP", "IN_TRANSIT", "DELIVERED"})


async def cancel_order(db: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    """
    Annulation par un HUMAIN côté commerce uniquement (jamais par l'IA : action commerciale
    irréversible). Seule une commande en attente de paiement peut être annulée ; une commande
    payée relèverait d'un remboursement. Les articles réservés à la création sont remis en
    stock, et une livraison pas encore partie est annulée avec la commande.
    Ne commite pas : l'appelant trace l'audit et commite en une seule transaction.
    """
    from sqlalchemy import select

    from app.models.delivery import Delivery, DeliveryStatus

    order = await db.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise OrderCreationError("Commande introuvable")
    if order.status == OrderStatus.PAID:
        raise OrderCreationError("Cette vente est confirmée : elle ne peut pas être annulée (un remboursement n'est pas géré ici)")
    if order.status != OrderStatus.PENDING:
        raise OrderCreationError(f"Cette commande est déjà au statut {order.status.value}")

    deliveries = (await db.execute(
        select(Delivery).where(Delivery.order_id == order.id, Delivery.tenant_id == tenant_id)
    )).scalars().all()
    if any(d.status.value in _DELIVERY_ALREADY_LEFT for d in deliveries):
        raise OrderCreationError("La livraison de cette commande est déjà partie : annulation impossible")

    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    for item in items:
        product = await db.get(Product, item.product_id)
        if product is not None and product.tenant_id == tenant_id:
            product.stock_quantity += item.quantity  # même produit désactivé : le stock reste juste
            if item.variant_index is not None:  # lot 95 : la taille vendue retrouve aussi son stock
                _move_variant_stock(product, item.variant_index, item.quantity, item.variant_label)

    for delivery in deliveries:
        if delivery.status not in (DeliveryStatus.CANCELLED, DeliveryStatus.FAILED):
            delivery.status = DeliveryStatus.CANCELLED

    order.status = OrderStatus.CANCELLED
    await db.flush()
    return order


def build_cancellation_message(order: Order, tu: bool = False) -> str:
    """Message fixe, jamais rédigé par l'IA — même numéro et même format de montant que le reçu."""
    if tu:  # lot 38
        return (
            f"Ta commande n° {str(order.id)[:8].upper()} "
            f"({float(order.total_amount):,.0f} {order.currency}) a été annulée. "
            "N'hésite pas à nous écrire si tu as des questions."
        )
    return (
        f"Votre commande n° {str(order.id)[:8].upper()} "
        f"({float(order.total_amount):,.0f} {order.currency}) a été annulée. "
        "N'hésitez pas à nous écrire si vous avez des questions."
    )
