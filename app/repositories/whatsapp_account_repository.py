from sqlalchemy import select

from app.models.whatsapp_account import WhatsAppAccount
from app.repositories.base import TenantScopedRepository


class WhatsAppAccountRepository(TenantScopedRepository[WhatsAppAccount]):
    model = WhatsAppAccount

    async def find_tenant_by_phone_number_id(self, phone_number_id: str) -> WhatsAppAccount | None:
        """
        Section 59.5 — routage du webhook : retrouve le tenant propriétaire d'un numéro,
        AVANT tout traitement métier. C'est le seul endroit où on lit un WhatsAppAccount
        sans connaître le tenant_id à l'avance (le webhook Meta ne le fournit pas).
        """
        stmt = select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
