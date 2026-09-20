"""
Repository de base : impose le filtrage par tenant_id sur toute lecture/écriture.

Principe (section 30) : il est interdit qu'une requête du tenant A puisse accéder
aux données du tenant B. En pratique, cela signifie qu'aucun repository ne doit
exposer de méthode permettant de lire un enregistrement sans fournir le tenant_id
du demandeur — même par erreur de programmation.
"""
import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base

ModelType = TypeVar("ModelType", bound=Base)


class TenantScopedRepository(Generic[ModelType]):
    model: type[ModelType]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> ModelType | None:
        """Ne retourne JAMAIS un enregistrement d'un autre tenant, même si record_id existe."""
        stmt = select(self.model).where(
            self.model.id == record_id,  # type: ignore[attr-defined]
            self.model.tenant_id == tenant_id,  # type: ignore[attr-defined]
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(self, tenant_id: uuid.UUID, limit: int = 50, offset: int = 0) -> list[ModelType]:
        stmt = (
            select(self.model)
            .where(self.model.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, instance: ModelType) -> ModelType:
        self.session.add(instance)
        await self.session.flush()
        return instance
