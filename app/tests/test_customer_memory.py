import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.customer_product_view import CustomerProductView
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.customer_memory_service import build_customer_memory, record_product_view


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    product_a = Product(tenant_id=tenant.id, sku="A", name="Samsung A56", price=280000, currency="XOF", stock_quantity=5)
    product_b = Product(tenant_id=tenant.id, sku="B", name="Coque", price=8000, currency="XOF", stock_quantity=10)
    db_session.add_all([product_a, product_b])
    await db_session.commit()
    await db_session.refresh(customer)
    await db_session.refresh(product_a)
    await db_session.refresh(product_b)
    return tenant, customer, product_a, product_b


@pytest.mark.asyncio
async def test_empty_memory_returns_empty_string(db_session, unique_email):
    tenant, customer, product_a, product_b = await _setup(db_session, unique_email)
    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert memory == ""


@pytest.mark.asyncio
async def test_memory_includes_past_order(db_session, unique_email):
    tenant, customer, product_a, product_b = await _setup(db_session, unique_email)
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=280000, currency="XOF", created_by="IA")
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=product_a.id, quantity=1, unit_price=280000, subtotal=280000))
    await db_session.commit()

    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert "MÉMOIRE CLIENT" in memory
    assert "Samsung A56" in memory
    assert "Commandes précédentes" in memory


@pytest.mark.asyncio
async def test_memory_includes_viewed_products_not_purchased(db_session, unique_email):
    tenant, customer, product_a, product_b = await _setup(db_session, unique_email)
    await record_product_view(db_session, tenant.id, customer.id, [product_a.id])
    await db_session.commit()

    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert "Produits déjà consultés sans achat" in memory
    assert "Samsung A56" in memory


@pytest.mark.asyncio
async def test_purchased_product_excluded_from_viewed_section(db_session, unique_email):
    """Un produit acheté ne doit jamais réapparaître dans « consulté sans achat »."""
    tenant, customer, product_a, product_b = await _setup(db_session, unique_email)
    await record_product_view(db_session, tenant.id, customer.id, [product_a.id])
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=280000, currency="XOF", created_by="IA")
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=product_a.id, quantity=1, unit_price=280000, subtotal=280000))
    await db_session.commit()

    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert "Produits déjà consultés sans achat" not in memory  # tout ce qui a été vu a aussi été acheté
    assert "Samsung A56" in memory  # apparaît côté commandes


@pytest.mark.asyncio
async def test_record_product_view_increments_count(db_session, unique_email):
    tenant, customer, product_a, product_b = await _setup(db_session, unique_email)
    await record_product_view(db_session, tenant.id, customer.id, [product_a.id])
    await record_product_view(db_session, tenant.id, customer.id, [product_a.id])
    await db_session.commit()

    from sqlalchemy import select

    view = (await db_session.execute(select(CustomerProductView))).scalar_one()
    assert view.view_count == 2


@pytest.mark.asyncio
async def test_memory_isolated_by_tenant(db_session, unique_email):
    tenant_a, customer_a, product_a1, product_a2 = await _setup(db_session, unique_email)
    tenant_b, customer_b, product_b1, product_b2 = await _setup(db_session, f"b_{unique_email}")

    await record_product_view(db_session, tenant_a.id, customer_a.id, [product_a1.id])
    await db_session.commit()

    memory_b = await build_customer_memory(db_session, tenant_b.id, customer_b.id)
    assert memory_b == ""  # rien du tenant A ne fuite
