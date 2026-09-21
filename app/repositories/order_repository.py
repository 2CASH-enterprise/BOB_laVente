from app.models.order import Order
from app.repositories.base import TenantScopedRepository


class OrderRepository(TenantScopedRepository[Order]):
    model = Order
