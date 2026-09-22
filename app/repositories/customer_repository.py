from sqlalchemy import select

from app.models.customer import Customer
from app.repositories.base import TenantScopedRepository


class CustomerRepository(TenantScopedRepository[Customer]):
    model = Customer

    async def get_or_create(self, tenant_id, whatsapp_number: str) -> Customer:
        customer, _ = await self.get_or_create_with_created_flag(tenant_id, whatsapp_number)
        return customer

    async def get_or_create_with_created_flag(self, tenant_id, whatsapp_number: str) -> tuple[Customer, bool]:
        """
        Utilisé par le webhook pour l'attribution QR (section CRM.7) : on ne doit
        attribuer une source d'acquisition qu'au tout premier contact, jamais réécrire
        la source d'un client déjà connu.
        """
        stmt = select(Customer).where(Customer.tenant_id == tenant_id, Customer.whatsapp_number == whatsapp_number)
        result = await self.session.execute(stmt)
        customer = result.scalar_one_or_none()
        if customer is not None:
            return customer, False

        customer = Customer(tenant_id=tenant_id, whatsapp_number=whatsapp_number)
        self.session.add(customer)
        await self.session.flush()
        return customer, True
