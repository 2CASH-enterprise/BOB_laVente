import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.plan_limits import (
    FREEMIUM_MAX_CONVERSATIONS_PER_MONTH,
    FREEMIUM_MAX_PRODUCTS,
    can_create_qr_code,
    is_conversation_quota_exceeded,
    remaining_product_slots,
)


async def _setup(db_session, email: str, is_paid: bool = False):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, is_paid=is_paid)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_remaining_product_slots_unlimited_for_paid(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=True)
    remaining = await remaining_product_slots(db_session, tenant.id, tenant.is_paid)
    assert remaining is None


@pytest.mark.asyncio
async def test_remaining_product_slots_freemium_full_at_start(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    remaining = await remaining_product_slots(db_session, tenant.id, tenant.is_paid)
    assert remaining == FREEMIUM_MAX_PRODUCTS


@pytest.mark.asyncio
async def test_remaining_product_slots_decreases_with_products(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    for i in range(3):
        db_session.add(Product(tenant_id=tenant.id, sku=f"P{i}", name=f"Produit {i}", price=1000, currency="XOF", stock_quantity=1))
    await db_session.commit()

    remaining = await remaining_product_slots(db_session, tenant.id, tenant.is_paid)
    assert remaining == FREEMIUM_MAX_PRODUCTS - 3


@pytest.mark.asyncio
async def test_remaining_product_slots_never_negative(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    for i in range(FREEMIUM_MAX_PRODUCTS + 5):
        db_session.add(Product(tenant_id=tenant.id, sku=f"P{i}", name=f"Produit {i}", price=1000, currency="XOF", stock_quantity=1))
    await db_session.commit()

    remaining = await remaining_product_slots(db_session, tenant.id, tenant.is_paid)
    assert remaining == 0


@pytest.mark.asyncio
async def test_conversation_quota_never_exceeded_for_paid(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=True)
    exceeded = await is_conversation_quota_exceeded(db_session, tenant.id, tenant.is_paid)
    assert exceeded is False


@pytest.mark.asyncio
async def test_conversation_quota_not_exceeded_below_limit(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    for _ in range(5):
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE))
    await db_session.commit()

    exceeded = await is_conversation_quota_exceeded(db_session, tenant.id, tenant.is_paid)
    assert exceeded is False


@pytest.mark.asyncio
async def test_conversation_quota_exceeded_above_limit(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, is_paid=False)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    for _ in range(FREEMIUM_MAX_CONVERSATIONS_PER_MONTH + 1):
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE))
    await db_session.commit()

    exceeded = await is_conversation_quota_exceeded(db_session, tenant.id, tenant.is_paid)
    assert exceeded is True


@pytest.mark.asyncio
async def test_can_create_qr_code_under_limit(db_session, unique_email):
    tenant = await _setup(db_session, unique_email)
    assert await can_create_qr_code(db_session, tenant.id) is True


@pytest.mark.asyncio
async def test_can_create_qr_code_at_limit(db_session, unique_email):
    from app.models.product_qr_code import ProductQrCode

    tenant = await _setup(db_session, unique_email)
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=1000, currency="XOF", stock_quantity=1)
    db_session.add(product)
    await db_session.flush()
    for i in range(5):
        db_session.add(ProductQrCode(tenant_id=tenant.id, product_id=product.id, code=f"code{i}"))
    await db_session.commit()

    assert await can_create_qr_code(db_session, tenant.id) is False
