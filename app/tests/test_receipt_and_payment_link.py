import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, payment_link=None):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, payment_link=payment_link)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="Samsung A56", price=100000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(product)
    await db_session.refresh(conversation)
    return tenant, product, conversation


@pytest.mark.asyncio
async def test_create_order_generates_confirmation_not_receipt(db_session, unique_email):
    """Le point clé de l'ajustement : à la création, jamais de reçu — seulement une confirmation."""
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 1}]})
    assert "order_id" in result
    await db_session.commit()

    from sqlalchemy import select

    from app.models.conversation import Message

    confirmation = (await db_session.execute(select(Message).where(Message.message_type == "order_confirmation"))).scalar_one_or_none()
    assert confirmation is not None
    assert "Samsung A56" in confirmation.content
    assert "capture d'écran du paiement" in confirmation.content
    assert "Reçu établi par Bob AI" not in confirmation.content  # jamais présenté comme un reçu

    receipt = (await db_session.execute(select(Message).where(Message.message_type == "receipt"))).scalar_one_or_none()
    assert receipt is None  # aucun reçu tant que le paiement n'est pas confirmé par le commerçant


@pytest.mark.asyncio
async def test_order_confirmation_includes_payment_link_when_configured(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email, payment_link="https://pay.wave.com/xyz")
    executor = ToolExecutor(db_session, tenant.id, conversation)

    await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 1}]})
    await db_session.commit()

    from sqlalchemy import select

    from app.models.conversation import Message

    confirmation = (await db_session.execute(select(Message).where(Message.message_type == "order_confirmation"))).scalar_one()
    assert "https://pay.wave.com/xyz" in confirmation.content


@pytest.mark.asyncio
async def test_share_payment_link_returns_configured_link(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email, payment_link="https://pay.wave.com/xyz")
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("share_payment_link", {})
    assert result["payment_link"] == "https://pay.wave.com/xyz"


@pytest.mark.asyncio
async def test_share_payment_link_none_configured(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email, payment_link=None)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("share_payment_link", {})
    assert "error" in result
