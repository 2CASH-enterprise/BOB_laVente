from sqlalchemy import select

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
