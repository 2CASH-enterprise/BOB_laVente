import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str, negotiation_enabled=True):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="X", name="X", price=100000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    db_session.add(TenantNegotiationSettings(tenant_id=tenant.id, enabled=negotiation_enabled))
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(product)
    await db_session.refresh(conversation)
    return tenant, product, conversation


@pytest.mark.asyncio
async def test_negotiate_price_tool_returns_counter(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("negotiate_price", {"product_id": str(product.id), "customer_offer": 80000})
    assert result["decision"] in ("COUNTER", "ACCEPT")


@pytest.mark.asyncio
async def test_negotiate_price_tool_disabled_returns_error(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email, negotiation_enabled=False)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("negotiate_price", {"product_id": str(product.id), "customer_offer": 80000})
    assert "error" in result


@pytest.mark.asyncio
async def test_negotiate_price_tool_invalid_offer(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("negotiate_price", {"product_id": str(product.id), "customer_offer": "pas un nombre"})
    assert "error" in result
