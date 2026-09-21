"""
Création de commande (section 18).

Avant création : 1. vérifier le stock, 2. calculer le total, 3. le client doit avoir
confirmé explicitement (imposé par le prompt système, section 20, règle 8 — cette
fonction elle-même ne peut pas vérifier une conversation, mais elle ne fait jamais
confiance à un prix ou un stock fourni en paramètre : tout est relu depuis la base).
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import Delivery
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.repositories.product_repository import ProductRepository


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
) -> Order:
    if not items:
        raise OrderCreationError("Aucun article dans la commande")

    product_repo = ProductRepository(db)
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

        product: Product | None = await product_repo.get(tenant_id=tenant_id, record_id=product_id)
        if product is None or not product.active:
            raise OrderCreationError(f"Produit introuvable ou indisponible : {product_id}")

        # Vérification stock RÉELLE au moment de la commande (section 18.1, 33) — jamais celle
        # que l'IA a pu voir plus tôt dans la conversation, qui peut être obsolète.
        if product.stock_quantity < quantity:
            raise OrderCreationError(
                f"Stock insuffisant pour {product.name} : demandé {quantity}, disponible {product.stock_quantity}"
            )

        if currency is None:
            currency = product.currency
        elif currency != product.currency:
            raise OrderCreationError("Impossible de mélanger plusieurs devises dans une même commande")

        unit_price = float(product.price)
        subtotal = unit_price * quantity
        total += subtotal

        order_items.append(
            OrderItem(product_id=product.id, quantity=quantity, unit_price=unit_price, subtotal=subtotal)
        )
        # Décrément immédiat du stock (réservation) — cohérent avec section 18 : la commande
        # engage réellement le stock dès sa création, pas seulement à la livraison.
        product.stock_quantity -= quantity

    order = Order(
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

    await db.flush()
    return order
