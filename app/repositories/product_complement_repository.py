from sqlalchemy import select

from app.models.product import Product
from app.models.product_complement import ProductComplement
from app.repositories.base import TenantScopedRepository


class ProductComplementRepository(TenantScopedRepository[ProductComplement]):
    model = ProductComplement

    async def list_for_product(self, tenant_id, product_id, limit: int = 2) -> list[Product]:
        """
        Section 22 — jamais plus de 2 propositions complémentaires. Retourne directement
        les produits réels (jointure), jamais juste les liaisons, pour que le prix/stock
        affiché soit toujours à jour (section 33).
        """
        stmt = (
            select(Product)
            .join(ProductComplement, ProductComplement.complement_product_id == Product.id)
            .where(
                ProductComplement.tenant_id == tenant_id,
                ProductComplement.product_id == product_id,
                Product.active.is_(True),
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
