from sqlalchemy import select

from app.models.knowledge_entry import KnowledgeEntry
from app.repositories.base import TenantScopedRepository


class KnowledgeEntryRepository(TenantScopedRepository[KnowledgeEntry]):
    model = KnowledgeEntry

    async def list_active(self, tenant_id) -> list[KnowledgeEntry]:
        """Utilisé par l'agent IA (section 20) — uniquement les entrées actives, jamais désactivées."""
        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.tenant_id == tenant_id, KnowledgeEntry.active.is_(True)
        ).order_by(KnowledgeEntry.category)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
