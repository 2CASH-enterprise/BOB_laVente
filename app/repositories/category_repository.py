from sqlalchemy import select

from app.models.category import Category
from app.repositories.base import TenantScopedRepository


class CategoryRepository(TenantScopedRepository[Category]):
    model = Category

    async def get_or_create_by_name(self, tenant_id, name: str) -> Category:
        stmt = select(Category).where(Category.tenant_id == tenant_id, Category.name == name)
        result = await self.session.execute(stmt)
        category = result.scalar_one_or_none()
        if category is not None:
            return category

        category = Category(tenant_id=tenant_id, name=name)
        self.session.add(category)
        await self.session.flush()
        return category
