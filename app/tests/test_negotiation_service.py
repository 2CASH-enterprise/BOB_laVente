from decimal import Decimal

import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.negotiation import Negotiation, NegotiationStatus
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.negotiation_service import NegotiationError, negotiate_price


async def _setup(db_session, email: str, max_discount_pct=10.0, max_rounds=3):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=10)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    settings = TenantNegotiationSettings(tenant_id=tenant.id, max_discount_pct=max_discount_pct, max_rounds=max_rounds)
    db_session.add(settings)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(product)
    await db_session.refresh(conversation)
    return tenant, product, conversation, settings


@pytest.mark.asyncio
async def test_offer_at_or_above_catalog_price_is_accepted(db_session, unique_email):
    tenant, product, conversation, settings = await _setup(db_session, unique_email)
    result = await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("280000"), settings)
    assert result["decision"] == "ACCEPT"
    assert result["final_price"] == 280000.0


@pytest.mark.asyncio
async def test_offer_within_floor_accepted_immediately(db_session, unique_email):
    """Offre à 265 000 (plancher = 252 000 avec 10% max) : acceptée directement, aucun round consommé."""
    tenant, product, conversation, settings = await _setup(db_session, unique_email)
    result = await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("265000"), settings)
    assert result["decision"] == "ACCEPT"
    assert result["final_price"] == 265000.0


@pytest.mark.asyncio
async def test_offer_below_floor_triggers_counter_never_below_floor(db_session, unique_email):
    tenant, product, conversation, settings = await _setup(db_session, unique_email)
    result = await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("100000"), settings)
    assert result["decision"] == "COUNTER"
    assert result["proposed_price"] >= result["min_price"]
    assert result["min_price"] == 252000.0  # 280000 * (1 - 10%)


@pytest.mark.asyncio
async def test_counter_never_undercuts_previous_proposal(db_session, unique_email):
    """Bob cède progressivement : chaque contre-offre est <= la précédente, jamais l'inverse."""
    tenant, product, conversation, settings = await _setup(db_session, unique_email, max_rounds=5)
    prices = []
    for _ in range(4):
        result = await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("100000"), settings)
        if result["decision"] != "COUNTER":
            break
        prices.append(result["proposed_price"])
    for i in range(1, len(prices)):
        assert prices[i] <= prices[i - 1]


@pytest.mark.asyncio
async def test_escalates_to_human_after_max_rounds(db_session, unique_email):
    tenant, product, conversation, settings = await _setup(db_session, unique_email, max_rounds=2)
    last_result = None
    for _ in range(5):
        last_result = await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("50000"), settings)
        if last_result["decision"] == "ESCALATE_HUMAN":
            break
    assert last_result["decision"] == "ESCALATE_HUMAN"
    await db_session.refresh(conversation)
    assert conversation.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_unknown_product_raises_error(db_session, unique_email):
    import uuid

    tenant, product, conversation, settings = await _setup(db_session, unique_email)
    with pytest.raises(NegotiationError):
        await negotiate_price(db_session, tenant.id, conversation, uuid.uuid4(), Decimal("100000"), settings)


@pytest.mark.asyncio
async def test_negative_offer_raises_error(db_session, unique_email):
    tenant, product, conversation, settings = await _setup(db_session, unique_email)
    with pytest.raises(NegotiationError):
        await negotiate_price(db_session, tenant.id, conversation, product.id, Decimal("-100"), settings)


@pytest.mark.asyncio
async def test_negotiation_isolated_by_tenant(db_session, unique_email):
    tenant_a, product_a, conversation_a, settings_a = await _setup(db_session, unique_email)
    tenant_b, product_b, conversation_b, settings_b = await _setup(db_session, f"b_{unique_email}")

    with pytest.raises(NegotiationError):
        await negotiate_price(db_session, tenant_b.id, conversation_b, product_a.id, Decimal("100000"), settings_b)
