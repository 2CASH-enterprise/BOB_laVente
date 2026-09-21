import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.order import Order, OrderItem, OrderStatus
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
    popular = Product(tenant_id=tenant.id, sku="POP", name="Téléphone populaire", price=150000, currency="XOF", stock_quantity=10)
    unpopular = Product(tenant_id=tenant.id, sku="UNPOP", name="Téléphone méconnu", price=150000, currency="XOF", stock_quantity=10)
    db_session.add_all([popular, unpopular])
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    for p in (popular, unpopular):
        await db_session.refresh(p)
    await db_session.refresh(conversation)
    return tenant, popular, unpopular, customer, conversation


async def _buy(db_session, tenant, customer, product, quantity=1):
    order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=float(product.price) * quantity, currency="XOF", created_by="IA")
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=product.price, subtotal=float(product.price) * quantity))
    await db_session.commit()
    return order


@pytest.mark.asyncio
async def test_recommend_products_prioritizes_best_sellers_without_budget(db_session, unique_email):
    tenant, popular, unpopular, customer, conversation = await _setup(db_session, unique_email)
    # 5 ventes du "populaire", aucune de "l'inconnu"
    for _ in range(5):
        await _buy(db_session, tenant, customer, popular)

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("recommend_products", {"customer_need": "téléphone"})

    names = [r["name"] for r in result["results"]]
    assert names[0] == "Téléphone populaire"  # le plus vendu apparaît en premier


@pytest.mark.asyncio
async def test_recommend_products_never_invents_sales_data(db_session, unique_email):
    """Sans aucune vente, aucun produit n'est artificiellement favorisé — égalité réelle."""
    tenant, popular, unpopular, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("recommend_products", {"customer_need": "téléphone"})
    assert len(result["results"]) == 2  # les deux apparaissent, aucun n'est exclu sans raison


@pytest.mark.asyncio
async def test_frequently_bought_together_reflects_real_orders(db_session, unique_email):
    tenant, main, accessory, customer, conversation = await _setup(db_session, unique_email)
    # 3 commandes où les deux produits apparaissent ensemble
    for _ in range(3):
        order = Order(tenant_id=tenant.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=300000, currency="XOF", created_by="IA")
        db_session.add(order)
        await db_session.flush()
        db_session.add(OrderItem(order_id=order.id, product_id=main.id, quantity=1, unit_price=main.price, subtotal=main.price))
        db_session.add(OrderItem(order_id=order.id, product_id=accessory.id, quantity=1, unit_price=accessory.price, subtotal=accessory.price))
        await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("get_frequently_bought_together", {"product_id": str(main.id)})
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "Téléphone méconnu"


@pytest.mark.asyncio
async def test_frequently_bought_together_no_data_returns_empty(db_session, unique_email):
    tenant, popular, unpopular, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("get_frequently_bought_together", {"product_id": str(popular.id)})
    assert result["results"] == []
    assert "message" in result


@pytest.mark.asyncio
async def test_frequently_bought_together_isolated_by_tenant(db_session, unique_email):
    tenant_a, main_a, acc_a, customer_a, conversation_a = await _setup(db_session, unique_email)
    tenant_b, main_b, acc_b, customer_b, conversation_b = await _setup(db_session, f"b_{unique_email}")

    for _ in range(3):
        order = Order(tenant_id=tenant_a.id, customer_id=customer_a.id, status=OrderStatus.PENDING, total_amount=300000, currency="XOF", created_by="IA")
        db_session.add(order)
        await db_session.flush()
        db_session.add(OrderItem(order_id=order.id, product_id=main_a.id, quantity=1, unit_price=main_a.price, subtotal=main_a.price))
        db_session.add(OrderItem(order_id=order.id, product_id=acc_a.id, quantity=1, unit_price=acc_a.price, subtotal=acc_a.price))
        await db_session.commit()

    executor_b = ToolExecutor(db_session, tenant_b.id, conversation_b)
    result = await executor_b.execute("get_frequently_bought_together", {"product_id": str(main_a.id)})
    assert result["results"] == []  # le produit du tenant A n'existe pas pour le tenant B
