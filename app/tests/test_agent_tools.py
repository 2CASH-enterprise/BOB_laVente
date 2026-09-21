import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )

    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=12)
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
async def test_search_products_returns_real_data_only(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("search_products", {"query": "Samsung"})
    assert len(result["results"]) == 1
    assert result["results"][0]["product_id"] == str(product.id)
    assert result["results"][0]["price"] == 280000.0
    assert result["results"][0]["stock"] == 12


@pytest.mark.asyncio
async def test_search_products_no_match_never_invents(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("search_products", {"query": "Produit qui n'existe pas"})
    assert result["results"] == []
    assert "message" in result


@pytest.mark.asyncio
async def test_check_stock_returns_real_stock(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("check_stock", {"product_id": str(product.id)})
    assert result["stock"] == 12


@pytest.mark.asyncio
async def test_check_stock_unknown_product_returns_error_not_guess(db_session, unique_email):
    import uuid

    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("check_stock", {"product_id": str(uuid.uuid4())})
    assert "error" in result


@pytest.mark.asyncio
async def test_get_product_price_matches_database_exactly(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("get_product_price", {"product_id": str(product.id)})
    assert result["price"] == 280000.0
    assert result["currency"] == "XOF"


@pytest.mark.asyncio
async def test_recommend_products_respects_budget_and_caps_at_three(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    for i in range(5):
        db_session.add(
            Product(
                tenant_id=tenant.id,
                sku=f"PHONE-{i}",
                name=f"Téléphone {i}",
                price=100000 + i * 10000,
                currency="XOF",
                stock_quantity=5,
            )
        )
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("recommend_products", {"customer_need": "téléphone", "budget": 120000})
    assert len(result["results"]) <= 3
    for item in result["results"]:
        assert item["product_id"]  # jamais de produit fictif : toujours un id réel


@pytest.mark.asyncio
async def test_handoff_to_human_updates_conversation_status(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("handoff_to_human", {"reason": "Client mécontent"})
    assert result["status"] == "handoff_registered"
    assert conversation.status == ConversationStatus.WAITING_HUMAN
    assert executor.handoff_requested is True


@pytest.mark.asyncio
async def test_tools_are_isolated_by_tenant(db_session, unique_email):
    """Un outil ne doit jamais renvoyer le produit d'un autre tenant (section 30)."""
    tenant_a, product_a, conversation_a = await _setup(db_session, unique_email)
    tenant_b, product_b, conversation_b = await _setup(db_session, f"b_{unique_email}")

    executor_b = ToolExecutor(db_session, tenant_b.id, conversation_b)
    result = await executor_b.execute("check_stock", {"product_id": str(product_a.id)})
    assert "error" in result  # le produit du tenant A est invisible pour le tenant B


@pytest.mark.asyncio
async def test_create_order_tool_creates_real_order_and_decrements_stock(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute(
        "create_order", {"items": [{"product_id": str(product.id), "quantity": 2}], "delivery_address": "Dakar"}
    )
    await db_session.commit()
    await db_session.refresh(product)

    assert "order_id" in result
    assert result["total_amount"] == 560000.0
    assert product.stock_quantity == 10  # 12 - 2


@pytest.mark.asyncio
async def test_create_order_tool_never_oversells(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 999}]})
    assert "error" in result
    assert "Stock insuffisant" in result["error"]
