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

    # Section 13 — la commission n'est PLUS calculée ici : elle ne doit être due que sur un
    # paiement réellement confirmé par le commerçant (voir mark_order_as_paid ci-dessous),
    # jamais sur une commande qui pourrait ne jamais être payée.

    await db.flush()
    return order


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
        raise OrderCreationError(f"Cette commande est déjà au statut {order.status.value}, impossible de la marquer payée")

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
