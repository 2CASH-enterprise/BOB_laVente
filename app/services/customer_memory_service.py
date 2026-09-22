"""
Mémoire client au-delà d'une conversation. Toute donnée vient directement de la base
(commandes réelles, vues réellement enregistrées) — jamais une supposition du LLM.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.customer_product_view import CustomerProductView
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product

MAX_PAST_ORDERS = 5
MAX_VIEWED_PRODUCTS = 5


async def build_customer_memory(db: AsyncSession, tenant_id, customer_id) -> str:
    """Retourne un texte prêt à injecter dans le prompt système, ou une chaîne vide si rien à dire."""
    orders_stmt = (
        select(Order)
        .where(Order.tenant_id == tenant_id, Order.customer_id == customer_id, Order.status != OrderStatus.CANCELLED)
        .order_by(Order.created_at.desc())
        .limit(MAX_PAST_ORDERS)
    )
    past_orders = list((await db.execute(orders_stmt)).scalars().all())

    purchased_product_ids: set = set()
    order_lines = []
    for order in past_orders:
        items_stmt = (
            select(OrderItem, Product.name)
            .join(Product, Product.id == OrderItem.product_id)
            .where(OrderItem.order_id == order.id)
        )
        items = (await db.execute(items_stmt)).all()
        names = []
        for item, product_name in items:
            purchased_product_ids.add(item.product_id)
            names.append(f"{product_name} (x{item.quantity})")
        if names:
            order_lines.append(f"- {order.created_at.strftime('%d/%m/%Y')} : {', '.join(names)} — statut {order.status.value}")

    views_stmt = (
        select(CustomerProductView, Product.name)
        .join(Product, Product.id == CustomerProductView.product_id)
        .where(CustomerProductView.tenant_id == tenant_id, CustomerProductView.customer_id == customer_id)
        .order_by(CustomerProductView.last_viewed_at.desc())
        .limit(MAX_VIEWED_PRODUCTS + len(purchased_product_ids))  # marge pour filtrer les déjà achetés
    )
    views = (await db.execute(views_stmt)).all()
    viewed_lines = [
        f"- {product_name}" for view, product_name in views if view.product_id not in purchased_product_ids
    ][:MAX_VIEWED_PRODUCTS]

    preferences_lines = await _format_preferences(db, tenant_id, customer_id)

    if not order_lines and not viewed_lines and not preferences_lines:
        return ""

    sections = ["MÉMOIRE CLIENT (ce client a déjà échangé avec toi par le passé)"]
    if order_lines:
        sections.append("Commandes précédentes :\n" + "\n".join(order_lines))
    if viewed_lines:
        sections.append("Produits déjà consultés sans achat :\n" + "\n".join(viewed_lines))
    if preferences_lines:
        sections.append(
            "Préférences détectées lors d'échanges précédents (INFÉRENCE, pas un fait vérifié) :\n"
            + preferences_lines
        )
    sections.append(
        "Tu peux t'appuyer sur cet historique pour personnaliser ton accueil (ex. reconnaître un client "
        "qui revient, proposer un produit déjà consulté) — mais ne mentionne jamais un détail qui n'est "
        "pas listé ci-dessus, et ne dis jamais explicitement que tu consultes un « historique »."
    )
    return "\n\n" + "\n\n".join(sections)


async def _format_preferences(db: AsyncSession, tenant_id, customer_id) -> str:
    customer = await db.get(Customer, customer_id)
    if customer is None or not customer.detected_preferences:
        return ""
    prefs = customer.detected_preferences
    lines = []
    if prefs.get("need"):
        lines.append(f"- Recherche : {prefs['need']}")
    if prefs.get("brand"):
        lines.append(f"- Marque préférée : {prefs['brand']}")
    if prefs.get("budget_max"):
        lines.append(f"- Budget maximum mentionné : {prefs['budget_max']}")
    return "\n".join(lines)


async def record_product_view(db: AsyncSession, tenant_id, customer_id, product_ids: list) -> None:
    """Appelé après search_products/recommend_products : enregistre les produits vus par ce client."""
    if not customer_id or not product_ids:
        return

    for product_id in product_ids:
        stmt = select(CustomerProductView).where(
            CustomerProductView.tenant_id == tenant_id,
            CustomerProductView.customer_id == customer_id,
            CustomerProductView.product_id == product_id,
        )
        view = (await db.execute(stmt)).scalar_one_or_none()
        if view is not None:
            view.view_count += 1
        else:
            db.add(
                CustomerProductView(tenant_id=tenant_id, customer_id=customer_id, product_id=product_id, view_count=1)
            )
    await db.flush()
