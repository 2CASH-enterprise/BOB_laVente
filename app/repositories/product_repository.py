from sqlalchemy import func, select

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.repositories.base import TenantScopedRepository


class ProductRepository(TenantScopedRepository[Product]):
    model = Product

    async def get_by_sku(self, tenant_id, sku: str) -> Product | None:
        stmt = select(Product).where(Product.tenant_id == tenant_id, Product.sku == sku)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_external_id(self, tenant_id, external_source: str, external_id: str) -> Product | None:
        """Utilisé pour l'upsert lors d'une synchronisation externe (section 25) — SKU non fiable."""
        stmt = select(Product).where(
            Product.tenant_id == tenant_id,
            Product.external_source == external_source,
            Product.external_id == external_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def search(
        self,
        tenant_id,
        query: str | None = None,
        category_id=None,
        min_price: float | None = None,
        max_price: float | None = None,
        active_only: bool = True,
        limit: int = 20,
    ) -> list[Product]:
        """
        Base du futur outil IA `search_products` (section 14). Ne renvoie JAMAIS un produit
        d'un autre tenant (filtre tenant_id systématique, section 30), ni un prix/stock
        inventé : ces valeurs viennent uniquement de cette requête, jamais du LLM (section 33).
        """
        stmt = select(Product).where(Product.tenant_id == tenant_id)

        if active_only:
            stmt = stmt.where(Product.active.is_(True))
        if query:
            stmt = stmt.where(Product.name.ilike(f"%{query}%"))
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        if min_price is not None:
            stmt = stmt.where(Product.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(Product.price <= max_price)

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_sales_counts(self, tenant_id, product_ids: list) -> dict:
        """
        Popularité réelle (point 5) : quantité totale vendue par produit, commandes annulées
        exclues. Retourne un dict {product_id: quantité}, 0 si jamais vendu — jamais une
        estimation, uniquement ce qui a réellement été commandé.
        """
        if not product_ids:
            return {}
        stmt = (
            select(OrderItem.product_id, func.sum(OrderItem.quantity))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.tenant_id == tenant_id, Order.status != OrderStatus.CANCELLED, OrderItem.product_id.in_(product_ids))
            .group_by(OrderItem.product_id)
        )
        rows = (await self.session.execute(stmt)).all()
        counts = {product_id: int(qty) for product_id, qty in rows}
        return {pid: counts.get(pid, 0) for pid in product_ids}

    async def get_frequently_bought_together(self, tenant_id, product_id, limit: int = 3) -> list[Product]:
        """
        Associations « souvent achetés ensemble » (point 5) : calculées depuis les VRAIES
        commandes passées (co-occurrence dans une même commande), jamais une supposition.
        """
        co_orders_stmt = (
            select(OrderItem.order_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.tenant_id == tenant_id, Order.status != OrderStatus.CANCELLED, OrderItem.product_id == product_id)
        )
        order_ids = [row[0] for row in (await self.session.execute(co_orders_stmt)).all()]
        if not order_ids:
            return []

        stmt = (
            select(OrderItem.product_id, func.count(OrderItem.order_id.distinct()).label("co_count"))
            .where(OrderItem.order_id.in_(order_ids), OrderItem.product_id != product_id)
            .group_by(OrderItem.product_id)
            .order_by(func.count(OrderItem.order_id.distinct()).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        product_ids = [row[0] for row in rows]
        if not product_ids:
            return []

        products_stmt = select(Product).where(
            Product.tenant_id == tenant_id, Product.id.in_(product_ids), Product.active.is_(True)
        )
        products = {p.id: p for p in (await self.session.execute(products_stmt)).scalars().all()}
        # Préserve l'ordre de co-occurrence (le plus fréquent en premier), pas l'ordre de la requête produits.
        return [products[pid] for pid in product_ids if pid in products]
