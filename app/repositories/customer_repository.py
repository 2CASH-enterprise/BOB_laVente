from sqlalchemy import select

from app.models.customer import Customer
from app.repositories.base import TenantScopedRepository


class CustomerRepository(TenantScopedRepository[Customer]):
    model = Customer

    async def get_or_create(self, tenant_id, whatsapp_number: str) -> Customer:
        stmt = select(Customer).where(Customer.tenant_id == tenant_id, Customer.whatsapp_number == whatsapp_number)
        result = await self.session.execute(stmt)
        customer = result.scalar_one_or_none()
        if customer is not None:
            return customer

        customer = Customer(tenant_id=tenant_id, whatsapp_number=whatsapp_number)
        self.session.add(customer)
        await self.session.flush()
        return customer
