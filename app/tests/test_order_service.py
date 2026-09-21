import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.order import OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.order_service import OrderCreationError, create_order


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(product)
    await db_session.refresh(customer)
    return tenant, product, customer


@pytest.mark.asyncio
async def test_create_order_success_computes_total_and_decrements_stock(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email)

    order = await create_order(
        db=db_session,
        tenant_id=tenant.id,
        customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 2}],
        delivery_address="Dakar",
        payment_method="cash",
        created_by="IA",
    )
    await db_session.commit()
    await db_session.refresh(product)

    assert order.status == OrderStatus.PENDING
    assert float(order.total_amount) == 560000.0
    assert order.currency == "XOF"
    assert product.stock_quantity == 3  # 5 - 2


@pytest.mark.asyncio
async def test_create_order_rejects_insufficient_stock(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email)

    with pytest.raises(OrderCreationError, match="Stock insuffisant"):
        await create_order(
            db=db_session,
            tenant_id=tenant.id,
            customer_id=customer.id,
            items=[{"product_id": str(product.id), "quantity": 999}],
            delivery_address=None,
            payment_method=None,
            created_by="IA",
        )


@pytest.mark.asyncio
async def test_create_order_rejects_unknown_product(db_session, unique_email):
    import uuid

    tenant, product, customer = await _setup(db_session, unique_email)

    with pytest.raises(OrderCreationError, match="introuvable"):
        await create_order(
            db=db_session,
            tenant_id=tenant.id,
            customer_id=customer.id,
            items=[{"product_id": str(uuid.uuid4()), "quantity": 1}],
            delivery_address=None,
            payment_method=None,
            created_by="IA",
        )


@pytest.mark.asyncio
async def test_create_order_rejects_empty_items(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email)

    with pytest.raises(OrderCreationError, match="Aucun article"):
        await create_order(
            db=db_session,
            tenant_id=tenant.id,
            customer_id=customer.id,
            items=[],
            delivery_address=None,
            payment_method=None,
            created_by="IA",
        )


@pytest.mark.asyncio
async def test_create_order_cannot_use_another_tenants_product(db_session, unique_email):
    """Isolation multi-tenant appliquée aussi à la création de commande (section 30)."""
    tenant_a, product_a, customer_a = await _setup(db_session, unique_email)
    tenant_b, product_b, customer_b = await _setup(db_session, f"b_{unique_email}")

    with pytest.raises(OrderCreationError, match="introuvable"):
        await create_order(
            db=db_session,
            tenant_id=tenant_b.id,
            customer_id=customer_b.id,
            items=[{"product_id": str(product_a.id), "quantity": 1}],
            delivery_address=None,
            payment_method=None,
            created_by="IA",
        )
