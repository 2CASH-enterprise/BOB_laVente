import pytest

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.order import OrderStatus
from app.models.order_commission import OrderCommission
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.order_service import OrderCreationError, create_order, mark_order_as_paid


async def _setup(db_session, email: str, commission_rate=None):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, commission_rate=commission_rate)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="Samsung A56", price=100000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(tenant)
    await db_session.refresh(product)
    await db_session.refresh(customer)
    return tenant, product, customer


@pytest.mark.asyncio
async def test_order_creation_never_creates_commission(db_session, unique_email):
    """Le point central de l'ajustement : la création seule ne doit jamais déclencher de commission."""
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=5.0)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 2}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    from sqlalchemy import select

    commissions = (await db_session.execute(select(OrderCommission).where(OrderCommission.order_id == order.id))).scalars().all()
    assert commissions == []
    assert order.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_mark_paid_creates_correct_commission(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=5.0)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 2}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    await mark_order_as_paid(db_session, tenant.id, order.id)
    await db_session.commit()

    from sqlalchemy import select

    commission = (await db_session.execute(select(OrderCommission).where(OrderCommission.order_id == order.id))).scalar_one()
    assert commission.order_total_amount == 200000
    assert commission.commission_rate_applied == 5.0
    assert commission.commission_amount == 10000


@pytest.mark.asyncio
async def test_mark_paid_sets_order_status(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    updated = await mark_order_as_paid(db_session, tenant.id, order.id)
    assert updated.status == OrderStatus.PAID


@pytest.mark.asyncio
async def test_mark_paid_without_commission_rate_creates_no_line(db_session, unique_email):
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=None)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    await mark_order_as_paid(db_session, tenant.id, order.id)
    await db_session.commit()

    from sqlalchemy import select

    commissions = (await db_session.execute(select(OrderCommission).where(OrderCommission.order_id == order.id))).scalars().all()
    assert commissions == []


@pytest.mark.asyncio
async def test_commission_rate_frozen_even_if_tenant_rate_changes_later(db_session, unique_email):
    """Le taux appliqué reste celui en vigueur au moment du clic « Paiement reçu »."""
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=5.0)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    await mark_order_as_paid(db_session, tenant.id, order.id)
    await db_session.commit()

    tenant.commission_rate = 20.0
    await db_session.commit()

    from sqlalchemy import select

    commission = (await db_session.execute(select(OrderCommission).where(OrderCommission.order_id == order.id))).scalar_one()
    assert commission.commission_rate_applied == 5.0


@pytest.mark.asyncio
async def test_mark_paid_twice_rejected(db_session, unique_email):
    """Impossible de déclencher deux fois le reçu/la commission pour la même commande."""
    tenant, product, customer = await _setup(db_session, unique_email, commission_rate=5.0)
    order = await create_order(
        db=db_session, tenant_id=tenant.id, customer_id=customer.id,
        items=[{"product_id": str(product.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    await mark_order_as_paid(db_session, tenant.id, order.id)
    await db_session.commit()

    with pytest.raises(OrderCreationError):
        await mark_order_as_paid(db_session, tenant.id, order.id)


@pytest.mark.asyncio
async def test_mark_paid_isolated_by_tenant(db_session, unique_email):
    tenant_a, product_a, customer_a = await _setup(db_session, unique_email)
    tenant_b, product_b, customer_b = await _setup(db_session, f"b_{unique_email}")

    order_a = await create_order(
        db=db_session, tenant_id=tenant_a.id, customer_id=customer_a.id,
        items=[{"product_id": str(product_a.id), "quantity": 1}], delivery_address=None, payment_method=None, created_by="IA",
    )
    await db_session.commit()

    with pytest.raises(OrderCreationError):
        await mark_order_as_paid(db_session, tenant_b.id, order_a.id)
