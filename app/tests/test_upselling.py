import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.product_complement import ProductComplement
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    main = Product(tenant_id=tenant.id, sku="IPH-15", name="iPhone 15", price=650000, currency="XOF", stock_quantity=5)
    case = Product(tenant_id=tenant.id, sku="CASE-15", name="Coque iPhone 15", price=8000, currency="XOF", stock_quantity=20)
    charger = Product(tenant_id=tenant.id, sku="CHRG-15", name="Chargeur rapide", price=12000, currency="XOF", stock_quantity=15)
    db_session.add_all([main, case, charger])
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    for p in (main, case, charger):
        await db_session.refresh(p)
    await db_session.refresh(conversation)
    return tenant, main, case, charger, conversation


@pytest.mark.asyncio
async def test_suggest_complementary_products_returns_configured_links(db_session, unique_email):
    tenant, main, case, charger, conversation = await _setup(db_session, unique_email)
    db_session.add(ProductComplement(tenant_id=tenant.id, product_id=main.id, complement_product_id=case.id))
    db_session.add(ProductComplement(tenant_id=tenant.id, product_id=main.id, complement_product_id=charger.id))
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("suggest_complementary_products", {"product_id": str(main.id)})

    assert len(result["results"]) == 2
    names = {r["name"] for r in result["results"]}
    assert names == {"Coque iPhone 15", "Chargeur rapide"}


@pytest.mark.asyncio
async def test_suggest_complementary_products_caps_at_two(db_session, unique_email):
    tenant, main, case, charger, conversation = await _setup(db_session, unique_email)
    earbuds = None
    from app.models.product import Product as P

    earbuds = P(tenant_id=tenant.id, sku="EAR-15", name="Écouteurs", price=25000, currency="XOF", stock_quantity=10)
    db_session.add(earbuds)
    await db_session.flush()

    for comp in (case, charger, earbuds):
        db_session.add(ProductComplement(tenant_id=tenant.id, product_id=main.id, complement_product_id=comp.id))
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("suggest_complementary_products", {"product_id": str(main.id)})
    assert len(result["results"]) <= 2  # jamais plus de 2 (section 22)


@pytest.mark.asyncio
async def test_suggest_complementary_products_none_configured(db_session, unique_email):
    tenant, main, case, charger, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("suggest_complementary_products", {"product_id": str(main.id)})
    assert result["results"] == []
    assert "message" in result


@pytest.mark.asyncio
async def test_suggest_complementary_products_excludes_inactive(db_session, unique_email):
    tenant, main, case, charger, conversation = await _setup(db_session, unique_email)
    case.active = False
    db_session.add(ProductComplement(tenant_id=tenant.id, product_id=main.id, complement_product_id=case.id))
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation)
    result = await executor.execute("suggest_complementary_products", {"product_id": str(main.id)})
    assert result["results"] == []


@pytest.mark.asyncio
async def test_complements_isolated_by_tenant(db_session, unique_email):
    tenant_a, main_a, case_a, charger_a, conversation_a = await _setup(db_session, unique_email)
    tenant_b, main_b, case_b, charger_b, conversation_b = await _setup(db_session, f"b_{unique_email}")

    db_session.add(ProductComplement(tenant_id=tenant_a.id, product_id=main_a.id, complement_product_id=case_a.id))
    await db_session.commit()

    executor_b = ToolExecutor(db_session, tenant_b.id, conversation_b)
    result = await executor_b.execute("suggest_complementary_products", {"product_id": str(main_a.id)})
    assert result["results"] == []  # le tenant B ne voit rien des liaisons du tenant A
